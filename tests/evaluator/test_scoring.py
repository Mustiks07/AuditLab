"""MAT-01..04 on the foundation pair and synthetic truths. Run: python -m unittest discover -s tests/evaluator -v"""
import copy
import json
import unittest
from pathlib import Path

from auditlab.evaluator import (TruthError, evaluate, oracle_submission, reward_from_counts,
                                score_submission, validate_submission, SubmissionError)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "foundation"


def truth_of(name):
    return json.loads((FIXTURES / name / "private" / "truth.json").read_text(encoding="utf-8"))


def finding(**override):
    base = {"type": "payment_allocation", "affected_ids": ["payment/P9", "invoice/I4"],
            "claimed_amount_minor": 2_000_000, "currency": "KZT",
            "evidence_ids": ["payments/D9", "ledger/R4", "ledger/R5", "policy/allocation"],
            "explanation": "x"}
    base.update(override)
    return base


def sub(episode, *findings):
    return {"episode_id": episode, "findings": list(findings)}


class SectionNineTable(unittest.TestCase):
    def check(self, submission, truth, steps, counts, reward):
        score = score_submission(submission, truth, steps)
        self.assertEqual((score["tp"], score["fp"], score["fn"]), counts)
        self.assertAlmostEqual(score["reward"], reward, places=12)
        return score

    def test_clean_empty(self):
        score = self.check(sub("S01"), truth_of("S01"), 0, (0, 0, 0), 1.0)
        self.assertEqual(score["f1"], 1.0)

    def test_clean_false_accusation(self):
        score = self.check(sub("S01", finding()), truth_of("S01"), 3, (0, 1, 0), -1.0)
        self.assertEqual(score["f1"], 0.0)

    def test_one_error_empty(self):
        self.check(sub("S02"), truth_of("S02"), 4, (0, 0, 1), -1.0)

    def test_one_error_full_finding(self):
        self.check(sub("S02", finding()), truth_of("S02"), 4, (1, 0, 0), 0.992)

    def test_duplicate_is_false_positive(self):
        score = self.check(sub("S02", finding(), finding(explanation="again")), truth_of("S02"), 4,
                           (1, 1, 0), 2 / 3 * 2 - 1 - 0.008)
        self.assertAlmostEqual(score["reward"], 0.325333333333, places=9)

    def test_permuted_duplicate_is_false_positive(self):
        other = finding(affected_ids=["invoice/I4", "payment/P9", "payment/P9"],
                        evidence_ids=["policy/allocation", "ledger/R5", "ledger/R4", "payments/D9", "ledger/R4"])
        self.check(sub("S02", finding(), other), truth_of("S02"), 4, (1, 1, 0), 0.3253333333333)

    def test_incomplete_evidence(self):
        self.check(sub("S02", finding(evidence_ids=["payments/D9", "policy/allocation"])), truth_of("S02"), 4,
                   (0, 1, 1), -1.0)

    def test_foreign_document_replaces_required_evidence(self):
        wrong = finding(evidence_ids=["payments/D10", "ledger/R4", "ledger/R5", "policy/allocation"])
        self.check(sub("S02", wrong), truth_of("S02"), 4, (0, 1, 1), -1.0)

    def test_repeated_evidence_does_not_strengthen(self):
        wrong = finding(evidence_ids=["payments/D9", "payments/D9", "ledger/R4", "ledger/R4", "policy/allocation"])
        self.check(sub("S02", wrong), truth_of("S02"), 4, (0, 1, 1), -1.0)

    def test_extra_evidence_is_harmless(self):
        extra = finding(evidence_ids=["payments/D9", "ledger/R4", "ledger/R5", "policy/allocation", "ledger/R1"])
        self.check(sub("S02", extra), truth_of("S02"), 4, (1, 0, 0), 0.992)

    def test_alternative_complete_set(self):
        truth = truth_of("S02")
        truth["issues"][0]["evidence_sets"].append(["payments/D9", "ledger/R7", "policy/allocation"])
        alt = finding(evidence_ids=["ledger/R7", "payments/D9", "policy/allocation"])
        self.check(sub("S02", alt), truth, 4, (1, 0, 0), 0.992)

    def test_mat04_costs(self):
        self.check(sub("S02", finding()), truth_of("S02"), 5, (1, 0, 0), 0.990)
        self.check(sub("S01"), truth_of("S01"), 100, (0, 0, 0), 0.8)
        self.check(sub("S02"), truth_of("S02"), 100, (0, 0, 1), -1.0)

    def test_reward_is_exact_not_float_accumulated(self):
        from fractions import Fraction
        self.assertEqual(reward_from_counts(1, 0, 0, 4), Fraction(124, 125))
        self.assertEqual(reward_from_counts(0, 0, 0, 100), Fraction(4, 5))


class SemanticAndStructural(unittest.TestCase):
    def setUp(self):
        self.truth = truth_of("S02")

    def test_one_unit_off_is_fp(self):
        for delta in (-1, 1):
            score = score_submission(sub("S02", finding(claimed_amount_minor=2_000_000 + delta)), self.truth, 4)
            self.assertEqual((score["tp"], score["fp"], score["fn"]), (0, 1, 1))

    def test_wrong_currency_and_lowercase_are_fp(self):
        for cur in ("USD", "kzt"):
            score = score_submission(sub("S02", finding(currency=cur)), self.truth, 4)
            self.assertEqual((score["tp"], score["fp"], score["fn"]), (0, 1, 1))

    def test_wrong_type_or_ids_are_fp(self):
        for override in ({"type": "fx_conversion"}, {"affected_ids": ["payment/P9"]}):
            score = score_submission(sub("S02", finding(**override)), self.truth, 4)
            self.assertEqual((score["tp"], score["fp"], score["fn"]), (0, 1, 1))

    def test_structural_failures_give_minus_one(self):
        bad = [
            {"episode_id": "S02"},
            {"episode_id": "S02", "findings": [], "extra": 1},
            {"episode_id": "OTHER", "findings": []},
            sub("S02", finding(claimed_amount_minor=True)),
            sub("S02", finding(claimed_amount_minor=2e6)),
            sub("S02", finding(claimed_amount_minor="2000000")),
            sub("S02", finding(claimed_amount_minor=-1)),
            sub("S02", finding(type="nonsense")),
            sub("S02", finding(type=["x"])),
            sub("S02", {k: v for k, v in finding().items() if k != "explanation"}),
            sub("S02", dict(finding(), surprise=1)),
            sub("S02", finding(evidence_ids="ledger/R4")),
            "not even an object",
        ]
        for submission in bad:
            with self.subTest(submission=submission):
                result = evaluate(submission, self.truth, 4)
                self.assertEqual((result.status, result.score["reward"]), ("invalid_submission", -1.0))

    def test_size_limits_boundaries(self):
        ok_30 = sub("S01", *[finding(affected_ids=[f"payment/P{i}"]) for i in range(30)])
        score = score_submission(ok_30, truth_of("S01"), 5)
        self.assertEqual((score["fp"], score["reward"]), (30, -1.0))
        self.assertEqual(evaluate(ok_30, truth_of("S01"), 5).status, "scored")
        self.assertEqual(evaluate(sub("S01", *[finding() for _ in range(31)]), truth_of("S01"), 5).status,
                         "invalid_submission")
        self.assertEqual(evaluate(sub("S02", finding(evidence_ids=[f"e/{i}" for i in range(10)])),
                                  self.truth, 1).status, "scored")
        self.assertEqual(evaluate(sub("S02", finding(evidence_ids=[f"e/{i}" for i in range(11)])),
                                  self.truth, 1).status, "invalid_submission")
        self.assertEqual(evaluate(sub("S02", finding(explanation="a" * 1000)), self.truth, 1).status, "scored")
        self.assertEqual(evaluate(sub("S02", finding(explanation="a" * 1001)), self.truth, 1).status,
                         "invalid_submission")

    def test_flag_all_scores_by_quality(self):
        spam = [finding(affected_ids=[f"payment/P{i}"], claimed_amount_minor=i) for i in range(30)]
        score = score_submission(sub("S01", *spam), truth_of("S01"), 10)
        self.assertEqual((score["tp"], score["fp"], score["reward"]), (0, 30, -1.0))

    def test_bad_truth_rejected(self):
        truth = truth_of("S02")
        truth["issues"].append(copy.deepcopy(truth["issues"][0]) | {"issue_id": "X2"})
        with self.assertRaises(TruthError):
            evaluate(sub("S02"), truth, 0)


class MatchingOptimality(unittest.TestCase):
    def two_issue_truth(self):
        def issue(iid, amount, evidence):
            return {"issue_id": iid, "type": "payment_allocation", "affected_ids": ["payment/P1"],
                    "expected_amount_minor": amount, "currency": "KZT", "evidence_sets": evidence}
        return {"episode_id": "T", "issues": [issue("A", 100, [["e1"], ["e2"]]),
                                              issue("B", 101, [["e1"]])]}

    def finding(self, evidence):
        return finding(affected_ids=["payment/P1"], claimed_amount_minor=100, evidence_ids=evidence)

    def test_greedy_first_choice_loses_a_match(self):
        # Greedy would give the first finding (e1) to issue A and strand the second finding (e2),
        # which can only serve A. Maximum matching: e1 -> B, e2 -> A.
        for order in ([["e1"], ["e2"]], [["e2"], ["e1"]]):
            subm = sub("T", *[self.finding(e) for e in order])
            score = evaluate(subm, self.two_issue_truth(), 0, tolerance_minor=1).score
            self.assertEqual((score["tp"], score["fp"], score["fn"]), (2, 0, 0), order)

    def test_permutation_stable(self):
        truth = self.two_issue_truth()
        findings = [self.finding(["e1"]), self.finding(["e2"]), self.finding(["e1", "e2"])]
        results = set()
        import itertools
        for perm in itertools.permutations(findings):
            r = evaluate(sub("T", *perm), truth, 3, tolerance_minor=1)
            results.add((r.score["tp"], r.score["fp"], r.score["fn"], r.score["reward"]))
        self.assertEqual(results, {(2, 1, 0, results.copy().pop()[3])})

    def test_oracle_gets_ceiling(self):
        for name, expected in (("S01", 1.0), ("S02", 1.0)):
            truth = truth_of(name)
            score = score_submission(oracle_submission(truth), truth, 0)
            self.assertEqual((score["fp"], score["fn"], score["reward"]), (0, 0, expected))
        self.assertEqual(validate_submission(oracle_submission(truth_of("S02")), "S02"), None)
        with self.assertRaises(SubmissionError):
            validate_submission(sub("S02", finding(currency=1)), "S02")


if __name__ == "__main__":
    unittest.main()
