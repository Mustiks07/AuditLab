import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from auditlab.research.curriculum.search import Attempt, Candidate, SearchConfig, Validation, search


def _candidate(number, stratum="one", split="train"):
    return Candidate(f"candidate-{number:02d}", f"family-{number:02d}", split, stratum, number, {})


def _config(candidates, **overrides):
    return replace(SearchConfig(tuple(candidates), 48, 48, 1, 2037, "a" * 40, "local-test"), **overrides)


def _passed(candidate, output):
    return Validation("passed", "verified")


class SearchTests(unittest.TestCase):
    def test_seeded_stratified_adaptation_and_uniform_have_equal_budgets(self):
        pool = [_candidate(index, "weak" if index % 2 else "strong") for index in range(64)]
        config = _config(pool, candidate_budget=14, attempt_budget=14)

        def run(candidate, repetition, output):
            return Attempt("completed", weak=candidate.stratum == "weak", cost_micros=2)

        with tempfile.TemporaryDirectory() as root:
            first = search(config, run, _passed, Path(root) / "first")
            permuted = replace(config, candidates=tuple(reversed(pool)))
            second = search(permuted, run, _passed, Path(root) / "second")
        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        self.assertEqual(first["arms"], second["arms"])
        adaptive = first["arms"]["adaptive"]
        uniform = first["arms"]["uniform"]
        self.assertEqual(adaptive["agent_runs"], 14)
        self.assertEqual(uniform["agent_runs"], 14)
        self.assertEqual(adaptive["cost_micros"], uniform["cost_micros"])
        self.assertGreater(adaptive["accepted"], uniform["accepted"])
        for arm in (adaptive, uniform):
            self.assertEqual(len({row["candidate_id"] for row in arm["candidates"]}), 14)

    def test_validates_before_run_and_preserves_all_outcomes(self):
        config = _config([_candidate(number) for number in range(5)], repetitions=2, min_weak_repetitions=2)

        def validate(candidate, output):
            if candidate.seed == 0:
                return Validation("failed", "missing_document")
            if candidate.seed == 1:
                return Validation("not_checked", "validator_offline")
            return _passed(candidate, output)

        def run(candidate, repetition, output):
            self.assertGreater(candidate.seed, 1)
            if candidate.seed == 2 and repetition == 2:
                return Attempt("timeout", reason="response_deadline")
            weak = candidate.seed == 4 or candidate.seed == 3 and repetition == 1
            return Attempt("completed", weak=weak, cost_micros=2)

        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "test"
            receipt = search(config, run, validate, output)
            self.assertEqual(json.loads((output / "search.json").read_text(encoding="utf-8")), receipt)

        for arm in receipt["arms"].values():
            outcomes = {row["candidate_id"]: row for row in arm["candidates"]}
            self.assertEqual(outcomes["candidate-00"]["reason"], "missing_document")
            self.assertEqual(outcomes["candidate-00"]["attempts"], [])
            self.assertEqual(outcomes["candidate-01"]["status"], "not_checked")
            self.assertEqual(outcomes["candidate-02"]["status"], "not_checked")
            self.assertEqual(outcomes["candidate-02"]["attempts"][1]["reason"], "response_deadline")
            self.assertIsNone(outcomes["candidate-02"]["attempts"][1]["weak"])
            self.assertEqual(outcomes["candidate-03"]["reason"], "weakness_threshold_not_met")
            self.assertEqual(outcomes["candidate-04"]["status"], "accepted")
            self.assertEqual(arm["agent_runs"], 6)
            self.assertEqual(arm["scored_attempts"], 5)
            self.assertIsNone(arm["cost_micros"])
            self.assertEqual(arm["known_cost_micros"], 10)

    def test_reserves_full_repetition_cohort_before_calling_runner(self):
        config = _config(
            [_candidate(number) for number in range(5)],
            repetitions=2,
            cost_budget_micros=15,
            max_attempt_cost_micros=5,
        )

        def run(candidate, repetition, output):
            return Attempt("completed", weak=True, cost_micros=3)

        with tempfile.TemporaryDirectory() as root:
            receipt = search(config, run, _passed, Path(root) / "test")
        for arm in receipt["arms"].values():
            self.assertEqual(arm["stop_reason"], "cost_budget")
            self.assertEqual(arm["agent_runs"], 2)
            self.assertEqual(arm["cost_micros"], 6)

    def test_runner_spend_contract_stops_both_arms_on_unreported_cost(self):
        config = _config([_candidate(1)], cost_budget_micros=5, max_attempt_cost_micros=5)

        def run(candidate, repetition, output):
            return Attempt("provider_error", reason="missing_usage", launches=None)

        with tempfile.TemporaryDirectory() as root:
            receipt = search(config, run, _passed, Path(root) / "test")
        self.assertEqual(receipt["status"], "stopped_cost_contract")
        self.assertIsNone(receipt["arms"]["adaptive"]["agent_runs"])
        self.assertEqual(receipt["arms"]["adaptive"]["stop_reason"], "unreported_attempt_cost")
        self.assertEqual(receipt["arms"]["uniform"]["attempt_callbacks"], 0)

    def test_holds_out_dev_and_test_before_any_callbacks_and_checks_attempt_cap(self):
        for split in ("dev", "test"):
            with self.subTest(split=split), self.assertRaisesRegex(ValueError, "only train"):
                _config([_candidate(1), _candidate(2, split=split)])
        config = _config([_candidate(1), _candidate(2)], candidate_budget=2, attempt_budget=3, repetitions=2)

        def run(candidate, repetition, output):
            return Attempt("completed", weak=False, cost_micros=0)

        with tempfile.TemporaryDirectory() as root:
            receipt = search(config, run, _passed, Path(root) / "test")
        for arm in receipt["arms"].values():
            self.assertEqual(arm["agent_runs"], 2)
            self.assertEqual(arm["stop_reason"], "attempt_budget")

    def test_interrupted_attempt_is_retained_and_not_replayed(self):
        def run(candidate, repetition, output):
            raise KeyboardInterrupt

        config = _config([_candidate(1)])
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "interrupted"
            with self.assertRaises(KeyboardInterrupt):
                search(config, run, _passed, output)
            receipt = json.loads((output / "search.json").read_text(encoding="utf-8"))
            with self.assertRaises(FileExistsError):
                search(config, run, _passed, output)
        arm = receipt["arms"]["adaptive"]
        self.assertTrue(arm["attempt_in_progress"])
        self.assertIsNone(arm["agent_runs"])
        self.assertIsNone(arm["cost_micros"])
        self.assertEqual(arm["candidates"][0]["attempts"][0]["status"], "in_progress")


if __name__ == "__main__":
    unittest.main()
