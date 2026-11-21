"""Reduction keeps the accounting proof and re-runs a public auditor on the rewrite."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from auditlab.generator import generate_episode
from auditlab.policies import make_policy
from auditlab.release.rehearsal import file_hashes
from auditlab.release.transforms import read_json
from auditlab.research.curriculum.reduce import (
    FailureTarget,
    ReductionBudget,
    ReductionCandidate,
    reduce,
    snapshot_sha256,
)
from auditlab.runner import make_env
from auditlab.runner.episode import run_episode
from tools.check_source_books import check_source_book


class MeasuredReductionTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.snapshot = self.root / "original"
        self.output = self.root / "reduction"
        generate_episode(40871, {
            "config_version": "1.1",
            "issue_counts": {"payment_allocation": 1},
            "clean_distractors": ["refund", "equal_amount_different_supplier"],
            "company_size": {"ledger_rows_target": 200, "documents_target": 60},
        }, self.snapshot)
        self.truth = read_json(self.snapshot / "private" / "truth.json")
        issue, = self.truth["issues"]
        self.target = FailureTarget("false_negative", issue["type"], issue["affected_ids"], issue["issue_id"])
        self.failure = {"reason": self.target.reason, "type": self.target.issue_type,
                        "affected_ids": list(self.target.affected_ids), "issue_id": self.target.issue_id}

    def candidate(self, **budget):
        return ReductionCandidate(self.snapshot, self.target, "test-sha", "local-test", ReductionBudget(**budget))

    def test_fresh_public_policy_runs_keep_clean_book_transition_and_target(self):
        original = file_hashes(self.snapshot)
        validations, observed = set(), []

        def validate(snapshot, output):
            self.assertFalse(output.exists())
            financial = check_source_book(snapshot)
            witness = self.run_auditor("reconcile", snapshot, 1, 60.0)
            score = witness["graded"]["score"]
            self.assertEqual((score["fn"], score["fp"]), (0, 0))
            self.assertLessEqual(witness["inspection_steps"] + 1, 100)
            validations.add(snapshot_sha256(snapshot))
            return {"status": financial["status"], "financial": financial, "witness_score": score}

        def investigate(snapshot, output, remaining):
            public_hash = snapshot_sha256(snapshot)
            self.assertIn(public_hash, validations)
            self.assertFalse(output.exists())
            seed = int(output.name[-2:])
            actual = self.run_auditor("flag_all", snapshot, seed, remaining, trusted_dir=output)
            observed.append((public_hash, actual["submission"]["episode_id"]))
            matched = {issue for _, issue in actual["graded"]["matches"]}
            failures = [self.failure] if self.target.issue_id not in matched else []
            artifact, = output.glob("*/trajectory.jsonl")
            return {"status": actual["status"], "attempt_id": artifact.parent.name,
                    "snapshot_sha256": public_hash, "failures": failures, "trajectory": str(artifact),
                    "cost_micros": 0}

        result = reduce(self.candidate(), investigate, validate, self.output)
        self.assertEqual(result["status"], "reduced")
        self.assertLess(result["selected_sizes"]["ledger_rows"], result["baseline"]["sizes"]["ledger_rows"])
        self.assertLess(result["selected_sizes"]["documents"], result["baseline"]["sizes"]["documents"])
        reduced = Path(result["selected_snapshot"])
        self.assertEqual(read_json(reduced / "private" / "truth.json")["issues"], self.truth["issues"])
        self.assertEqual(check_source_book(reduced)["status"], "passed")
        self.assertEqual(file_hashes(self.snapshot), original)
        self.assertEqual(result, read_json(self.output / "reduction.json"))
        self.assertEqual(len(observed), result["runs_used"])
        self.assertEqual({episode for digest, episode in observed if digest == result["selected_snapshot_sha256"]},
                         {read_json(reduced / "public" / "manifest.json")["episode_id"]})
        self.assertNotEqual(result["baseline"]["snapshot_sha256"], result["selected_snapshot_sha256"])
        self.assertEqual(result["baseline"]["frequency"]["matched"], 3)
        self.assertEqual(result["cost"]["cost_micros"], 0)
        accepted = [item for item in result["proposals"] if item["accepted"]]
        self.assertTrue(accepted)
        self.assertTrue(all(item["frequency"]["matched"] == 3 for item in accepted))

    def test_replayed_baseline_artifact_cannot_accept_smaller_snapshot(self):
        baseline = []

        def investigate(snapshot, output, remaining):
            if snapshot == self.snapshot:
                baseline.append(self.receipt(snapshot, len(baseline)))
            return baseline[(int(output.name[-2:]) - 1)]

        result = reduce(self.candidate(max_candidates=1), investigate, self.stub_validation, self.output)
        self.assertEqual((result["status"], result["reason"]), ("unchanged", "candidate_budget"))
        proposal, = result["proposals"]
        self.assertEqual(proposal["reason"], "insufficient_assessed_repetitions")
        self.assertTrue(all(item["reason"] == "missing_or_reused_attempt_id" for item in proposal["attempts"]))
        self.assertEqual(result["selected_snapshot"], str(self.snapshot))

    def test_different_failure_reason_and_frequency_are_both_rejected(self):
        attempt_count = 0

        def investigate(snapshot, output, remaining):
            nonlocal attempt_count
            attempt_count += 1
            result = self.receipt(snapshot, attempt_count)
            if snapshot != self.snapshot and output.name.endswith("-03"):
                result["failures"][0]["reason"] = "unavailable_evidence"
            return result

        result = reduce(self.candidate(max_candidates=1), investigate, self.stub_validation, self.output)
        proposal, = result["proposals"]
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(proposal["reason"], "observed_frequency_below_baseline")
        self.assertEqual(proposal["frequency"]["matched"], 2)
        self.assertEqual(proposal["frequency"]["attempted"], 3)
        self.assertEqual(proposal["attempts"][-1]["reason"], "different_outcome")

    def test_invalid_financial_rewrite_does_not_spend_any_agent_runs(self):
        def validate(snapshot, output):
            return {"status": "passed"} if snapshot == self.snapshot else {"status": "failed", "reason": "missing proof"}

        def investigate(snapshot, output, remaining):
            self.assertEqual(snapshot, self.snapshot)
            return self.receipt(snapshot, output.name)

        result = reduce(self.candidate(max_candidates=1), investigate, validate, self.output)
        self.assertEqual(result["runs_used"], 3)
        self.assertEqual(result["selected_snapshot"], str(self.snapshot))
        self.assertEqual(result["proposals"][0]["reason"], "validation_not_passed")
        self.assertEqual(result["proposals"][0]["validation"]["reason"], "missing proof")

    def test_run_budget_reserves_a_full_batch_and_keeps_baseline(self):
        def investigate(snapshot, output, remaining):
            return self.receipt(snapshot, output.name)

        result = reduce(self.candidate(max_runs=5), investigate, self.stub_validation, self.output)
        self.assertEqual((result["status"], result["reason"]), ("unchanged", "run_budget"))
        self.assertEqual((result["runs_used"], result["proposals"]), (3, []))

    def test_provider_errors_are_not_counted_as_financial_failures(self):
        result = reduce(self.candidate(), lambda *_: {"status": "timeout"},
                        self.stub_validation, self.output)
        baseline = result["baseline"]
        self.assertEqual((result["status"], result["reason"]), ("not_checked", "insufficient_assessed_repetitions"))
        self.assertEqual(baseline["frequency"]["assessed"], 0)
        self.assertIsNone(baseline["frequency"]["per_assessed"])

    def test_cost_budget_reserves_a_full_batch_before_starting_another_candidate(self):
        def investigate(snapshot, output, remaining):
            return {**self.receipt(snapshot, output.name), "cost_micros": 2}

        result = reduce(self.candidate(cost_budget_micros=14, max_attempt_cost_micros=3), investigate,
                        self.stub_validation, self.output)
        self.assertEqual((result["status"], result["reason"]), ("unchanged", "cost_budget"))
        self.assertEqual((result["runs_used"], result["proposals"]), (3, []))
        self.assertEqual(result["cost"]["cost_micros"], 6)

    def test_unreported_cost_stops_a_cost_limited_run(self):
        result = reduce(self.candidate(cost_budget_micros=10, max_attempt_cost_micros=3),
                        lambda snapshot, *_: self.receipt(snapshot, 1), self.stub_validation, self.output)
        self.assertEqual((result["status"], result["reason"]), ("not_checked", "unreported_attempt_cost"))
        self.assertEqual(result["runs_used"], 1)
        self.assertIsNone(result["cost"]["cost_micros"])

    def receipt(self, snapshot, attempt):
        return {"status": "submitted", "attempt_id": f"test-{attempt}", "snapshot_sha256": snapshot_sha256(snapshot),
                "failures": [dict(self.failure)]}

    @staticmethod
    def stub_validation(*args):
        return {"status": "passed"}

    @staticmethod
    def run_auditor(name, snapshot, seed, remaining, **options):
        env = make_env("closed", evaluator="inprocess", **options)
        try:
            run_episode(env, make_policy(name, seed), seed, {
                "snapshot_dir": str(snapshot), "policy_name": name, "time_limit_s": min(remaining, 60.0),
            })
            return env.final
        finally:
            env.close()


if __name__ == "__main__":
    unittest.main()
