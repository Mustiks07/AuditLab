import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from auditlab.evaluator.service import handle
from auditlab.evaluator.schema import RULES_GENESIS
from auditlab.generator import generate_episode
from auditlab.release.transforms import read_json
from auditlab.research.curriculum.objectives import assess_failures, objective_failures, OBJECTIVES
from auditlab.research.curriculum.reduce import FailureTarget
from auditlab.research.curriculum.search import Candidate
from auditlab.research.curriculum.study import _choose_representative


class FinancialObjectiveTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.issue, self.clean = root / "issue", root / "clean"
        common = {"config_version": "1.2", "financial_case": {"kind": "allocation", "amount_minor": 555_517,
                  "applied_bps": 4287, "tranches": 2}, "clean_distractors": ["refund"]}
        generate_episode(54911, {**common, "issue_counts": {"payment_allocation": 1}}, self.issue)
        generate_episode(54911, common, self.clean)
        [truth] = read_json(self.issue / "private/truth.json")["issues"]
        self.finding = {"type": truth["type"], "affected_ids": truth["affected_ids"], "currency": truth["currency"],
                        "claimed_amount_minor": truth["expected_amount_minor"], "evidence_ids": truth["evidence_sets"][0],
                        "explanation": "Test a distinct financial failure against the real evaluator."}

    def objectives(self, snapshot, findings):
        episode = read_json(snapshot / "public/manifest.json")["episode_id"]
        evaluation = handle({"private_dir": str(snapshot / "private"), "inspection_steps": 5, "rules": RULES_GENESIS,
                             "submission": {"episode_id": episode, "findings": findings}})
        self.assertEqual(evaluation["status"], "scored")
        receipt = {"status": "submitted", "failures": assess_failures(snapshot, findings, evaluation)}
        return {goal for goal in OBJECTIVES if objective_failures(receipt, goal)}

    def test_groups_submissions_by_the_actual_failed_financial_condition(self):
        amount = self.finding["claimed_amount_minor"]
        expected = (([], {"missed_issue"}),
                    ([{**self.finding, "claimed_amount_minor": amount + 1}], {"wrong_amount"}),
                    ([{**self.finding, "evidence_ids": []}], {"wrong_evidence"}),
                    ([{**self.finding, "evidence_ids": [*self.finding["evidence_ids"], "ledger/UNKNOWN"]}], {"wrong_evidence"}),
                    ([{**self.finding, "evidence_ids": [], "claimed_amount_minor": amount + 1}], set()),
                    ([self.finding], set()))
        for findings, result in expected:
            with self.subTest(findings=findings):
                self.assertEqual(self.objectives(self.issue, findings), result)
        self.assertEqual(self.objectives(self.clean, [self.finding]), {"clean_false_positive"})

    def test_timeouts_and_rate_limits_cannot_be_selected_even_with_diagnostics(self):
        for status in ("timeout", "provider_error", "rate_limited", "invalid_submission"):
            receipt = {"status": status, "reason": "HTTP 429", "failures": [{"reason": goal} for goal in OBJECTIVES]}
            self.assertFalse(any(objective_failures(receipt, goal) for goal in OBJECTIVES))

    def test_semantically_bad_accusation_on_clean_book_is_still_a_valid_target(self):
        self.assertEqual(self.objectives(self.clean, [{**self.finding, "type": "", "affected_ids": [""]}]),
                         {"clean_false_positive"})
        target = FailureTarget("clean_false_positive", "", ("",))
        self.assertTrue(target.matches({"reason": "clean_false_positive", "type": "", "affected_ids": [""]}))

    def test_export_representative_retains_the_first_stable_refinement(self):
        found = {}
        for name, method in (("first", "exploration"), ("second", "refinement"), ("third", "refinement")):
            candidate = Candidate(name, "family", "train", "allocation", 41, {})
            _choose_representative(found, candidate, {"proposal": {"method": method}}, "target")
        self.assertEqual(found["family"][0].candidate_id, "second")
