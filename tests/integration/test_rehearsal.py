"""Rehearsal evidence: frozen denominator, real pair, cleanup and no unsafe fallback."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from auditlab.release.rehearsal import (execute, independent_answer, inspect_image, pending_plan,
    replay_actions, run_attempt, run_pair, selection_manifest, summarize)
from auditlab.release.transforms import read_json, transform_public, transform_truth

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "foundation" / "S02"


class RehearsalTests(unittest.TestCase):
    def test_selection_has_exact_denominators_and_distinct_inputs(self):
        plan = selection_manifest()
        self.assertEqual(len(plan["episodes"]), 20)
        self.assertEqual(sum(row["category"] == "clean" for row in plan["episodes"]), 5)
        self.assertEqual(sum(row["category"] == "interaction" for row in plan["episodes"]), 5)
        self.assertEqual(len({row["seed"] for row in plan["episodes"]}), 20)
        self.assertEqual({r["config"]["interaction"] for r in plan["episodes"] if r["category"] == "interaction"},
                         {"allocation_fx_same_payment", "cutoff_fx_same_invoice"})
        self.assertTrue(all(sum(row["config"]["issue_counts"].values()) <= 12 for row in plan["episodes"]))
        self.assertEqual(plan, selection_manifest())
        summary = summarize(pending_plan()["episodes"])
        self.assertEqual(summary["random"]["planned"], 20)
        self.assertEqual(summary["random"]["clean"]["planned"], 5)

    def test_failed_attempts_never_improve_mean_by_synthetic_zero_score(self):
        rows = pending_plan()["episodes"]
        a = next(row for row in rows if row["id"] == "reconcile/internal-01")
        a.update(status="passed", metrics={"tp": 0, "fp": 1, "fn": 0, "f1": 0, "reward": -1},
                 inspection_steps=3, outcome="submitted", elapsed_seconds=.2)
        b = next(row for row in rows if row["id"] == "reconcile/internal-02")
        b.update(status="failed", kind="infrastructure_error")
        s = summarize(rows)["reconcile"]
        self.assertEqual((s["planned"], s["measured"], s["failed"], s["not_checked"]), (20, 1, 1, 18))
        self.assertEqual(s["mean_reward"], -1)
        self.assertEqual(s["clean"], {"planned": 5, "measured": 1, "false_accusation_episodes": 1, "rate_over_measured": 1})

    def test_missing_image_never_pulls(self):
        self.assertEqual(inspect_image(None)["status"], "not_checked")
        with patch("auditlab.release.rehearsal.subprocess.run", side_effect=FileNotFoundError("docker")) as call:
            self.assertEqual(inspect_image("missing:local")["status"], "failed")
            self.assertEqual(call.call_args.args[0], ["docker", "image", "inspect", "missing:local"])

    def test_executor_freezes_selection_and_keeps_unrun_attempts(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "attempt"
            selection = selection_manifest()
            selection["episodes"] = [{"id": "internal-01", "category": "single_or_mixed", "seed": 11,
                "pair_seed": 33, "config": {"issue_counts": {"payment_allocation": 1}}}]
            def fixture_generator(seed, config, destination):
                self.assertTrue((output / "selection.json").is_file())
                shutil.copytree(FIXTURE, destination)
            with patch("auditlab.release.rehearsal.selection_manifest", return_value=selection), \
                    patch("auditlab.generator.generate_episode", side_effect=fixture_generator):
                result = execute(output, None)
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(len(result["episodes"]), 80)
            self.assertEqual(len(result["pairs"]), 20)
            self.assertEqual(result["datasets"][0]["status"], "passed")
            self.assertEqual(result["pairs"][0]["status"], "passed")
            self.assertEqual(result["summary"]["oracle"]["measured"], 1)
            self.assertEqual(result["summary"]["reconcile"]["measured"], 0)
            self.assertEqual(read_json(output / "report.json")["status"], "incomplete")

    def test_real_oracle_closed_trajectory_and_transformed_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            selection = {"id": "internal-01", "category": "single_or_mixed", "seed": 931, "pair_seed": 97}
            before = (FIXTURE / "public" / "ledger.csv").read_bytes()
            answer, evidence = independent_answer(FIXTURE / "public", output / "answer", None, None)
            self.assertEqual(evidence["backend"], "host_public_copy_not_os_isolated")
            oracle = run_attempt(FIXTURE, output / "oracle", selection, "oracle", 0, None, None)
            self.assertEqual(oracle["status"], "passed", oracle)
            self.assertEqual(oracle["metrics"]["reward"], 1)
            trajectory = [json.loads(line) for line in (output / "oracle/trusted/attempt/trajectory.jsonl").read_text().splitlines()]
            self.assertTrue(all(row["reward"] == 0 for row in trajectory))
            source = output / "oracle/trusted/attempt/trajectory.jsonl"
            pair = run_pair(FIXTURE, answer, oracle, output / "pair", selection, None, None, source)
            self.assertEqual(pair["status"], "passed", pair)
            self.assertTrue(pair["oracle_score_agrees"])
            self.assertTrue(pair["transformed_answer_agrees"])
            self.assertEqual(pair["replay"]["status"], "passed")
            trajectory[0]["observation"]["remaining_steps"] = 73
            tampered = output / "tampered.jsonl"
            tampered.write_text("\n".join(json.dumps(row) for row in trajectory), encoding="utf-8")
            replay = replay_actions(FIXTURE, tampered, output / "bad-replay", selection,
                                    source_report=source.with_name("report.json"))
            self.assertEqual(replay["status"], "failed")
            self.assertEqual(replay["mismatches"], [1])
            self.assertEqual((FIXTURE / "public" / "ledger.csv").read_bytes(), before)
            with self.assertRaises(FileExistsError):
                run_attempt(FIXTURE, output / "oracle", selection, "oracle", 0, None, None)

    def test_closed_replay_detects_changed_trusted_reward_without_public_change(self):
        from auditlab.runner.env import AuditEnvironment, InProcessEvaluator
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            selection = {"id": "internal-01", "category": "single_or_mixed", "seed": 931}
            original = run_attempt(FIXTURE, output / "oracle", selection, "oracle", 0, None, None)
            self.assertEqual(original["true_reward"], 1)
            source = output / "oracle/trusted/attempt/trajectory.jsonl"
            def defective_evaluator(private, submission, steps):
                result = InProcessEvaluator()(private, submission, steps)
                result["score"]["reward"] = -1
                return result
            def defective_environment(mode, **options):
                return AuditEnvironment(mode, evaluator=defective_evaluator, **options)
            with patch("auditlab.runner.make_env", side_effect=defective_environment):
                replay = replay_actions(FIXTURE, source, output / "replay", selection)
            self.assertEqual(replay["status"], "failed")
            self.assertEqual(replay["mismatches"], ["trusted_report"])
            self.assertEqual(set(replay["trusted_mismatches"]), {"evaluation", "true_reward"})
            self.assertEqual(read_json(output / "replay/replay/report.json")["true_reward"], -1)
            public = [json.loads(line) for line in (output / "replay/replay/trajectory.jsonl").read_text().splitlines()]
            self.assertTrue(all(row["reward"] == 0 for row in public))

    def test_closed_replay_without_original_trusted_report_is_not_certified(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            result = replay_actions(FIXTURE, output / "trajectory.jsonl", output / "replay", {"seed": 931})
            self.assertEqual(result["status"], "not_checked")

    def test_bad_snapshot_preserves_failed_row_without_score(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            row = run_attempt(root / "missing", root / "result", {"id": "internal-01", "seed": 0, "category": "clean"},
                              "oracle", 0, None, None)
            self.assertEqual(row["status"], "failed")
            self.assertIsNone(row["metrics"])
            self.assertEqual(read_json(root / "result/result.json")["status"], "failed")

    def test_transform_maps_all_public_evidence_and_reverses_id_order(self):
        from auditlab.environment import open_session
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "public"
            mapping = transform_public(FIXTURE / "public", target, 113)
            truth = transform_truth(read_json(FIXTURE / "private/truth.json"), mapping)
            self.assertNotEqual(truth["episode_id"], "S02")
            self.assertGreater(mapping["raw_ids"]["row_id"]["R4"], mapping["raw_ids"]["row_id"]["R5"])
            session = open_session(target)
            for issue in truth["issues"]:
                for group in issue["evidence_sets"]:
                    for ref in group:
                        table, key = ref.split("/", 1)
                        action = ({"action": "get_document", "args": {"doc_id": key}}
                                  if table in ("payments", "invoices") else
                                  {"action": "get_record", "args": {"table": table, "id": key}})
                        self.assertTrue(session.inspect(action)["ok"], ref)

    def test_large_transform_has_no_small_id_pool_limit(self):
        from auditlab.generator import generate_episode
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generate_episode(190003, {"company_size": {"ledger_rows_target": 2000, "documents_target": 200}}, root / "source")
            mapping = transform_public(root / "source/public", root / "target/public", 31)
            rows = mapping["raw_ids"]["row_id"]
            self.assertGreaterEqual(len(rows), 2000)
            self.assertEqual(len(rows), len(set(rows.values())))


if __name__ == "__main__":
    unittest.main()
