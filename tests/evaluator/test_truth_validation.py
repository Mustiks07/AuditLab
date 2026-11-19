"""Malformed references are generator/infrastructure errors, never auditor FP/FN.

D1-D4 counterexamples from the external release review; SPEC §9.3 identity is
(type, canonical affected_ids), independent of amount/currency/evidence.
"""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from auditlab.evaluator.matcher import TruthError, evaluate, validate_truth

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'fixtures' / 'foundation' / 'S02'


def valid_truth():
    return json.loads((FIXTURE / 'private/truth.json').read_text(encoding='utf-8'))


class TruthValidation(unittest.TestCase):
    def assert_rejected(self, truth):
        before = copy.deepcopy(truth)
        with self.assertRaises(TruthError):
            validate_truth(truth)
        with self.assertRaises(TruthError):
            evaluate({'episode_id': 'S02', 'findings': []}, truth, 0)
        self.assertEqual(truth, before, 'validation must not normalize malformed closed input in place')

    def test_d1_same_cause_cannot_be_split_by_amount_currency_or_evidence(self):
        for change in ({'expected_amount_minor': 2000001}, {'currency': 'USD'},
                       {'evidence_sets': [['ledger/R7']]}, {'expected_amount_minor': 0, 'currency': 'JPY'}):
            with self.subTest(change=change):
                truth = valid_truth()
                duplicate = copy.deepcopy(truth['issues'][0])
                duplicate.update(change, issue_id='X2')
                duplicate['affected_ids'].reverse()
                truth['issues'].append(duplicate)
                self.assert_rejected(truth)

    def test_d2_evidence_sets_must_be_satisfiable_with_ten_ids(self):
        truth = valid_truth()
        truth['issues'][0]['evidence_sets'] = [[f'ledger/R{i}' for i in range(11)]]
        self.assert_rejected(truth)
        truth['issues'][0]['evidence_sets'] = [[f'ledger/R{i}' for i in range(10)]]
        validate_truth(truth)

    def test_d3_string_amount_is_rejected_before_matching_arithmetic(self):
        truth = valid_truth()
        truth['issues'][0]['expected_amount_minor'] = '2000000'
        submission = json.loads((ROOT / 'tests/runner/golden/S02-reconcile/submission.json').read_text())
        with self.assertRaises(TruthError):
            evaluate(submission, truth, 0)  # previously validation passed, then subtraction raised TypeError

    def test_d4_strict_amount_types(self):
        for amount in (True, False, 2000000.0, -1, None, [], {}):
            with self.subTest(amount=amount):
                truth = valid_truth()
                truth['issues'][0]['expected_amount_minor'] = amount
                self.assert_rejected(truth)
        for amount in (0, 2**70):
            truth = valid_truth()
            truth['issues'][0]['expected_amount_minor'] = amount
            validate_truth(truth)

    def test_d4_issue_ids_types_entities_and_currency(self):
        for field, values in {
            'issue_id': [None, '', 1, [], {}],
            'type': ['unknown', '', None, [], {}],
            'affected_ids': ['invoice/I4', [], [None], [''], [['invoice/I4']], {}],
            'currency': [None, '', 1, [], {}],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    truth = valid_truth()
                    truth['issues'][0][field] = value
                    self.assert_rejected(truth)

    def test_d4_evidence_requires_nested_nonempty_string_lists(self):
        for evidence in ('ledger/R4', ['ledger/R4'], [], [[]], [[None]], [['']], [[1]], {}, [None]):
            with self.subTest(evidence=evidence):
                truth = valid_truth()
                truth['issues'][0]['evidence_sets'] = evidence
                self.assert_rejected(truth)

    def test_missing_fields_and_duplicate_issue_ids_are_rejected(self):
        for field in valid_truth()['issues'][0]:
            truth = valid_truth()
            del truth['issues'][0][field]
            self.assert_rejected(truth)
        for issue in (None, [], 'issue', 1):
            truth = valid_truth()
            truth['issues'] = [issue]
            self.assert_rejected(truth)
        truth = valid_truth()
        another = copy.deepcopy(truth['issues'][0])
        another['affected_ids'] = ['payment/DIFFERENT']
        truth['issues'].append(another)
        self.assert_rejected(truth)

    def test_distinct_types_or_entities_remain_valid_and_input_unchanged(self):
        truth = valid_truth()
        other_type = copy.deepcopy(truth['issues'][0])
        other_type.update(issue_id='X2', type='fx_conversion')
        other_entity = copy.deepcopy(truth['issues'][0])
        other_entity.update(issue_id='X3', affected_ids=['payment/P10', 'invoice/I5'])
        truth['issues'].extend([other_type, other_entity])
        before = copy.deepcopy(truth)
        validate_truth(truth)
        self.assertEqual(truth, before)

    def test_runner_rejects_malformed_truth_before_an_observation(self):
        from auditlab.runner import make_env
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary) / 'episode'
            shutil.copytree(FIXTURE, snapshot)
            truth = valid_truth()
            truth['issues'][0]['expected_amount_minor'] = '2000000'
            (snapshot / 'private/truth.json').write_text(json.dumps(truth), encoding='utf-8')
            env = make_env('closed')
            try:
                with self.assertRaises(TruthError):
                    env.reset(0, {'snapshot_dir': str(snapshot)})
                self.assertIsNone(env.final)
                result = env.step({'action': 'get_document', 'args': {'doc_id': 'D9'}})
                self.assertEqual(result[0]['error']['code'], 'no_active_episode')
            finally:
                env.close()


if __name__ == '__main__':
    unittest.main()
