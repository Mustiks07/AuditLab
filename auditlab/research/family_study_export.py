"""Public projection of checked profile-1.2 financial controls; no sealed inputs."""
from pathlib import Path
import copy
import hashlib
import shutil

from .console_export import read, write
from auditlab.release.rehearsal import file_hashes

KINDS = {'period_crossing': 'boundary', 'lawful_distractor': 'lawful_refund'}
PUBLIC = ('manifest.json', 'policy.json', 'ledger.csv', 'invoices.jsonl', 'payments.jsonl', 'fx.csv')


def study_members(root):
    root = Path(root).resolve()
    study = read(root/'study.json')
    members, seen = [], set()
    for pair in study.get('pair_validation') or []:
        if pair.get('status') != 'passed' or not pair.get('kind'):
            continue
        parent, kind = pair['root'], pair['kind']
        for name in (parent, kind):
            if not isinstance(name, str) or '/' in name or '\\' in name or name in ('.', '..'):
                raise ValueError('unsafe financial control identifier')
        candidate = read(root/'cases'/parent/'candidate.json')['candidate']
        if candidate['split'] not in ('train', 'dev'):
            raise ValueError('sealed financial control cannot be exported')
        base = root/'financial-controls'/parent
        if parent not in seen:
            members.append(dict(id='v2-'+parent, candidate=candidate, snapshot=root/'cases'/parent/'s',
                                validation=base/'base', parent=None, kind=None, pair=None))
            seen.add(parent)
        members.append(dict(id='v2-'+parent+'-'+kind, candidate=candidate, snapshot=base/'variants'/kind,
                            validation=base/kind, parent='v2-'+parent, kind=KINDS.get(kind, kind), pair=pair))
    for member in members:
        for field in ('snapshot', 'validation'):
            if root not in member[field].resolve().parents:
                raise ValueError('financial control escaped its trusted directory')
    return members


def publish_study(root, target):
    study = read(root/'study.json')
    base = {'schema_version': '1.0', 'source_sha': study['source_sha'], 'protocol_id': study['protocol_id'],
            'provenance': {'kind': 'measured', 'note': 'Генератор 1.8: связанные ситуации проверены независимым решателем.'}}
    def artifact(kind, ident, **fields):
        return {**copy.deepcopy(base), 'artifact_kind': kind, 'artifact_id': ident, **fields}
    members = study_members(root)
    for m in members:
        answer = read(m['validation']/'answer_before_truth.json')
        receipt = read(m['validation']/'validation.json')
        if receipt.get('status') != 'passed':
            raise ValueError('financial control validation is not passed')
        if (receipt.get('binding') != {side: file_hashes(m['snapshot']/side) for side in ('public','private')}
                or receipt.get('answer_sha256_before_truth') != hashlib.sha256(
                    (m['validation']/'answer_before_truth.json').read_bytes()).hexdigest()):
            raise ValueError('financial control no longer matches its independent receipt')
        ref = 'snapshots/'+m['id']+'/public'
        public = m['snapshot']/'public'
        dest = target/ref
        dest.mkdir(parents=True, exist_ok=True)
        for name in PUBLIC:
            src = public/name
            if src.is_symlink() or root not in src.resolve().parents:
                raise ValueError('unsafe public financial control')
            shutil.copyfile(src, dest/name)
        vid = m['id']+'-validation'
        findings = [{k: f[k] for k in ('type','affected_ids','claimed_amount_minor','currency','evidence_ids')}
                    for f in answer['findings']]
        write(target/'validations'/f'{vid}.json', artifact('validation',vid,snapshot_ref=ref,
            independent={'status':'passed','engine':'Независимый финансовый расчёт',
                         'findings':findings,'arithmetic':answer.get('arithmetic',[])},
            source_book={'status':receipt['checks']['source_book_consistent']['status']},
            witness={'status':receipt['checks']['independent_solver']['status']}))
        manifest = read(public/'manifest.json')
        write(target/'candidates'/f"{m['id']}.json", artifact('candidate',m['id'],
            family_id=m['candidate']['family_id'],split=m['candidate']['split'],snapshot_ref=ref,
            role='variant' if m['parent'] else 'original', parent_id=m['parent'], transformation=m['kind'],
            validation_id=vid, expected=findings, expected_check='passed',
            design={'stratum':m['candidate']['stratum'],'issue_types':sorted({f['type'] for f in findings}),
                    'clean':not findings,'currencies':sorted(manifest['currency_exponents']),
                    'settlement_timing':'changed' if m['kind']=='boundary' else 'not_checked'},
            change={'parameter':m['kind']}, reason=(m['pair'] or {}).get('relationship')))
    return {'candidates':len(members),'target':str(target)}
