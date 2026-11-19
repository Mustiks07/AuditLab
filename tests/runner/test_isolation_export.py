"""Policy-process isolation, abort handling, atomic write-once export."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from auditlab.runner import RunnerError, make_env
from auditlab.runner.episode import OraclePolicy, run_episode
from auditlab.runner.isolation import SubprocessPolicy, child_env, docker_command
from auditlab.runner.testing import FixturePublicSession

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "foundation"
SNAP = {"snapshot_dir": str(FIXTURES / "S02"), "policy_name": "scripted"}

# A child that tries to find secrets: reports env, cwd listing and attempts to read the closed truth.
PROBE = r"""
import json, os, sys
for line in sys.stdin:
    probe = {"env": sorted(os.environ), "cwd_files": os.listdir("."), "secret": os.environ.get("AUDITLAB_TEST_SECRET")}
    try:
        open(sys.argv[1], encoding="utf-8").read(); probe["read_truth"] = True
    except OSError:
        probe["read_truth"] = False
    open("probe.json", "w").write(json.dumps(probe))
    print(json.dumps({"action": "submit", "args": {"episode_id": "S02", "findings": []}}))
    sys.stdout.flush()
"""


class Isolation(unittest.TestCase):
    def test_child_gets_clean_env_and_empty_cwd(self):
        os.environ["AUDITLAB_TEST_SECRET"] = "top-secret"
        try:
            policy = SubprocessPolicy([sys.executable, "-c", PROBE, str(FIXTURES / "S02/private/truth.json")])
        finally:
            del os.environ["AUDITLAB_TEST_SECRET"]
        env = make_env("closed", session_opener=FixturePublicSession, evaluator="inprocess")
        scratch = Path(policy._scratch.name)
        result = run_episode(env, policy, 0, SNAP)
        self.assertTrue(result[2])  # terminated: submitted normally
        # The scratch dir is deleted on close; re-run to capture the probe before cleanup.
        policy = SubprocessPolicy([sys.executable, "-c", PROBE, "nonexistent"])
        policy.act({"x": 1})
        probe = json.loads((Path(policy._scratch.name) / "probe.json").read_text(encoding="utf-8"))
        policy.close()
        self.assertIsNone(probe["secret"])
        self.assertNotIn("AUDITLAB_TEST_SECRET", probe["env"])
        self.assertEqual(probe["cwd_files"], [])
        self.assertFalse(scratch.exists())

    def test_crash_is_policy_failure_not_a_score(self):
        with tempfile.TemporaryDirectory() as trusted, tempfile.TemporaryDirectory() as exports:
            env = make_env("train", session_opener=FixturePublicSession, evaluator="inprocess", trusted_dir=trusted)
            policy = SubprocessPolicy([sys.executable, "-c", "import sys; sys.exit(3)"])
            run_dir = Path(exports) / "crash"
            result = run_episode(env, policy, 0, {**SNAP, "export_dir": str(run_dir)})
            self.assertEqual((result[2], result[3], result[4]), (False, True, {"status": "policy_failure"}))
            self.assertEqual(env.final["status"], "policy_failure")
            run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual((run["status"], run["outcome"]), ("failed", "policy_failure"))  # never "completed"
            report = json.loads(next(Path(trusted).iterdir()).joinpath("report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "policy_failure")
            self.assertIn("abort", [e["event"] for e in report["events"]])

    def test_hung_policy_is_killed_at_deadline(self):
        env = make_env("closed", session_opener=FixturePublicSession, evaluator="inprocess")
        policy = SubprocessPolicy([sys.executable, "-c", "import time; time.sleep(30)"])
        cfg = {**SNAP, "time_limit_s": 0.5}
        result = run_episode(env, policy, 0, cfg)
        self.assertTrue(result[3])
        self.assertEqual(env.final["status"], "truncated_timeout")  # SPEC §8, not a crash
        self.assertEqual(env.final["inspection_steps"], 0)
        self.assertEqual(env.final["reward"], -1.0)  # S02 has an error, empty answer
        self.assertIsNotNone(policy._proc.poll())

    def test_hang_after_some_inspections_counts_accepted_ones(self):
        script = chr(10).join([
            "import sys, json, time",
            "for i, l in enumerate(sys.stdin):",
            "    if i == 3: time.sleep(30)",
            "    print(json.dumps({'action': 'get_document', 'args': {'doc_id': 'NOPE' if i else 'D9'}}))",
            "    sys.stdout.flush()"])
        env = make_env("train", session_opener=FixturePublicSession, evaluator="inprocess")
        policy = SubprocessPolicy([sys.executable, "-c", script])
        result = run_episode(env, policy, 0, {"snapshot_dir": str(FIXTURES / "S01"), "time_limit_s": 1.5})
        self.assertEqual((result[3], env.final["status"], env.final["inspection_steps"]),
                         (True, "truncated_timeout", 3))
        self.assertAlmostEqual(result[1], 1 - 0.006, places=12)

    def test_policy_host_runs_a_factory_over_stdio(self):
        env = make_env("train", session_opener=FixturePublicSession, evaluator="inprocess")
        cmd = [sys.executable, "-m", "auditlab.runner.policy_host", "--factory", "auditlab_test_policy:make", "0"]
        policy = SubprocessPolicy(cmd, env=child_env({"PYTHONPATH": child_env()["PYTHONPATH"] + os.pathsep
                                                      + str(Path(__file__).parent)}))
        result = run_episode(env, policy, 0, {"snapshot_dir": str(FIXTURES / "S01")})
        self.assertEqual((result[2], env.final["status"], env.final["inspection_steps"]), (True, "submitted", 2))
        self.assertAlmostEqual(result[1], 0.996, places=12)

    def test_garbage_output_is_policy_failure(self):
        env = make_env("closed", session_opener=FixturePublicSession, evaluator="inprocess")
        policy = SubprocessPolicy([sys.executable, "-c", "import sys\nfor l in sys.stdin: print('not json'); sys.stdout.flush()"])
        run_episode(env, policy, 0, SNAP)
        self.assertEqual(env.final["status"], "policy_failure")

    def test_docker_profile_has_no_network_volumes_or_caps(self):
        cmd = docker_command("auditlab-policy:test")
        self.assertIn("--network", cmd)
        self.assertEqual(cmd[cmd.index("--network") + 1], "none")
        self.assertIn("--read-only", cmd)
        self.assertEqual(cmd[cmd.index("--cap-drop") + 1], "ALL")
        self.assertNotIn("-v", cmd)
        self.assertNotIn("--volume", cmd)
        self.assertNotIn("--privileged", cmd)
        self.assertEqual(cmd[-1], "auditlab-policy:test")

    def test_in_process_exception_is_also_policy_failure(self):
        class Boom:
            def act(self, obs):
                raise ValueError("bug")

        env = make_env("closed", session_opener=FixturePublicSession, evaluator="inprocess")
        run_episode(env, Boom(), 0, SNAP)
        self.assertEqual(env.final["status"], "policy_failure")


class Export(unittest.TestCase):
    def test_public_export_layout_and_write_once(self):
        with tempfile.TemporaryDirectory() as exports:
            env = make_env("train", session_opener=FixturePublicSession, evaluator="inprocess")
            run = Path(exports) / "nested" / "S02-oracle"  # parent may not exist yet
            cfg = {**SNAP, "run_id": "run-1", "policy_name": "oracle", "export_dir": str(run)}
            run_episode(env, OraclePolicy(FIXTURES / "S02", inspections=4), 0, cfg)
            self.assertEqual(sorted(p.name for p in run.iterdir()),
                             ["public", "run.json", "score.json", "submission.json", "trajectories.jsonl"])
            self.assertEqual(json.loads((run / "run.json").read_text(encoding="utf-8")),
                             {"run_id": "run-1", "episode_id": "S02", "policy": "oracle", "mode": "train",
                              "status": "completed", "outcome": "submitted", "inspection_steps": 4})
            score = json.loads((run / "score.json").read_text(encoding="utf-8"))
            self.assertAlmostEqual(score["reward"], 0.992, places=12)
            self.assertEqual(sorted(p.name for p in (run / "public").iterdir()),
                             sorted(p.name for p in (FIXTURES / "S02/public").iterdir()))
            rows = [json.loads(l) for l in (run / "trajectories.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([r["step"] for r in rows], [1, 2, 3, 4, 5])
            self.assertEqual({"episode_id", "step", "action", "observation", "reward", "terminated", "truncated"},
                             set(rows[0]))
            self.assertAlmostEqual(rows[-1]["reward"], 0.992, places=12)
            self.assertTrue(rows[-1]["terminated"])
            before = {p.name: p.read_bytes() for p in run.iterdir() if p.is_file()}
            # An existing export directory is refused up front and never overwritten.
            env2 = make_env("train", session_opener=FixturePublicSession, evaluator="inprocess")
            with self.assertRaises(RunnerError):
                env2.reset(0, cfg)
            self.assertEqual({p.name: p.read_bytes() for p in run.iterdir() if p.is_file()}, before)
            self.assertEqual([p.name for p in run.parent.iterdir()], ["S02-oracle"], "no staging leftovers")

    def test_export_files_are_ascii_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as exports:
            env = make_env("train", session_opener=FixturePublicSession, evaluator="inprocess")
            run = Path(exports) / "r"
            env.reset(0, {**SNAP, "export_dir": str(run)})
            submission = {"episode_id": "S02", "findings": [{
                "type": "payment_allocation", "affected_ids": ["payment/P9", "invoice/I4"],
                "claimed_amount_minor": 2_000_000, "currency": "KZT",
                "evidence_ids": ["payments/D9", "ledger/R4", "ledger/R5", "policy/allocation"],
                "explanation": "50 000 − 30 000 = 20 000 ₸ на исключающей границе."}]}
            env.step({"action": "submit", "args": submission})
            for path in run.glob("*.json*"):
                path.read_bytes().decode("ascii")  # readable with any default encoding
            self.assertEqual(json.loads((run / "submission.json").read_text(encoding="ascii")), submission)

    def test_truncated_runs_are_exported_but_never_completed(self):
        with tempfile.TemporaryDirectory() as exports:
            env = make_env("train", session_opener=FixturePublicSession, evaluator="inprocess")

            class Spin:
                def act(self, obs):
                    return {"action": "get_document", "args": {"doc_id": "D9"}}

            run_dir = Path(exports) / "spin"
            run_episode(env, Spin(), 0, {**SNAP, "run_id": "spin", "export_dir": str(run_dir)})
            run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual((run["status"], run["outcome"]), ("truncated", "truncated_budget"))
            rows = (run_dir / "trajectories.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 100)
            self.assertTrue(json.loads(rows[-1])["truncated"])

    def test_trajectory_is_exactly_what_step_returned(self):
        with tempfile.TemporaryDirectory() as exports:
            env = make_env("train", session_opener=FixturePublicSession, evaluator="inprocess")
            run_dir = Path(exports) / "exact"
            obs, _ = env.reset(3, {**SNAP, "export_dir": str(run_dir)})
            returned = []
            actions = [{"action": "get_document", "args": {"doc_id": "D9"}},
                       {"action": "nope", "args": {}},
                       {"action": "submit", "args": {"episode_id": "S02", "findings": []}}]
            for step, action in enumerate(actions, start=1):
                obs, reward, term, trunc, _ = env.step(action)
                returned.append({"episode_id": "S02", "step": step, "action": action, "observation": obs,
                                 "reward": reward, "terminated": term, "truncated": trunc})
                action["args"]["mutated_after"] = True  # must not leak into the record
            for row in returned:
                row["action"]["args"].pop("mutated_after")
            rows = [json.loads(l) for l in (run_dir / "trajectories.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows, returned)


if __name__ == "__main__":
    unittest.main()
