"""Real entry points only. Absent modules skip; broken imports fail."""
import copy
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "foundation"


def require(module, name):
    if importlib.util.find_spec(module) is None:
        raise unittest.SkipTest(f"не проверено: отсутствует {module}.{name}")
    # Missing transitive dependencies and missing entry points are regressions, not skips.
    return getattr(importlib.import_module(module), name)


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def snapshot_hashes(path):
    return {str(file.relative_to(path)): hashlib.sha256(file.read_bytes()).hexdigest()
            for file in path.rglob("*") if file.is_file()}


def manual_finding():
    return {"type": "payment_allocation", "affected_ids": ["payment/P9", "invoice/I4"],
            "claimed_amount_minor": 2_000_000, "currency": "KZT",
            "evidence_ids": ["payments/D9", "ledger/R4", "ledger/R5", "policy/allocation"],
            "explanation": "50 000 - 30 000 = 20 000 KZT на исключающей границе."}


class PolicyExecutionError(RuntimeError):
    pass


class ComponentPairTests(unittest.TestCase):
    def test_public_session_reads_pair_evidence(self):
        open_session = require("auditlab.environment", "open_session")
        for label, expected in (("S01", 0), ("S02", 2_000_000)):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                public = Path(temporary) / "public"
                shutil.copytree(FIXTURES / label / "public", public)
                session = open_session(public)
                def inspect(action, **args):
                    reply = session.inspect({"action": action, "args": args})
                    self.assertTrue(reply["ok"], reply)
                    return reply["result"]
                # Known IDs test addressability, not discovery by an ordinary policy.
                payment = inspect("get_document", doc_id="D9")
                inspect("get_record", table="policy", id="allocation")
                balance = 0
                for row_id in payment["settlement_index"]["unapplied_row_ids"]:
                    row = inspect("get_record", table="ledger", id=row_id)
                    balance += int(row["credit_minor"]) - int(row["debit_minor"])
                self.assertEqual(balance, expected)

    def test_evaluator_six_numerical_examples(self):
        score_submission = require("auditlab.evaluator", "score_submission")
        finding = manual_finding()
        incomplete = copy.deepcopy(finding)
        incomplete["evidence_ids"].remove("ledger/R5")
        cases = [("S01", [], 0, (0, 0, 0), 1.0),
                 ("S01", [finding], 3, (0, 1, 0), -1.0),
                 ("S02", [], 4, (0, 0, 1), -1.0),
                 ("S02", [finding], 4, (1, 0, 0), 0.992),
                 ("S02", [finding, finding], 4, (1, 1, 0), 1 / 3 - 0.008),
                 ("S02", [incomplete], 4, (0, 1, 1), -1.0)]
        for label, findings, steps, counts, reward in cases:
            with self.subTest(label=label, counts=counts):
                truth = load(FIXTURES / label / "private" / "truth.json")
                result = score_submission({"episode_id": label, "findings": copy.deepcopy(findings)},
                                          truth, steps)
                self.assertEqual(tuple(result[key] for key in ("tp", "fp", "fn")), counts)
                self.assertAlmostEqual(result["reward"], reward, places=12)

    def test_independent_verifier_public_only_pair(self):
        verify_snapshot = require("auditlab.verifier", "verify_snapshot")
        for label in ("S01", "S02"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                public = Path(temporary) / "public"
                shutil.copytree(FIXTURES / label / "public", public)
                before = snapshot_hashes(public)
                findings = verify_snapshot(public)
                self.assertEqual(before, snapshot_hashes(public))
                self.assertEqual(len(findings), 0 if label == "S01" else 1)
                if findings:
                    expected = manual_finding()
                    for key in ("type", "claimed_amount_minor", "currency"):
                        self.assertEqual(findings[0][key], expected[key])
                    self.assertEqual(set(findings[0]["affected_ids"]), set(expected["affected_ids"]))
                    self.assertTrue(set(expected["evidence_ids"]) <= set(findings[0]["evidence_ids"]))
                    self.assertLessEqual(len(findings[0]["evidence_ids"]), 10)

    def test_runner_control_pair_submit_after_four_inspections(self):
        make_env = require("auditlab.runner", "make_env")
        require("auditlab.environment", "open_session")
        require("auditlab.evaluator", "score_submission")
        for label in ("S01", "S02"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                snapshot = Path(temporary) / "episode"
                exported = Path(temporary) / "export"
                shutil.copytree(FIXTURES / label, snapshot)
                env = make_env("train")
                observation, _ = env.reset(19, {"snapshot_dir": str(snapshot),
                    "export_dir": str(exported), "policy_name": "integration-manual"})
                self.assertEqual(observation["remaining_steps"], 100)
                for step in range(1, 5):
                    observation, reward, terminated, truncated, _ = env.step(
                        {"action": "get_document", "args": {"doc_id": "D9"}})
                    self.assertTrue(observation["ok"])
                    self.assertEqual(observation["remaining_steps"], 100 - step)
                    self.assertEqual((reward, terminated, truncated), (0, False, False))
                submission = {"episode_id": label, "findings": [] if label == "S01" else [manual_finding()]}
                _, reward, terminated, truncated, _ = env.step({"action": "submit", "args": submission})
                self.assertEqual((terminated, truncated), (True, False))
                self.assertAlmostEqual(reward, 0.992, places=12)
                self.assertEqual(load(exported / "submission.json"), submission)
                self.assertEqual(len((exported / "trajectories.jsonl").read_text().splitlines()), 5)

    def test_generated_auditor_score_trajectory_and_verifier(self):
        generate = require("auditlab.generator", "generate_episode")
        require("auditlab.environment", "open_session")
        make_env = require("auditlab.runner", "make_env")
        make_policy = require("auditlab.policies", "make_policy")
        score_submission = require("auditlab.evaluator", "score_submission")
        verify_snapshot = require("auditlab.verifier", "verify_snapshot")
        supplied = os.environ.get("AUDITLAB_CHECK_ARTIFACTS")
        if supplied:
            output = Path(supplied) / "generated-roundtrip"
            output.mkdir()
        else:
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            output = Path(temporary.name)
        for errors in (0, 1):
            with self.subTest(payment_allocation=errors):
                episode = output / f"episode-{errors}"
                exported = output / f"run-{errors}"
                config = {"config_version": "1.0", "company_size": {
                    "ledger_rows_target": 400, "documents_target": 65}, "base_currency": "KZT",
                    "currencies": ["KZT"], "timezone": "Asia/Almaty", "target_period": "2026-11",
                    "issue_counts": {"duplicate_invoice": 0, "payment_allocation": errors,
                                     "fx_conversion": 0, "period_cutoff": 0},
                    "interaction": None, "clean_distractors": ["split_payment"]}
                generate(19, config, episode)
                before = snapshot_hashes(episode)
                public = episode / "public"
                episode_id = load(public / "manifest.json")["episode_id"]
                # Fix independent answer before reading truth. Only public files are copied.
                independent_public = output / f"independent-{errors}" / "public"
                shutil.copytree(public, independent_public)
                independent = verify_snapshot(independent_public)
                (output / f"independent-{errors}.json").write_text(
                    json.dumps(independent, ensure_ascii=False, indent=2), encoding="utf-8")
                env = make_env("train")
                observation, info = env.reset(19, {"snapshot_dir": str(episode),
                    "export_dir": str(exported), "policy_name": "reconcile"})
                self.assertEqual(observation["remaining_steps"], 100)
                policy = make_policy("reconcile", 19)
                actions = []
                actual = []
                for step in range(1, 101):
                    try:
                        action = policy.act(copy.deepcopy(observation))
                    except Exception as error:
                        raise PolicyExecutionError("reconcile.act failed") from error
                    actions.append(copy.deepcopy(action))
                    observation, reward, terminated, truncated, info = env.step(action)
                    actual.append({"episode_id": episode_id, "step": step, "action": action,
                                   "observation": copy.deepcopy(observation), "reward": reward,
                                   "terminated": terminated, "truncated": truncated})
                    if terminated or truncated:
                        break
                self.assertTrue(terminated)
                self.assertFalse(truncated)
                self.assertEqual(actions[-1]["action"], "submit")
                self.assertTrue(all(row["reward"] == 0 for row in actual[:-1]))
                submission = actions[-1]["args"]
                truth = load(episode / "private" / "truth.json")
                self.assertEqual(len(truth["issues"]), errors)
                # Compare independently of the scorer, so a broken scorer cannot certify itself.
                for findings in (submission["findings"], independent):
                    def key(finding, amount):
                        return (finding["type"], tuple(sorted(finding["affected_ids"])),
                                finding[amount], finding["currency"])
                    self.assertCountEqual([key(f, "claimed_amount_minor") for f in findings],
                                          [key(f, "expected_amount_minor") for f in truth["issues"]])
                    for finding in findings:
                        issue = next(t for t in truth["issues"]
                                     if key(t, "expected_amount_minor") == key(finding, "claimed_amount_minor"))
                        self.assertTrue(any(set(group) <= set(finding["evidence_ids"])
                                            for group in issue["evidence_sets"]))
                score = score_submission(submission, truth, len(actions) - 1)
                independent_score = score_submission({"episode_id": episode_id, "findings": independent}, truth, 0)
                for result in (score, independent_score):
                    self.assertEqual((result["tp"], result["fp"], result["fn"]), (errors, 0, 0))
                self.assertAlmostEqual(reward, score["reward"], places=12)
                self.assertEqual(load(exported / "submission.json"), submission)
                self.assertEqual(load(exported / "score.json"), score)
                trajectory = [json.loads(line) for line in (exported / "trajectories.jsonl").read_text().splitlines()]
                self.assertEqual(trajectory, actual)
                metadata = load(exported / "run.json")
                self.assertEqual(metadata["episode_id"], episode_id)
                self.assertEqual(metadata["mode"], "train")
                self.assertEqual(metadata["policy"], "reconcile")
                self.assertEqual(metadata["status"], "completed")
                self.assertTrue(metadata["run_id"])
                self.assertEqual(snapshot_hashes(public), snapshot_hashes(exported / "public"))
                self.assertEqual(before, snapshot_hashes(episode))


if __name__ == "__main__":
    unittest.main()
