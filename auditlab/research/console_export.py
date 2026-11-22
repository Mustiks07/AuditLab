"""Explicit trusted-to-public export; the web server never scans experiment working files."""
from __future__ import annotations
import copy
import json
import re
import shutil
from pathlib import Path
from auditlab.research.agents.experiment import _load_attempts
from auditlab.research.agents.protocol import get_protocol
from auditlab.research.agents.execute import source_sha


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    if isinstance(value, dict) and value.get("artifact_kind"):
        from .artifacts import validate
        validate(value)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    temp.replace(path)


def _status_error(row):
    """Public reason of an unsuccessful attempt, e.g. ``rate_limit: HTTP 429``; never provider payloads."""
    detail = row.get('status_detail')
    if row.get('status') == 'completed' or not isinstance(detail, str) or not detail.strip():
        return None
    code, _, message = detail.partition(':')
    code = code.strip() if re.fullmatch(r'[a-z][a-z0-9_]{0,39}', code.strip()) else row.get('status')
    return {'code': code, 'message': (message.strip() or detail.strip())[:160]}


def publish(plan, output, target, *, comparison=None, state=None):
    """Publish completed attempt evidence and optionally an independently computed comparison.

    Holdout/test episodes are never exported. No provider config, transcript, private path or key
    is copied. Only explicit public snapshot files are copied, rejecting symlinks.
    """
    if any(e['split'] in ('holdout', 'test', 'final') for e in plan['episodes']):
        raise ValueError('sealed episodes cannot be published to the console')
    output, target = Path(output), Path(target)
    if target.resolve() == output.resolve() or output.resolve() in target.resolve().parents:
        raise ValueError('console export must be separate from trusted output')
    rows = _load_attempts(output)
    eid = plan['artifact_id']
    proto = get_protocol(plan['protocol_id'])
    base = {'schema_version':'1.0', 'source_sha':source_sha(), 'protocol_id':plan['protocol_id'],
            'provenance':{'kind':'measured', 'note':'Фактический запуск; локальные политики и API-модели указаны отдельно.'}}
    def artifact(kind, ident, **fields):
        return {**copy.deepcopy(base), 'artifact_kind':kind, 'artifact_id':ident, **fields}
    for row in rows:
        aid = row['artifact_id']
        ep = row['episode']
        public = Path(ep['episode_ref'])/'public'
        ref = f'snapshots/{aid}/public'
        dest = target/ref
        dest.mkdir(parents=True, exist_ok=True)
        for name in ('manifest.json','policy.json','ledger.csv','invoices.jsonl','payments.jsonl','fx.csv'):
            src = public/name
            if src.is_symlink() or public.resolve() not in src.resolve().parents:
                raise ValueError('unsafe public snapshot')
            shutil.copyfile(src, dest/name)
        steps = []
        steps_path = output/'attempts'/aid/'steps.jsonl'
        for raw in steps_path.read_text(encoding='utf-8').splitlines() if steps_path.exists() else []:
            step = json.loads(raw); action = step['action']; obs = step['observation']
            args = action.get('args') or {}
            if action.get('name') == 'submit': args = args.get('findings', {})
            steps.append({'step':step['index'], 'action':{'action':action.get('name'), 'args':args},
                          'observation':{'ok':obs.get('error') is None, 'result':obs.get('result'),
                                         'error':obs.get('error'), 'remaining_steps':obs.get('remaining_steps')}})
        ev = (row.get('_evaluation') or {}).get('official')
        evaluation = None if ev is None else {'visibility':'open', 'tp':ev['tp'], 'fp':ev['fp'], 'fn':ev['fn'],
                                               'reward':ev['return'], 'inspection_steps':row.get('inspection_steps')}
        diagnosis = (row.get('_evaluation') or {}).get('diagnosis')
        if evaluation is not None and diagnosis is not None:
            # Publish labels by submitted index, never private issue IDs or truth.
            evaluation['finding_diagnostics'] = [
                {'index': f['index'], 'label': f['label']} for f in diagnosis.get('findings', [])]
        totals = row.get('totals') or {}; tokens = totals.get('tokens') or {}
        kind = 'test_double' if row.get('test_double') else 'measured'
        attempt = artifact('attempt', aid, experiment_id=eid, variant_id=row.get('variant'),
                           repetition=row.get('repetition'), status=row['status'], snapshot_ref=ref,
                           error=_status_error(row),
                           episode={k:ep.get(k) for k in ('family_id','split')}, evaluation=evaluation,
                           submission=row.get('final_answer'), steps=steps,
                           usage={'input_tokens':tokens.get('input'), 'output_tokens':tokens.get('output'),
                                  'cost_usd':totals.get('cost_usd'), 'elapsed_s':totals.get('wall_seconds')},
                           started_at=row.get('started_at'), finished_at=row.get('finished_at'))
        attempt['provenance']['kind'] = kind
        write(target/'attempts'/f'{aid}.json', attempt)
    counts = {}
    for row in rows: counts[row['status']] = counts.get(row['status'],0)+1
    planned = len(plan['arms'])*len(plan['episodes'])*plan['repetitions']
    agents = [{'variant_id':a['name'], 'label':a.get('label',a['name']), 'kind':a.get('agent',{}).get('kind','llm'),
               'model':{'provider':a.get('provider',{}).get('provider'), 'model_id':a.get('provider',{}).get('model')}} for a in plan['arms']]
    for entry,arm in zip(agents,plan['arms']):
        if not arm.get('label') and arm.get('agent',{}).get('kind')=='llm':
            suffix={'tools':'без заметок','memory':'с заметками'}.get(arm['agent'].get('variant'),'аудитор')
            entry['label']=str(arm.get('provider',{}).get('model','ИИ'))+' · '+suffix
    spent = None if any(r['totals'].get('cost_usd') is None for r in rows) else sum(r['totals']['cost_usd'] for r in rows)
    write(target/'experiment.json', artifact('experiment',eid,title=plan.get('title','Проверка финансовых аудиторов'),
        status=state or ('completed' if len(rows)>=planned else 'incomplete'), agents=agents,
        mode={'protocol_id':proto.protocol_id,'official':proto.official,'action_limit':100,'time_limit_s':proto.time_limit_s},
        progress={'planned':planned,'completed':counts.get('completed',0),'running':0,
                  'failed':{k:v for k,v in counts.items() if k!='completed'}},
        repetitions=plan['repetitions'], attempt_ids=[r['artifact_id'] for r in rows],
        budget={'currency':'USD','limit':plan.get('budget',{}).get('max_usd_total'),'spent':spent},
        cost={'usd':spent}, split={'rule':'Исходное разбиение зафиксировано в плане; открытая выборка.'}))
    if comparison is not None:
        variants=[]
        for name, m in comparison['arms'].items():
            variants.append({'variant_id':name,'label':next((a['label'] for a in agents if a['variant_id']==name),name),'metrics':{
                'micro_f1':m['micro_f1']['value'], 'mean_reward':m.get('mean_reward_agent_ended',m['mean_reward'])['value'],
                'clean_fp':{'count':m['fp_on_clean']['attempts_with_findings'],'denominator':m['fp_on_clean']['clean_attempts']},
                'completion':{'completed':m['completion']['submitted'],'total':m['completion']['attempts']},
                'timeouts':m['status'].get('timeout',0),
                'errors':sum(n for status,n in m['status'].items() if status not in ('completed','timeout')),
                'actions_mean':m['actions']['mean'],'time_s':m['wall_time_s']['mean'],
                'cost_usd':None if m['cost_usd']['n']!=m['attempts'] else (m['cost_usd']['mean'] or 0)*m['attempts']}})
        # The console convention is b-a; the independent scorer reports a-b.
        paired = comparison.get('paired_by_family_agent_ended', comparison['paired_by_family'])
        pairs=[{'a':v['b'],'b':v['a'],'metric':'mean_reward','difference':v['difference'],'interval':v['ci95'],
                'method':'Парный bootstrap по экономическим семействам; сбои сервиса исключены'} for v in paired.values() if v['difference'] is not None and v['ci95'] is not None]
        missing=[{'variant_id':name,'reason':'incomplete_runs','message':f"Завершено {m['completion']['submitted']} из {m['attempts']} попыток; сбои сервиса не доказывают качество модели."}
                 for name,m in comparison['arms'].items() if m['completion']['submitted'] < m['attempts']]
        missing += [{'variant_id':v['a'],'reason':'insufficient_families','message':'Недостаточно независимых семейств для оценки неопределённости сравнения.'}
                    for v in paired.values() if v['ci95'] is None]
        write(target/'comparison.json',artifact('comparison',f'comparison-{eid}', experiment_id=eid,
            title='Независимое сравнение аудиторов',status='incomplete' if missing else 'completed', verification={'status':'passed'},
            verified_by='review.agent_generalization.compare', sample={'families':comparison['families'],
            'episodes':comparison['episodes'],'repetitions':plan['repetitions'],
            'split':plan['episodes'][0]['split'] if len({e['split'] for e in plan['episodes']})==1 else None,
            'note':'Внутренняя открытая проверка, не конкурсный балл. Качество и средняя награда исключают сбои сервиса; все попытки показаны в завершении.'},
            variants=variants,paired=pairs,missing=missing))
    return {'experiment_id':eid,'attempts':len(rows),'target':str(target)}


def publish_curriculum(output, target):
    """Project measured GPT 1 search/reduction and Claude 2 receipts, never the sealed partition."""
    import hashlib
    root,target=Path(output).resolve(),Path(target).resolve()
    exp=read(root/'experiment.json'); red=read(root/'reduction/reduction.json'); failure=read(root/'failure.json')
    cur=read(root/'curriculum/curriculum.json'); eid=exp['artifact_id'];rid=red['artifact_id'];fid=failure['artifact_id']
    members={m['candidate_id']:m for m in exp['partition']['members']}
    if members[failure['candidate_id']]['split'] not in ('train','dev'):raise ValueError('sealed failure')
    base={'schema_version':'1.0','source_sha':exp['source_sha'],'protocol_id':exp['protocol_id'],
          'provenance':{'kind':'measured','note':'Повторный запуск встроенной политики; не результат ИИ-модели.'}}
    def make(artifact_kind,ident,**fields):return {**copy.deepcopy(base),'artifact_kind':artifact_kind,'artifact_id':ident,**fields}
    def safe(path):
        p=Path(path).resolve()
        if root not in p.parents:raise ValueError('reference outside experiment')
        return p
    def snapshot(path,label):
        public=safe(path)/'public';dest=target/'snapshots'/label/'public';dest.mkdir(parents=True,exist_ok=True)
        for name in ('manifest.json','policy.json','ledger.csv','invoices.jsonl','payments.jsonl','fx.csv'):
            src=public/name
            if src.is_symlink():raise ValueError('symlink snapshot')
            shutil.copyfile(src,dest/name)
        return f'snapshots/{label}/public'
    selected=next(p for p in red['proposals'] if p['snapshot']==red['selected_snapshot'])
    attempt_ids=[]; sides={};repeats={}
    for label,part in [('original',red['baseline']),('reduced',selected)]:
        ref=snapshot(part['snapshot'],label);validation=part['validation'];vid=f'{rid}-{label}-validation'
        answer_hash=validation['answer_sha256_before_truth']
        answers=[p for p in root.rglob('answer_before_truth.json') if hashlib.sha256(p.read_bytes()).hexdigest()==answer_hash]
        if not answers:raise ValueError('missing independently committed answer')
        answer=read(answers[0])
        write(target/'validations'/f'{vid}.json',make('validation',vid,snapshot_ref=ref,
            independent={'engine':'Независимый финансовый расчёт','status':validation['status'],'findings':answer['findings']},
            source_book=validation['checks']['source_book_consistent'],witness=validation['checks']['witness_trajectory']))
        aids=[]
        for item in part['attempts']:
            receipt=item['receipt'];aid=receipt['attempt_id'];aids.append(aid);attempt_ids.append(aid)
            directory=safe(item['output_dir'])/'runner'/aid
            steps=[json.loads(l) for l in (directory/'trajectory.jsonl').read_text().splitlines() if l.strip()]
            report=read(directory/'report.json');score=report['evaluation']['score']
            submit=next((s['action']['args'] for s in reversed(steps) if s['action']['action']=='submit'),None)
            write(target/'attempts'/f'{aid}.json',make('attempt',aid,experiment_id=eid,variant_id=exp['agent']['name'],
                status='completed' if receipt['status']=='submitted' else receipt['status'],repetition=item['repetition'],
                snapshot_ref=ref,episode={'split':members[failure['candidate_id']]['split'],'family_id':members[failure['candidate_id']]['family_id']},
                evaluation={'visibility':'open',**{k:score[k] for k in ('tp','fp','fn','reward')},'inspection_steps':report['inspection_steps']},
                submission=submit,steps=steps,usage={'cost_usd':receipt['cost_micros']/1e6,'elapsed_s':receipt['elapsed_seconds']}))
        sides[label]={'snapshot_ref':ref,'validation_id':vid,**{k:part['sizes'][k] for k in ('ledger_rows','documents')}}
        repeats[label]={'failed':part['frequency']['matched'],'total':part['frequency']['attempted'],'attempt_ids':aids}
    frequency=red['baseline']['frequency'];interval=frequency['per_attempt']['wilson_95']
    write(target/'experiment.json',make('experiment',eid,title='Поиск и сокращение финансовой ошибки',status=exp['status'],
        mode={'protocol_id':exp['budget']['episode_protocol_id'],
              'official':exp['budget']['episode_protocol_id']=='genesis-official-60s',
              'action_limit':exp['budget']['actions_per_episode'],'time_limit_s':exp['budget']['seconds_per_episode']},
        agents=[{'variant_id':exp['agent']['name'],'label':'Случайный аудитор','kind':'baseline_policy'}],
        attempt_ids=attempt_ids,progress={'planned':len(attempt_ids),'completed':len(attempt_ids),'running':0},
        repetitions=red['budget']['repetitions'],budget={'currency':'USD','limit':0,'spent':red['cost']['cost_micros']/1e6}))
    write(target/'failure.json',make('failure',fid,experiment_id=eid,title='Пропущенное распределение платежа',
        finding_type=failure['target']['issue_type'],variant_id=exp['agent']['name'],
        family_id=members[failure['candidate_id']]['family_id'],attempt_ids=repeats['original']['attempt_ids'],
        validation_id=sides['original']['validation_id'],reduction_id=rid,
        criterion={'text':'Аудитор не обнаружил подтверждённую ошибку распределения платежа.'},
        frequency={'failed':frequency['matched'],'total':frequency['attempted']},
        interval={'low':interval[0],'high':interval[1],'method':'Уилсон, 95%'},alternatives=['Проверена встроенная случайная политика; перенос на ИИ-модель не измерен.']))
    write(target/'reduction.json',make('reduction',rid,experiment_id=eid,failure_id=fid,**sides,repeats=repeats,families=[],
        method='Удаление групп с независимой проверкой и новыми расследованиями',
        transformations=['Удалены не связанные с ошибкой записи и документы; ошибка подтверждена повторно.' for p in red['proposals'] if p['accepted']]))
    items=[];filtered=[];splits={}
    for item in cur['examples']:
        if item['split'] not in ('train','dev'):raise ValueError('sealed curriculum item')
        if item['status']!='included':
            filtered.append({'title':'Повтор оформления','reason':item.get('reason')});continue
        splits[item['split']]=splits.get(item['split'],0)+1
        provenance=item.get('provenance') or {}
        origin=('reduction' if provenance.get('reduction') else 'family_transform' if item.get('parent')
                else 'clean_control' if provenance.get('selection')=='predeclared_clean_control'
                else 'failure' if provenance.get('selection')=='observed_agent_failure' else 'search')
        items.append({'item_id':item['candidate_id'],'title':'Финансовый случай '+str(len(items)+1),
            'split':item['split'],'family_id':item['family_id'],'hash':item['snapshot_sha256'],
            'validation':item['validation']['status'],'origin':origin})
    write(target/'curriculum.json',make('curriculum',cur['artifact_id'],experiment_id=eid,title='Проверенный учебный набор',
        failure_ids=[fid],reduction_ids=[rid],items=items,filtered=filtered,splits=splits,export_formats=['SFT'],split_hash=exp['partition']['partition_sha256']))
    write(target/'training.json',make('training','training-'+eid,curriculum_id=cur['artifact_id'],kind='weights',
        state='not_available',result=None,error={'code':'not_checked','message':'Обучение весов не запускалось: модель и GPU не настроены.'}))
    write(target/'comparison.json',make('comparison','comparison-'+eid,experiment_id=eid,title='Эффект обучения ещё не измерен',
        status='not_checked',verification={'status':'not_checked','reason':'Нет обученных моделей и финального эксперимента.'},
        variants=[],paired=[],missing=[{'variant_id':'weights','reason':'Обучение весов и независимый финальный прогон не выполнены.'}]))
    return {'experiment_id':eid,'attempts':len(attempt_ids),'target':str(target)}


# Public facets of a generated company. They describe the design of open train/dev cases
# (already evaluated), never seeds, private paths or generator internals.
_FACETS = ('settlement_timing', 'interaction', 'currencies', 'base_currency', 'allocation_mode')
_CHANGE = {'id_order': 'ids_and_order', 'lawful_refund': 'clean_distractors', 'fixed': 'settlement_timing',
           'currency': 'base_currency'}


def publish_families(output, target):
    """Project the searched candidates and their paired financial variants to console candidates.

    Every train/dev candidate becomes a ``candidate`` artifact with its public design facets and, when the
    independent reviewer checked it, a ``validation``. Each paired variant (rename, lawful distractor,
    fixed defect, changed currency) references its parent and carries the expected answer that the
    trusted pipeline compared with the independent calculation. Sealed partitions are never exported.
    """
    root, target = Path(output).resolve(), Path(target).resolve()
    if target == root or root in target.parents:
        raise ValueError('console export must be separate from trusted output')
    exp = read(root/'experiment.json')
    members = {m['candidate_id']: m for m in exp['partition']['members']}
    base = {'schema_version':'1.0', 'source_sha':exp['source_sha'], 'protocol_id':exp['protocol_id'],
            'provenance':{'kind':'measured', 'note':'Генератор и независимая проверка выполнены; это не ответы ИИ-модели.'}}

    def make(kind, ident, **fields):
        return {**copy.deepcopy(base), 'artifact_kind':kind, 'artifact_id':ident, **fields}

    def safe(path):
        p = Path(path).resolve()
        if root not in p.parents:
            raise ValueError('reference outside experiment')
        return p

    def snapshot(path, label):
        public = safe(path)/'public'
        dest = target/'snapshots'/label/'public'
        dest.mkdir(parents=True, exist_ok=True)
        for name in ('manifest.json','policy.json','ledger.csv','invoices.jsonl','payments.jsonl','fx.csv'):
            src = public/name
            if src.is_symlink() or public not in src.resolve().parents:
                raise ValueError('unsafe public snapshot')
            shutil.copyfile(src, dest/name)
        return f'snapshots/{label}/public'

    def validation(directory, ident, ref):
        directory = safe(directory)
        if not (directory/'validation.json').exists():
            return None
        receipt = read(directory/'validation.json')
        answer = read(directory/'answer_before_truth.json') if (directory/'answer_before_truth.json').exists() else None
        checks = receipt.get('checks') or {}
        def status(name):
            item = checks.get(name) or {}
            return {'status': item.get('status', 'not_checked')}
        findings = [{k: f[k] for k in ('type','claimed_amount_minor','currency','affected_ids','evidence_ids') if k in f}
                    for f in (answer or {}).get('findings') or []]
        vid = f'{ident}-validation'
        write(target/'validations'/f'{vid}.json', make('validation', vid, snapshot_ref=ref,
            independent={'engine':'Независимый финансовый расчёт', 'status':receipt.get('status', 'not_checked'),
                         'findings':findings, 'arithmetic':(answer or {}).get('arithmetic') or []},
            source_book=status('source_book_consistent'), witness=status('witness_trajectory')))
        return vid

    checked = {}
    search_rows = read(root/'search/search.json')['arms'].values() if (root/'search/search.json').exists() else []
    for arm in search_rows:
        for row in arm['candidates']:
            ref = (row.get('validation') or {}).get('artifact_ref')
            if ref and (row.get('validation') or {}).get('status') == 'passed':
                checked.setdefault(row['candidate_id'], safe(ref).parent)
    for item in exp.get('clean_calibration') or []:
        checked.setdefault(item['candidate_id'], safe(item['validation_ref']).parent)
    for key in {p['parent'] for p in exp.get('paired') or []}:
        checked[key] = root/'pairs'/key/'baseline-validation'

    written = []
    for receipt_path in sorted((root/'candidates').glob('*/candidate.json')):
        receipt = read(receipt_path)
        candidate = receipt['candidate']
        key = candidate['candidate_id']
        member = members.get(key)
        if member is None or member['split'] not in ('train', 'dev') or receipt.get('status') == 'generation_failed':
            continue
        config = candidate['config']
        ref = snapshot(receipt_path.parent/'snapshot', key)
        counts = config.get('issue_counts') or {}
        design = {k: config[k] for k in _FACETS if k in config}
        design.update(stratum=candidate.get('stratum'), issue_types=sorted(t for t, n in counts.items() if n),
                      clean=not any(counts.values()))
        vid = validation(checked[key], key, ref) if key in checked else None
        write(target/'candidates'/f'{key}.json', make('candidate', key, family_id=member['family_id'],
              split=member['split'], snapshot_ref=ref, role='original', design=design, validation_id=vid))
        written.append(key)
    for pair in exp.get('paired') or []:
        key, parent = pair['candidate_id'], pair['parent']
        if members[parent]['split'] not in ('train', 'dev'):
            raise ValueError('sealed paired example')
        ref = snapshot(root/'pairs'/parent/'snapshots'/pair['kind'], key)
        vid = validation(root/'pairs'/parent/pair['kind'], key, ref)
        parameter = _CHANGE.get(pair['kind'])
        before = read(root/'candidates'/parent/'candidate.json')['candidate']['config']
        after = pair['config']
        change = {'parameter': parameter}
        if parameter in ('settlement_timing', 'base_currency'):
            change.update({'from': before.get(parameter), 'to': after.get(parameter)})
        elif parameter == 'clean_distractors':
            change.update({'added': sorted(set(after.get(parameter) or []) - set(before.get(parameter) or []))})
        counts = after.get('issue_counts') or {}
        design = {k: after[k] for k in _FACETS if k in after}
        design.update(stratum=read(root/'candidates'/parent/'candidate.json')['candidate'].get('stratum'),
                      issue_types=sorted(t for t, n in counts.items() if n), clean=not any(counts.values()))
        write(target/'candidates'/f'{key}.json', make('candidate', key, family_id=members[parent]['family_id'],
              split=members[parent]['split'], snapshot_ref=ref, role='variant', parent_id=parent,
              transformation=pair['kind'], reason=pair['reason'], change=change, design=design,
              expected=[{k: f[k] for k in ('type','claimed_amount_minor','currency')} for f in pair['expected']],
              expected_check=pair['status'], validation_id=vid))
        written.append(key)
    return {'candidates': len(written), 'target': str(target)}
