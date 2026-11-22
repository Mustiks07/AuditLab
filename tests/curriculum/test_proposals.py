import csv
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from auditlab.generator import generate_episode
from auditlab.research.curriculum.proposals import ProposalAxis, search_proposals
from auditlab.research.curriculum.search import Attempt, Candidate, SearchConfig, Validation


_AMOUNT = ("financial_case", "amount_minor")
_FRACTION = ("financial_case", "applied_bps")


def _root(number=0, *, seed=41300):
    return Candidate(f"train-{number}", f"family-{number}", "train", "allocation", seed, {
        "config_version": "1.2", "issue_counts": {"payment_allocation": 1},
        "financial_case": {"kind": "allocation", "amount_minor": 20000, "applied_bps": 2500, "tranches": 1},
    })


def _settings(roots, **options):
    return replace(SearchConfig(tuple(roots), 4, 8, 2, 14481, "a" * 40, "proposal-test"), **options)


def _passed(candidate, output):
    return Validation("passed", "independent_check_fixture")


def _offers(receipt, arm):
    return [row["proposal"] for row in receipt["arms"][arm]["candidates"]]


class ProposalSearchTests(unittest.TestCase):
    def test_feedback_creates_a_new_one_knob_proposal_at_the_same_budget(self):
        root = _root()
        settings = _settings([root], cost_budget_micros=12, max_attempt_cost_micros=2)
        axes = {root.candidate_id: (ProposalAxis(_AMOUNT, (10000, 20000, 30000)),
                                    ProposalAxis(_FRACTION, (2500, 5000, 7500)))}

        def experiment(output, weakness):
            def run(candidate, repetition, directory):
                return Attempt("completed", weak=weakness, cost_micros=1)

            receipt = search_proposals(settings, run, _passed, output, axes=axes)
            self.assertEqual(json.loads((output / "search.json").read_text(encoding="utf-8")), receipt)
            return receipt

        with TemporaryDirectory() as temporary:
            root_path = Path(temporary)
            weak = experiment(root_path / "weak", True)
            strong = experiment(root_path / "strong", False)
            repeated = experiment(root_path / "repeat", True)

        adaptive = _offers(weak, "adaptive")
        uniform = _offers(weak, "uniform")
        self.assertEqual(adaptive, _offers(repeated, "adaptive"))
        self.assertEqual(weak["plan_sha256"], repeated["plan_sha256"])
        self.assertEqual(uniform, _offers(strong, "uniform"))
        self.assertEqual([row["coordinate"] for row in _offers(strong, "adaptive")],
                         [row["coordinate"] for row in uniform])
        self.assertEqual(adaptive[0]["coordinate"], uniform[0]["coordinate"])
        parent, refined = adaptive[:2]
        self.assertEqual(refined["method"], "refinement")
        self.assertEqual(refined["parent"], parent["candidate"]["candidate_id"])
        before, after = [offer["candidate"]["config"]["financial_case"] for offer in (parent, refined)]
        changed = [name for name in before if before[name] != after[name]]
        self.assertEqual(refined["change"]["path"], ["financial_case", *changed])
        self.assertEqual(len(changed), 1)
        self.assertEqual(refined["change"]["before"], before[changed[0]])
        self.assertEqual(refined["change"]["after"], after[changed[0]])
        self.assertEqual({offer["candidate"]["family_id"] for offer in adaptive}, {root.family_id})
        self.assertEqual({offer["candidate"]["seed"] for offer in adaptive}, {root.seed})
        self.assertEqual(weak["plan"]["proposal_space"]["coordinate_count"], 9)
        for arm in weak["arms"].values():
            self.assertEqual(arm["agent_runs"], 8)
            self.assertEqual(arm["cost_micros"], 8)
            self.assertEqual(arm["offered"], 4)
            self.assertEqual(sum(row["validation_seconds"] for row in arm["candidates"]), arm["validation_seconds"])

    def test_rejected_proposals_are_preserved_before_any_agent_launch(self):
        root = _root()
        settings = _settings([root])
        axis = ProposalAxis(_AMOUNT, (10000, 20000, 30000, 40000))
        validated: dict[Path, int] = {}

        def validate(candidate, output):
            amount = candidate.config["financial_case"]["amount_minor"]
            validated[output.parent] = amount
            return Validation("failed", "generation_rejected_proposed_terms") if amount == 30000 else _passed(candidate, output)

        def run(candidate, repetition, output):
            self.assertIn(output.parent, validated)
            self.assertNotEqual(validated[output.parent], 30000)
            return Attempt("completed", weak=True, cost_micros=0)

        with TemporaryDirectory() as temporary:
            receipt = search_proposals(settings, run, validate, Path(temporary) / "run", axes={root.candidate_id: (axis,)})

        self.assertEqual(len(validated), 8)
        for arm in receipt["arms"].values():
            rejected, = [row for row in arm["candidates"] if row["status"] == "rejected"]
            self.assertEqual(rejected["reason"], "generation_rejected_proposed_terms")
            self.assertEqual(rejected["proposal"]["candidate"]["config"]["financial_case"]["amount_minor"], 30000)
            self.assertEqual(rejected["attempts"], [])
            self.assertGreaterEqual(rejected["validation_seconds"], 0)
            self.assertEqual(arm["agent_runs"], 6)
            self.assertEqual(arm["cost_micros"], 0)

    def test_registry_and_invalid_axes_are_rejected_before_creating_artifacts(self):
        root = _root()
        settings = _settings([root])
        axis = ProposalAxis(_AMOUNT, (20000, 30000))
        default_axes = {root.candidate_id: (axis,)}
        cases = [
            ({"holdout_families": {root.family_id}}, "frozen holdout"),
            ({"holdout_seeds": {root.seed}}, "frozen holdout"),
            ({"axes": {"unknown-root": (axis,)}}, "known training roots"),
            ({"axes": {root.candidate_id: (ProposalAxis(_AMOUNT, (30000, 40000)),)}}, "must include"),
            ({"axes": {root.candidate_id: (axis, axis)}}, "must not overlap"),
        ]

        def never(*args):
            self.fail("an invalid registration must not enter a callback")

        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "run"
            for options, expected in cases:
                with self.subTest(options=options), self.assertRaisesRegex(ValueError, expected):
                    search_proposals(settings, never, never, output, **({"axes": default_axes} | options))
                self.assertFalse(output.exists())

    def test_materializes_only_offered_financial_terms_with_the_generator(self):
        root = _root(seed=41321)
        settings = _settings([root], candidate_budget=2, attempt_budget=2, repetitions=1)
        axis = ProposalAxis(_AMOUNT, tuple(range(10000, 60000, 10000)))
        materialized = {}

        def validate(candidate, output):
            snapshot = output.parent / "snapshot"
            generate_episode(candidate.seed, candidate.config, snapshot)
            amount = candidate.config["financial_case"]["amount_minor"]
            payments = [json.loads(line) for line in (snapshot / "public/payments.jsonl").read_text("utf-8").splitlines()]
            payment, = [row for row in payments if row["amount_minor"] == amount]
            with (snapshot / "public/ledger.csv").open(encoding="utf-8", newline="") as stream:
                posted = sum(int(row["credit_minor"]) for row in csv.DictReader(stream)
                             if row["payment_id"] == payment["payment_id"] and row["account"] == "AR")
            self.assertEqual(posted, amount // 4)
            materialized[output.parent] = snapshot
            return _passed(candidate, output)

        def run(candidate, repetition, output):
            self.assertTrue((materialized[output.parent] / "public/ledger.csv").is_file())
            return Attempt("completed", weak=True, cost_micros=0)

        with TemporaryDirectory() as temporary:
            receipt = search_proposals(settings, run, validate, Path(temporary) / "run", axes={root.candidate_id: (axis,)})
        self.assertEqual(receipt["plan"]["proposal_space"]["coordinate_count"], 5)
        self.assertEqual(len(materialized), 4)
        self.assertEqual(_offers(receipt, "adaptive")[1]["method"], "refinement")


if __name__ == "__main__":
    unittest.main()
