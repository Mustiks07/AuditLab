"""Regressions against the real simulator (PR #8): generator, PublicSession, policies.

Skipped with "не проверено" while those modules are absent; a broken import fails.
Covers the three integrator blockers on PR #9: flat reset config, generator output directory,
export published directly in export_dir with status=completed.
"""
import hashlib
import importlib
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from auditlab.runner import RunnerError, make_env

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "foundation"
CONFIG = {"config_version": "1.0", "company_size": {"ledger_rows_target": 400, "documents_target": 65},
          "base_currency": "KZT", "currencies": ["KZT"], "timezone": "Asia/Almaty", "target_period": "2026-11",
          "issue_counts": {"duplicate_invoice": 0, "payment_allocation": 1, "fx_conversion": 0, "period_cutoff": 0},
          "interaction": None, "clean_distractors": ["split_payment"]}
FINDING = {"type": "payment_allocation", "affected_ids": ["payment/P9", "invoice/I4"],
           "claimed_amount_minor": 2_000_000, "currency": "KZT",
           "evidence_ids": ["payments/D9", "ledger/R4", "ledger/R5", "policy/allocation"],
           "explanation": "50 000 − 30 000 = 20 000 ₸ на исключающей границе."}


def require(module, name):
    if importlib.util.find_spec(module) is None:
        raise unittest.SkipTest(f"не проверено: отсутствует {module}.{name}")
    return getattr(importlib.import_module(module), name)


def hashes(path):
    return {str(f.relative_to(path)): hashlib.sha256(f.read_bytes()).hexdigest() for f in path.rglob("*") if f.is_file()}


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


class RealSimulator(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="auditlab-real-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_control_pair_with_real_session_flat_config_and_direct_export(self):
        require("auditlab.environment", "open_session")
        for label in ("S01", "S02"):
            with self.subTest(label=label):
                snapshot = self.tmp / label
                shutil.copytree(FIXTURES / label, snapshot)
                run_dir = self.tmp / f"run-{label}"
                env = make_env("train")  # default: real open_session, evaluator in its own process
                obs, info = env.reset(19, {"snapshot_dir": str(snapshot), "export_dir": str(run_dir),
                                           "policy_name": "integration-manual"})
                self.assertEqual(obs["remaining_steps"], 100)
                self.assertNotIn("integration-manual", json.dumps([obs, info]))
                for step in range(1, 5):
                    obs, reward, term, trunc, _ = env.step({"action": "get_document", "args": {"doc_id": "D9"}})
                    self.assertTrue(obs["ok"], obs)
                    self.assertEqual((obs["remaining_steps"], reward, term, trunc), (100 - step, 0, False, False))
                submission = {"episode_id": label, "findings": [] if label == "S01" else [FINDING]}
                _, reward, term, trunc, _ = env.step({"action": "submit", "args": submission})
                self.assertEqual((term, trunc), (True, False))
                self.assertAlmostEqual(reward, 0.992, places=12)
                run = load(run_dir / "run.json")
                self.assertEqual((run["status"], run["policy"], run["mode"], run["episode_id"]),
                                 ("completed", "integration-manual", "train", label))
                self.assertTrue(run["run_id"])
                self.assertEqual(load(run_dir / "submission.json"), submission)
                self.assertEqual(len((run_dir / "trajectories.jsonl").read_text(encoding="utf-8").splitlines()), 5)
                self.assertEqual(hashes(run_dir / "public"), hashes(FIXTURES / label / "public"))
                self.assertFalse((run_dir / "private").exists())

    def test_reset_with_generator_config_uses_a_new_directory(self):
        require("auditlab.generator", "generate_episode")
        require("auditlab.environment", "open_session")
        env = make_env("train")
        obs, _ = env.reset(19, dict(CONFIG))  # previously FileExistsError: runner pre-created the target
        self.assertEqual(obs["remaining_steps"], 100)
        self.assertIn("episode_id", obs["manifest"])
        _, reward, term, trunc, _ = env.step({"action": "submit", "args": {
            "episode_id": obs["manifest"]["episode_id"], "findings": []}})
        self.assertEqual((term, trunc, reward), (True, False, -1.0))  # one real error, empty answer
        env.close()

    def test_generated_episode_reconcile_policy_end_to_end(self):
        generate = require("auditlab.generator", "generate_episode")
        require("auditlab.environment", "open_session")
        make_policy = require("auditlab.policies", "make_policy")
        episode = self.tmp / "episode"
        generate(19, dict(CONFIG), episode)
        before = hashes(episode)
        run_dir = self.tmp / "run"
        env = make_env("train")
        obs, _ = env.reset(19, {"snapshot_dir": str(episode), "export_dir": str(run_dir),
                                "policy_name": "reconcile"})
        policy = make_policy("reconcile", 19)
        for _ in range(100):
            obs, reward, term, trunc, info = env.step(policy.act(obs))
            if term or trunc:
                break
        self.assertEqual((term, trunc), (True, False))
        self.assertEqual(info["score"]["tp"], 1)
        self.assertEqual(load(run_dir / "run.json")["status"], "completed")
        self.assertEqual(load(run_dir / "score.json"), info["score"])
        self.assertEqual(before, hashes(episode), "runner must not change the snapshot")

    def test_reconcile_policy_in_separate_process(self):
        require("auditlab.generator", "generate_episode")
        require("auditlab.environment", "open_session")
        require("auditlab.policies", "make_policy")
        from auditlab.runner.episode import run_episode
        from auditlab.runner.isolation import SubprocessPolicy, child_env, policy_command
        snapshot = self.tmp / "S02"
        shutil.copytree(FIXTURES / "S02", snapshot)
        env = make_env("train")
        policy = SubprocessPolicy(policy_command("reconcile", 19), env=child_env())
        result = run_episode(env, policy, 19, {"snapshot_dir": str(snapshot), "policy_name": "reconcile",
                                               "export_dir": str(self.tmp / "iso-run")})
        self.assertEqual((result[2], result[3]), (True, False))
        self.assertEqual(env.final["graded"]["score"]["tp"], 1)
        self.assertEqual(load(self.tmp / "iso-run" / "run.json")["status"], "completed")

    def test_existing_export_dir_is_refused_before_the_episode(self):
        require("auditlab.environment", "open_session")
        taken = self.tmp / "taken"
        taken.mkdir()
        with self.assertRaises(RunnerError):
            make_env("train").reset(0, {"snapshot_dir": str(FIXTURES / "S01"), "export_dir": str(taken)})


if __name__ == "__main__":
    unittest.main()
