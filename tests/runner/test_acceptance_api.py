"""API-01/03 and documentary API-05 interoperability with real components.

These checks do not claim a formal JSON Schema exists: Observation is currently
published as dict[str, object]. The external adapter checks documented fields.
"""
import ast
from concurrent.futures import ThreadPoolExecutor
import copy
import csv
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

from auditlab.runner import make_env
from auditlab.runner.episode import run_episode
from auditlab.runner.isolation import SubprocessPolicy

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'fixtures' / 'foundation'
GOLDEN = Path(__file__).parent / 'golden'
ADAPTER = Path(__file__).with_name('stdlib_adapter.py')


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


class AcceptanceApi(unittest.TestCase):
    def test_api03_submit_steps_1_99_100_real_session_and_evaluator(self):
        for label in ('S01', 'S02'):
            submission = load(GOLDEN / (label + '-reconcile') / 'submission.json')
            for step in (1, 99, 100):
                with self.subTest(episode=label, submit_step=step):
                    env = make_env('train')  # real PublicSession and separate evaluator process
                    try:
                        env.reset(0, {'snapshot_dir': str(FIXTURES / label)})
                        for inspection in range(step - 1):
                            result = env.step({'action': 'get_document', 'args': {
                                'doc_id': 'D9' if inspection % 2 else 'UNKNOWN'}})
                            self.assertEqual(result[2:4], (False, False))
                            self.assertEqual(result[0]['remaining_steps'], 99 - inspection)
                        result = env.step({'action': 'submit', 'args': submission})
                        self.assertEqual(result[2:4], (True, False))
                        self.assertEqual(result[0]['remaining_steps'], 100 - step)
                        self.assertAlmostEqual(result[1], 1 - .002 * (step - 1), places=12)
                        self.assertEqual(env.final['inspection_steps'], step - 1)
                        self.assertEqual(len(env._trajectory), step)
                        frozen = copy.deepcopy(env.final)
                        self.assertEqual(env.step({'action': 'submit', 'args': submission})[4],
                                         {'protocol_error': 'episode_finished'})
                        self.assertEqual(env.final, frozen)
                        self.assertEqual(len(env._trajectory), step)
                    finally:
                        env.close()

    def test_api01_concurrent_real_environments_and_reset_do_not_cross(self):
        barrier = threading.Barrier(2, timeout=10)
        with tempfile.TemporaryDirectory() as temporary:
            def worker(label):
                env = make_env('train', trusted_dir=str(Path(temporary) / label))
                try:
                    observation, _ = env.reset(0, {'snapshot_dir': str(FIXTURES / label), 'run_id': 'first'})
                    self.assertEqual(observation['remaining_steps'], 100)
                    with (FIXTURES / label / 'public/ledger.csv').open(encoding='utf-8') as stream:
                        expected = next(row for row in csv.DictReader(stream) if row['row_id'] == 'R7')['timestamp']
                    budgets = []
                    for index in range(4):
                        barrier.wait()
                        if label == 'S01' and index == 2:
                            reset, _ = env.reset(0, {'snapshot_dir': str(FIXTURES / label), 'run_id': 'second'})
                            self.assertEqual(reset['remaining_steps'], 100)
                        result = env.step({'action': 'get_record', 'args': {'table': 'ledger', 'id': 'R7'}})
                        self.assertEqual(result[0]['result']['timestamp'], expected)
                        budgets.append(result[0]['remaining_steps'])
                        barrier.wait()
                    result = env.step({'action': 'submit', 'args': {'episode_id': label, 'findings': []}})
                    self.assertEqual(result[2:4], (True, False))
                    return budgets, result[1]
                finally:
                    env.close()
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(worker, label) for label in ('S01', 'S02')]
                a, b = [future.result(timeout=20) for future in futures]
            self.assertEqual(a[0], [99, 98, 99, 98])
            self.assertAlmostEqual(a[1], .996)
            self.assertEqual(b, ([99, 98, 97, 96], -1.0))
            abandoned = load(Path(temporary) / 'S01/first/report.json')
            self.assertEqual(abandoned['status'], 'abandoned')
            self.assertEqual(load(Path(temporary) / 'S02/first/report.json')['status'], 'submitted')

    def test_api05_external_stdlib_adapter_all_actions_and_golden_files(self):
        tree = ast.parse(ADAPTER.read_text(encoding='utf-8'))
        imported = {alias.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                    for alias in node.names}
        imported |= {node.module.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertEqual(imported, {'json', 'sys', 'decimal'})
        for label in ('S01', 'S02'):
            with self.subTest(episode=label):
                env = make_env('train')
                policy = SubprocessPolicy([sys.executable, '-I', str(ADAPTER)])
                try:
                    result = run_episode(env, policy, 0, {'snapshot_dir': str(FIXTURES / label)})
                    self.assertEqual(result[2:4], (True, False), env.final)
                    self.assertEqual(env.final['status'], 'submitted')
                    self.assertEqual({r['action']['action'] for r in env._trajectory},
                                     {'list_records', 'get_record', 'get_document', 'calculator', 'submit'})
                    # Read committed golden JSON directly, without loading team policy/oracle code.
                    golden = GOLDEN / (label + '-reconcile')
                    expected = load(golden / 'submission.json')['findings']
                    actual = env.final['submission']['findings']
                    self.assertEqual(len(actual), len(expected))
                    for a, b in zip(actual, expected):
                        self.assertEqual({k: v for k, v in a.items() if k != 'explanation'},
                                         {k: v for k, v in b.items() if k != 'explanation'})
                    records = [json.loads(line) for line in (golden / 'trajectories.jsonl').read_text().splitlines()]
                    payment_golden = next(r for r in records if r['action'] == env._trajectory[0]['action'])
                    self.assertEqual(env._trajectory[0]['observation']['result'], payment_golden['observation']['result'])
                    self.assertEqual(env._trajectory[0]['observation']['remaining_steps'], 99)
                    self.assertAlmostEqual(result[1], 1 - .002 * env.final['inspection_steps'])
                    for row in env._trajectory:
                        self.assertEqual(set(row), {'episode_id', 'step', 'action', 'observation',
                                                    'reward', 'terminated', 'truncated'})
                        json.dumps(row, allow_nan=False)
                finally:
                    policy.close()
                    env.close()


if __name__ == '__main__':
    unittest.main()
