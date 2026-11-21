"""The artifact checks must catch the defects they exist for (synthetic test doubles, clearly not results)."""
import sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from review.agent_generalization import artifact_checks as ac  # noqa: E402

TRUTH = {"episode_id": "E", "issues": [{"issue_id": "A", "type": "payment_allocation", "affected_ids": ["payment/P1", "invoice/I1"],
                                         "expected_amount_minor": 10000, "currency": "KZT", "evidence_sets": [["payments/DP1", "ledger/R4"]]}]}
GOOD = {"type": "payment_allocation", "affected_ids": ["payment/P1", "invoice/I1"], "claimed_amount_minor": 10000, "currency": "KZT",
        "evidence_ids": ["payments/DP1", "ledger/R4"], "explanation": "x"}


def attempt(i, findings, status="completed"):
    return {"attempt_id": f"a{i}", "status": status, "final_answer": {"episode_id": "E", "findings": findings}, "inspection_steps": 5}


class FailureAndReduction(unittest.TestCase):
    def test_persistent_failure_needs_two_of_three(self):
        atts = [attempt(0, []), attempt(1, []), attempt(2, [GOOD])]
        self.assertEqual(ac.check_failure({"kind": "FN"}, atts, TRUTH)["status"], "passed")
        self.assertEqual(ac.check_failure({"kind": "FN"}, [attempt(0, []), attempt(1, [GOOD]), attempt(2, [GOOD])], TRUTH)["status"], "failed")

    def test_cherry_picked_repetitions_are_detected(self):
        atts = [attempt(0, []), attempt(1, [])]
        r = ac.check_failure({"kind": "FN", "all_attempt_ids": ["a0", "a1", "a2"]}, atts, TRUTH)
        self.assertEqual(r["status"], "failed"); self.assertEqual(r["omitted_attempts"], ["a2"])

    def test_wrong_amount_is_classified(self):
        self.assertEqual(ac.failure_kinds(attempt(0, [{**GOOD, "claimed_amount_minor": 9999}]), TRUTH), {"wrong_amount_currency_or_entities"})
        self.assertEqual(ac.failure_kinds(attempt(0, [{**GOOD, "evidence_ids": ["payments/DP1"]}]), TRUTH), {"incomplete_evidence"})
        self.assertEqual(ac.failure_kinds(attempt(0, [GOOD]), {"episode_id": "E", "issues": []}), {"FP_on_clean"})
        self.assertEqual(ac.failure_kinds(attempt(0, [], "timeout"), TRUTH), {"timeout"})

    def test_reduction_must_keep_the_defect_and_the_failure(self):
        before = [attempt(i, []) for i in range(3)]
        after_ok = [attempt(i, []) for i in range(3)]
        after_gone = [attempt(i, [GOOD]) for i in range(3)]
        self.assertEqual(ac.check_reduction({"failure_kind": "FN"}, {"status": "passed"}, before, after_ok, TRUTH, TRUTH)["status"], "passed")
        self.assertEqual(ac.check_reduction({"failure_kind": "FN"}, {"status": "passed"}, before, after_gone, TRUTH, TRUTH)["status"], "failed")
        self.assertEqual(ac.check_reduction({"failure_kind": "FN"}, {"status": "failed"}, before, after_ok, TRUTH, TRUTH)["status"], "failed")


class CurriculumTrainingClaims(unittest.TestCase):
    def test_renamed_copies_and_holdout_leaks(self):
        r = ac.check_curriculum({"x1": "h1", "x2": "h1", "x3": "h3"}, {"x3": {"c9"}}, {"h3"}, {"c9"})
        self.assertEqual(r["status"], "failed"); self.assertEqual(r["renamed_or_identical_copies"], 1)
        self.assertEqual(r["examples_sharing_whole_history_with_dev_or_holdout"], ["x3"])

    def test_prompt_change_is_not_weight_training(self):
        self.assertEqual(ac.check_training({"kind": "prompt", "claimed_as": "training"})["status"], "failed")
        self.assertEqual(ac.check_training({"kind": "few_shot"})["status"], "not_checked")
        self.assertEqual(ac.check_training({"kind": "weights", "job_id": "j", "checkpoint_id": "c", "status": "succeeded", "provider": "mock"})["status"], "failed")
        self.assertEqual(ac.check_training({"kind": "weights", "job_id": "j", "checkpoint_id": "c", "status": "succeeded", "provider": "x"})["status"], "passed")

    def test_claims(self):
        bad = ac.check_claims({"ui": "Official score: 0.93. Improvement proven on holdout."})
        self.assertEqual({h["rule"] for h in bad["hits"]}, {"official_score", "unproven_improvement"})
        good = ac.check_claims({"ui": "This is not an official score. Improvement not shown: the interval contains 0."})
        self.assertEqual(ac.check_claims({"ui": "Сейчас мы смотрим запись, не живой запуск."})["status"], "passed")
        self.assertEqual(ac.check_claims({"ui": "Это живой запуск модели."})["status"], "failed")
        self.assertEqual(good["status"], "passed")


if __name__ == "__main__":
    unittest.main()
