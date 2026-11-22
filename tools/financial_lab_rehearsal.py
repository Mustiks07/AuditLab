"""Evidence for the financial laboratory screens: related situations, error dossiers and the results map.

Input is the trusted output of ``tools/research_rehearsal.py`` (its ``search`` directory). The script
1) publishes every open train/dev candidate and each paired financial variant as console candidates;
2) runs the built-in local policies on exactly those public companies through the ordinary experiment
   runner (real runner, real evaluator, no network, no model calls);
3) publishes the attempts and the independent comparison of these policies.

Sealed partitions are never read. Local policies are not AI models; the console labels them as such.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from auditlab.research.console_export import publish, publish_families, read, write  # noqa: E402
from auditlab.research.agents import experiment  # noqa: E402
from review.agent_generalization.compare import compare_experiment  # noqa: E402

ARMS = {'empty': 'Пустой ответ · встроенная политика', 'random': 'Случайные выводы · встроенная политика',
        'flag_all': 'Обвинить всё · встроенная политика', 'reconcile': 'Сверка по документам · встроенная политика'}


def episodes(search: Path) -> list[dict]:
    exp = read(search/'experiment.json')
    members = {m['candidate_id']: m for m in exp['partition']['members']}
    rows = []
    for receipt in sorted((search/'candidates').glob('*/candidate.json')):
        key = read(receipt)['candidate']['candidate_id']
        member = members.get(key)
        if member and member['split'] in ('train', 'dev'):
            rows.append({'episode_ref': str(receipt.parent/'snapshot'), 'family_id': member['family_id'], 'split': member['split']})
    for pair in exp.get('paired') or []:
        member = members[pair['parent']]
        rows.append({'episode_ref': str(search/'pairs'/pair['parent']/'snapshots'/pair['kind']),
                     'family_id': member['family_id'], 'split': member['split']})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--search', type=Path, required=True, help='search directory of research_rehearsal.py')
    parser.add_argument('--output', type=Path, required=True, help='new directory')
    args = parser.parse_args()
    search, output = args.search.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    families = publish_families(search, output/'console-families')
    plan = {'schema_version': '1.0', 'artifact_id': 'financial-lab-local-policies', 'protocol_id': 'genesis-official-60s',
            'purpose': 'selection', 'transport': 'in_process', 'repetitions': 1,
            'title': 'Встроенные политики на связанных финансовых ситуациях',
            'episodes': episodes(search),
            'arms': [{'name': name, 'label': label, 'agent': {'kind': 'baseline_policy', 'policy': name}}
                     for name, label in ARMS.items()],
            'analysis': {'pairs': [['reconcile', 'random']]}}
    write(output/'plan.json', plan)
    plan, digest = experiment.load_plan(output/'plan.json')
    summary = experiment.run_experiment(plan, digest, output/'runs')
    experiment.export_for_review(output/'runs', output/'independent-review', plan)
    comparison = compare_experiment(output/'independent-review')
    write(output/'comparison.json', comparison)
    publish(plan, output/'runs', output/'console-runs', comparison=comparison)
    receipt = {'families': families, 'attempts': summary['attempts_total'], 'network_calls': 0, 'model_calls': 0}
    write(output/'receipt.json', receipt)
    print(json.dumps(receipt, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
