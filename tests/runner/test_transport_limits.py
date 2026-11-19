"""Regression probes for the release audit: bounded IO, deadlines and cleanup."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from auditlab.runner import make_env
from auditlab.runner.episode import run_episode
from auditlab.runner.isolation import (
    MAX_ACTION_BYTES, PolicyFailure, PolicyTimeout, SubprocessPolicy, child_env,
)

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "fixtures" / "foundation" / "S02"


class TransportRegressions(unittest.TestCase):
    def policy(self, script, **kwargs):
        policy = SubprocessPolicy([sys.executable, "-c", script], **kwargs)
        self.addCleanup(policy.close)
        return policy

    def test_deadline_precedes_malformed_input(self):
        clock = [0.0]
        env = make_env("closed", evaluator="inprocess", clock=lambda: clock[0])
        self.addCleanup(env.close)
        env.reset(0, {"snapshot_dir": str(SNAPSHOT), "time_limit_s": .1})
        clock[0] = 1000
        reply = env.step(None)
        self.assertEqual((reply[2], reply[3]), (False, True))
        self.assertEqual(env.final["status"], "truncated_timeout")
        for _ in range(100):
            self.assertTrue(env.step(None)[3])
        self.assertEqual(len(env._trajectory), 1)

    def test_nonreading_policy_cannot_block_observation_write(self):
        policy = self.policy("import time; time.sleep(2)", timeout_s=.1)
        started = time.monotonic()
        with self.assertRaises(PolicyTimeout):
            policy.act({"document": "x" * 1_000_000})
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIsNotNone(policy._proc.poll())

    def test_unsolicited_flood_is_rejected_without_unbounded_queue(self):
        policy = self.policy("import sys; sys.stdout.write('null\\n' * 20000); sys.stdout.flush()")
        policy._reader.join(timeout=5)
        self.assertFalse(policy._reader.is_alive())
        self.assertIn("unsolicited", policy._failure)
        self.assertLessEqual(policy._lines.qsize(), 1)
        with self.assertRaises(PolicyFailure):
            policy.act({})

    def test_one_oversized_line_is_rejected_without_newline(self):
        policy = self.policy(f"import sys; sys.stdout.write('x' * {MAX_ACTION_BYTES + 1}); sys.stdout.flush()")
        policy._reader.join(timeout=5)
        self.assertFalse(policy._reader.is_alive())
        self.assertIn("byte limit", policy._failure)
        self.assertLessEqual(policy._lines.qsize(), 1)

    def test_reset_error_closes_started_policy(self):
        policy = self.policy("import time; time.sleep(20)")
        env = make_env("closed", evaluator="inprocess")
        self.addCleanup(env.close)
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(FileNotFoundError):
                run_episode(env, policy, 0, {"snapshot_dir": empty})
        self.assertIsNotNone(policy._proc.poll())
        self.assertFalse(Path(policy._scratch.name).exists())

    def test_unsafe_development_transport_is_not_a_filesystem_sandbox(self):
        # Preserve the original canary counterexample; never interpret clean env/cwd as ISO-01.
        with tempfile.TemporaryDirectory() as trusted:
            canary = Path(trusted) / "truth.json"
            canary.write_text('{"canary":"SYNTHETIC_ONLY"}', encoding="utf-8")
            script = ("import sys,json\nfrom pathlib import Path\n"
                      "for line in sys.stdin:\n"
                      " p=Path(json.loads(line)['path'])\n"
                      " value=json.loads(p.read_text())\n"
                      " p.with_name('write-probe').write_text('synthetic audit only')\n"
                      " print(json.dumps(value),flush=True)\n")
            policy = self.policy(script, env=child_env())
            self.assertEqual(policy.act({"path": str(canary)})["canary"], "SYNTHETIC_ONLY")
            self.assertTrue(canary.with_name("write-probe").exists())

    def test_closed_cli_refuses_unsafe_backend(self):
        process = subprocess.run([sys.executable, "-m", "auditlab.runner", "policy", "--name", "reconcile",
                                  "--snapshot", str(SNAPSHOT), "--mode", "closed", "--unsafe-dev-subprocess"],
                                 cwd=ROOT, capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 2)
        self.assertIn(b"allowed only with --mode train", process.stderr)


if __name__ == "__main__":
    unittest.main()
