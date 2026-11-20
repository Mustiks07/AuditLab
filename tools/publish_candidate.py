"""Publish an immutable candidate only from merged, successful, exact-source CI artifacts.

No builds or new measurements occur here. A failed upload/verification leaves a
DRAFT for inspection; this tool never replaces an existing tag, release or asset.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REPO = 'Mustiks07/AuditLab'
JOBS = {'verify', 'frontend', 'Build offline bundle',
        'Internal offline rehearsal (classic)', 'Internal offline rehearsal (containerd)'}
STORES = ('classic', 'containerd')
PUBLIC_FILES = {'manifest.json', 'policy.json', 'ledger.csv', 'invoices.jsonl', 'payments.jsonl', 'fx.csv'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def gh(*args, payload=None):
    return subprocess.check_output(['gh', *args], input=None if payload is None else json.dumps(payload), text=True)


def api(path, payload=None, method=None):
    args = ['api', path]
    if method:
        args += ['--method', method]
    if payload is not None:
        args += ['--input', '-']
    return json.loads(gh(*args, payload=payload))


def pages(path, key=None):
    raw = gh('api', path, '--paginate')
    decoder, values = json.JSONDecoder(), []
    while raw.strip():
        page, end = decoder.raw_decode(raw.lstrip())
        values.extend(page[key] if key else page)
        raw = raw.lstrip()[end:]
    return values


def artifact_names(source):
    return {f'offline-bundle-{source}', *(f'offline-evidence-{store}-{source}' for store in STORES)}


def validate_ci(run, jobs, artifacts, source, run_id):
    """Pure guard: green badges from another SHA/attempt/job are not publication evidence."""
    require(run.get('id') == run_id and run.get('head_sha') == source, 'CI run/source revision differs')
    require(run.get('repository', {}).get('full_name') == REPO
            and run.get('head_repository', {}).get('full_name') == REPO, 'CI repository differs')
    require(run.get('path') == '.github/workflows/verify.yml', 'wrong CI workflow')
    require(run.get('event') in {'push', 'workflow_dispatch'}, 'PR merge-ref CI cannot certify a source release')
    require(run.get('status') == 'completed' and run.get('conclusion') == 'success', 'CI run has not succeeded')
    attempt = run.get('run_attempt')
    require(type(attempt) is int and attempt > 0, 'missing CI attempt')
    for name in JOBS:
        matched = [job for job in jobs if job.get('name') == name]
        require(len(matched) == 1, f'missing or duplicate CI job: {name}')
        job = matched[0]
        require(job.get('head_sha') == source and job.get('run_id') == run_id
                and job.get('run_attempt') == attempt, f'stale job receipt: {name}')
        require(job.get('status') == 'completed' and job.get('conclusion') == 'success', f'failed or incomplete CI job: {name}')
    accepted = {}
    for name in artifact_names(source):
        matched = [artifact for artifact in artifacts if artifact.get('name') == name]
        require(len(matched) == 1, f'missing or duplicate artifact: {name}')
        artifact = matched[0]
        require(artifact.get('expired') is False and artifact.get('size_in_bytes', 0) > 0, f'unavailable artifact: {name}')
        require(artifact.get('workflow_run', {}).get('id') == run_id
                and artifact['workflow_run'].get('head_sha') == source, f'artifact provenance differs: {name}')
        require(type(artifact.get('id')) is int and re.fullmatch(r'sha256:[0-9a-f]{64}', artifact.get('digest', '')),
                f'missing immutable artifact identity: {name}')
        accepted[name] = {key: artifact[key] for key in ('id', 'size_in_bytes', 'digest')}
    return {'repository': REPO, 'source_revision': source, 'run_id': run_id,
            'run_attempt': attempt, 'artifacts': accepted}


def current_ci(source, run_id):
    prefix = f'repos/{REPO}/actions/runs/{run_id}'
    run = api(prefix)
    jobs = pages(f'{prefix}/jobs?filter=latest&per_page=100', 'jobs')
    artifacts = pages(f'{prefix}/artifacts?per_page=100', 'artifacts')
    return validate_ci(run, jobs, artifacts, source, run_id)


def require_merged_source(source):
    subprocess.run(['git', 'fetch', 'origin', 'main'], cwd=ROOT, check=True)
    subprocess.run(['git', 'merge-base', '--is-ancestor', source, 'origin/main'], cwd=ROOT, check=True)


def require_unused_tag(tag):
    # Listing by ID also sees drafts; GET /releases/tags/TAG can return 404 for a draft.
    releases = pages(f'repos/{REPO}/releases?per_page=100')
    require(not any(release.get('tag_name') == tag for release in releases), 'refuse to replace an existing release, including a draft')
    existing = subprocess.run(['git', 'ls-remote', '--exit-code', '--tags', 'origin', f'refs/tags/{tag}'],
                              cwd=ROOT, capture_output=True, text=True)
    require(existing.returncode == 2, 'tag exists or remote tag absence could not be verified')


def validate_preload(preload, images):
    require(preload.get('status') == 'passed', 'fresh-host preload check did not pass')
    present, checked = preload.get('images_present_before_load', {}), preload.get('checked_identities', {})
    require(set(present) == set(checked) == set(images), 'preload image roles differ')
    for role, image in images.items():
        expected = {image[key] for key in ('id', 'oci_manifest_digest', 'tag') if image.get(key)}
        require(present[role] is False and set(checked[role]) == expected
                and all(value is False for value in checked[role].values()), f'preloaded or unchecked identity: {role}')


def validate_source_books(datasets, snapshot_root):
    require(len(datasets) == 20 and {r.get('id') for r in datasets} == {f'internal-{i:02d}' for i in range(1, 21)},
            'need twenty distinct source-book receipts')
    receipts = {}
    for row in datasets:
        receipt, hashes = row.get('source_book', {}), row.get('snapshot_sha256', {})
        require(row.get('status') == receipt.get('status') == 'passed' and receipt.get('source_findings') == 0,
                f'source book not independently clean: {row["id"]}')
        source, target = receipt.get('source_files_sha256', {}), receipt.get('target_files_sha256', {})
        require(set(source) == set(target) == PUBLIC_FILES, 'source-book file set differs')
        expected = {**{'private/source_book/' + name: value for name, value in source.items()},
                    **{'public/' + name: value for name, value in target.items()},
                    **{'private/' + name: receipt.get(field) for name, field in (
                        ('source_transition.json', 'transition_sha256'), ('provenance.json', 'provenance_sha256'),
                        ('truth.json', 'truth_sha256'))}}
        for name, value in expected.items():
            path = snapshot_root / row['id'] / name
            require(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) and hashes.get(name) == value
                    and path.is_file() and not path.is_symlink() and digest(path) == value,
                    f'source receipt is not bound to packaged bytes: {row["id"]}/{name}')
        receipts[row['id']] = receipt
    return receipts


def build_packages(workspace, source, run_id, tag, ci_receipt):
    from offline import verify_bundle
    from tools.check_rehearsal_evidence import check
    from auditlab.release.acceptance import matrix
    bundle, evidence = workspace / 'bundle', workspace / 'evidence'
    manifest = verify_bundle(bundle)
    require(manifest['source_revision'] == source, 'bundle source differs')
    report = {'source_revision': source, 'workflow_run': f'https://github.com/{REPO}/actions/runs/{run_id}',
              'ci_receipt': ci_receipt, 'payload_size_bytes': manifest['payload_size_bytes'], 'images': manifest['images'],
              'scope': 'Measured internal candidate evidence; organizer acceptance, human handoff and whole-host 4GB memory remain unverified',
              'stores': {}}
    for store in STORES:
        root = evidence / store
        summary = check(root, bundle, source)
        require(summary['exit_code'] == 0, f'{store}: incomplete rehearsal evidence: {summary}')
        attempts = list(root.glob('verify-*/launch.json'))
        require(len(attempts) == 1, f'{store}: ambiguous attempt')
        attempt = attempts[0].parent
        read = lambda path: json.loads(path.read_text(encoding='utf-8'))
        launch, general, rehearsal = (read(attempt / path) for path in ('launch.json', 'verify/report.json', 'verify/rehearsal/report.json'))
        controller, preload = read(attempt / 'controller-resources.json'), read(root / 'preload.json')
        require(launch.get('cleanup', {}).get('status') == 'passed', f'{store}: launcher cleanup did not pass')
        validate_preload(preload, manifest['images'])
        ids = read(root / 'resolved-images.json')
        require(ids == {role: image['id'] for role, image in launch['loaded_images'].items()}, 'receiver image IDs differ')
        acceptance = matrix(general, source)
        require(general.get('acceptance') == acceptance, 'acceptance summary is stale or differs from executed evidence')
        require(len(acceptance) == 24 and all(row['status'] == ('not_checked' if row['id'] in {'OPS-01', 'OPS-02'} else 'passed')
                for row in acceptance), 'expected twenty-two passed internal groups and explicit OPS-01/02 gaps')
        receipts = validate_source_books(rehearsal['datasets'], attempt / 'verify/rehearsal/snapshots')
        report['stores'][store] = {
            'tests': sum(row['tests_run'] for row in general['checks']),
            'summary': {key: value for key, value in summary.items() if key != 'result_root'},
            'acceptance': acceptance, 'acceptance_counts': dict(Counter(row['status'] for row in acceptance)),
            'source_book_receipts': receipts, 'preload': preload,
            'docker_version': launch['docker_server_version'], 'loaded_images': launch['loaded_images'],
            'strict_exit_code': launch['exit_code'], 'controller_peak_bytes': controller['peak_memory_bytes'],
            'policy_peak_bytes': max(row['resources']['peak_memory_bytes'] for row in rehearsal['episodes'] if row.get('resources')),
            'verifier_peak_bytes': max(row['independent']['resources']['peak_memory_bytes'] for row in rehearsal['datasets'] + rehearsal['pairs']),
            'max_episode_seconds': max(row['elapsed_seconds'] for row in rehearsal['episodes']),
            'image_load_seconds': launch['image_load_seconds'], 'verification_container_seconds': launch['verification_container_seconds'],
            'policies': rehearsal['summary'],
        }
    dist = workspace / 'dist'
    dist.mkdir(exist_ok=False)
    for directory, suffix in ((bundle, 'offline'), (evidence, 'evidence')):
        with zipfile.ZipFile(dist / f'AuditLab-{tag}-{suffix}.zip', 'w', compression=zipfile.ZIP_STORED if suffix == 'offline' else zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(directory.rglob('*')):
                require(not path.is_symlink(), 'refuse symlinked package contents')
                if path.is_file():
                    archive.write(path, path.relative_to(directory))
    (dist / f'AuditLab-{tag}-REPORT.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    checksums = {path.name: digest(path) for path in sorted(dist.iterdir())}
    (dist / f'AuditLab-{tag}-SHA256SUMS.txt').write_text(''.join(f'{sha}  {name}\n' for name, sha in checksums.items()), encoding='utf-8')
    return dist, report


def verify_assets(release, dist):
    assets = release.get('assets', [])
    require(len(assets) == len(list(dist.iterdir())), 'release asset count differs')
    by_name = {asset['name']: asset for asset in assets}
    require(set(by_name) == {path.name for path in dist.iterdir()}, 'release asset names differ')
    for path in dist.iterdir():
        asset = by_name[path.name]
        require(asset.get('state') == 'uploaded' and asset.get('size') == path.stat().st_size
                and asset.get('digest') == 'sha256:' + digest(path), f'uploaded asset differs: {path.name}')


def publish(tag, source, run_id, workspace, receipt_path):
    require_merged_source(source)
    require_unused_tag(tag)
    original_receipt = json.loads(receipt_path.read_text())
    require(current_ci(source, run_id) == original_receipt, 'CI attempt/artifacts changed since download guard')
    dist, report = build_packages(workspace, source, run_id, tag, original_receipt)
    classic, containerd = (report['stores'][store] for store in STORES)
    require(classic['tests'] == containerd['tests'], 'test counts differ across stores')
    body = f'''Кандидат {tag}; точный исходный коммит `{source}`.

[Проверенный CI]({report['workflow_run']}): на каждом из двух Docker-хранилищ {classic['tests']} тестов, 20 снимков, 80 попыток и 20 пар. Все 20 исходных книг независимо проверены; хеши исходников, мутаций и эталонов связаны с приложенными файлами.

Внутренняя матрица SPEC: 22 группы пройдены, OPS-01/OPS-02 остаются «не проверено». Это не конкурсный балл и не приёмка организатора. Приёмка новым человеком и полная память машины с 4 GB RAM не подтверждены. Контейнерный бюджет — 1536 MiB на процесс-контейнер, 3 GiB controller + одна policy/verifier; Docker/ОС вне этого измерения.

После распаковки offline.zip: `python offline.py check`, затем `python offline.py verify --suite rehearsal`. Нужны Linux x86_64, Python 3.11 и локальный Docker с cgroup v2. Строгий verify ожидаемо завершился кодом 2 из-за незакрытых пунктов; internal evidence checker — кодом 0. Подробные фактические измерения каждого хранилища, явные знаменатели метрик и оставшиеся ограничения сохранены в REPORT.json/evidence.zip. SHA256SUMS проверяет поставляемые файлы.
'''
    release = api(f'repos/{REPO}/releases', method='POST', payload={
        'tag_name': tag, 'target_commitish': source, 'name': f'AuditLab {tag} — проверенный кандидат',
        'body': body, 'draft': True, 'prerelease': True, 'make_latest': 'false'})
    release_id = release['id']
    require(release.get('draft') is True and release.get('tag_name') == tag, 'created release is not the expected draft')
    for path in sorted(dist.iterdir()):
        gh('api', f'https://uploads.github.com/repos/{REPO}/releases/{release_id}/assets?name={quote(path.name, safe="")}',
           '--method', 'POST', '--header', 'Content-Type: application/octet-stream', '--input', str(path))
    endpoint = f'repos/{REPO}/releases/{release_id}'
    uploaded = api(endpoint)  # ID works for private drafts; tag lookup is intentionally never used.
    require(uploaded.get('draft') is True and uploaded.get('target_commitish') == source
            and uploaded.get('tag_name') == tag, 'draft source identity changed')
    verify_assets(uploaded, dist)
    require(current_ci(source, run_id) == original_receipt, 'CI changed during upload; draft retained')
    published = api(endpoint, method='PATCH', payload={'draft': False, 'prerelease': True, 'make_latest': 'false'})
    require(published.get('draft') is False and published.get('prerelease') is True, 'candidate publication not confirmed')
    verify_assets(api(endpoint), dist)
    tag_ref = api(f'repos/{REPO}/git/ref/tags/{quote(tag, safe="")}')
    require(tag_ref.get('object', {}).get('type') == 'commit' and tag_ref['object'].get('sha') == source,
            'published tag does not point at accepted source')
    print(json.dumps({'url': published['html_url'], 'source': source, 'release_id': release_id,
                      'assets': uploaded['assets']}, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('guard', 'publish'))
    parser.add_argument('--tag', required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--run-id', required=True, type=int)
    parser.add_argument('--workspace', type=Path, default=Path('publication'))
    parser.add_argument('--receipt', type=Path, default=Path('publication/ci-receipt.json'))
    args = parser.parse_args(argv)
    require(re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+-rc\.[1-9][0-9]*', args.tag), 'tag must be a versioned rc prerelease')
    require(re.fullmatch(r'[0-9a-f]{40}', args.source), 'source must be a full Git SHA')
    require(args.run_id > 0, 'run ID must be positive')
    if args.command == 'guard':
        require_merged_source(args.source)
        require_unused_tag(args.tag)
        receipt = current_ci(args.source, args.run_id)
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt, indent=2))
    else:
        publish(args.tag, args.source, args.run_id, args.workspace, args.receipt)


if __name__ == '__main__':
    main()
