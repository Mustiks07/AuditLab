"""Hostile policies inside the real release sandbox (the gaps found by jury-compatibility-v3).

Opt in after loading the release image:  AUDITLAB_POLICY_IMAGE=<id> python -m unittest tests/runner/test_hostile_sandbox.py

Existing tests already cover canary files and network (test_docker_boundary) and protocol, oversized
lines and deadlines on the development SubprocessPolicy (test_transport_limits, test_isolation_export).
These tests run the same attacks through DockerPolicy — the production lifecycle, sandbox flags and
cleanup — with only the entry point replaced by the attacker's code. A skip is not a pass.
"""
import json
import os
from pathlib import Path
import subprocess
import time
import unittest
from unittest.mock import patch

from auditlab.runner import make_env
from auditlab.runner import isolation
from auditlab.runner.episode import run_episode

IMAGE = os.environ.get("AUDITLAB_POLICY_IMAGE")
SNAPSHOT = Path(__file__).resolve().parents[2] / "fixtures" / "foundation" / "S02"
REAL_COMMAND = isolation.docker_command

PROLOGUE = "import json, os, subprocess, sys, time\nobservation = sys.stdin.readline()\n"
SUBMIT = ("print(json.dumps({'action': 'submit', 'args': {'episode_id': 'S02', 'findings': []}}), flush=True)\n"
          "sys.stdin.readline()\n")
ATTACKS = {
    # Spawn far more processes than --pids-limit allows, plus a detached grandchild that tries to outlive
    # the episode. The count of successful spawns is reported through a calculator expression.
    "children": PROLOGUE + (
        "if os.fork() == 0:\n"
        "    os.setsid(); os.execvp('sleep', ['sleep', '600'])\n"
        "alive = []\n"
        "for _ in range(400):\n"
        "    try: alive.append(subprocess.Popen(['sleep', '300']))\n"
        "    except OSError: break\n"
        "print(json.dumps({'action': 'calculator', 'args': {'expression': str(len(alive))}}), flush=True)\n"
        "sys.stdin.readline()\n") + SUBMIT,
    "memory": PROLOGUE + "hog = []\nwhile True: hog.append(bytearray(64 * 1024 * 1024))\n",
    "hang": PROLOGUE + "time.sleep(600)\n",
    "flood": PROLOGUE + "sys.stdout.write('x' * (16 * 1024 * 1024)); sys.stdout.flush(); time.sleep(600)\n",
}


def attacker(script):
    def command(image, **kwargs):
        base = REAL_COMMAND(image, **kwargs)
        at = base.index(image)
        return base[:at] + ["--entrypoint", "python", image, "-c", script]
    return command


def container_exists(name):
    return subprocess.run(["docker", "ps", "--all", "--quiet", "--filter", f"name=^{name}$"],
                          capture_output=True, text=True, timeout=10).stdout.strip() != ""


@unittest.skipUnless(IMAGE, "не проверено: set AUDITLAB_POLICY_IMAGE to the preloaded release image")
class HostileSandbox(unittest.TestCase):
    def run_attack(self, name, *, memory="1536m", time_limit_s=20.0):
        actions = []
        with patch.object(isolation, "docker_command", attacker(ATTACKS[name])):
            policy = isolation.DockerPolicy(IMAGE, "attacker", 0, memory=memory, timeout_s=time_limit_s)
        self.addCleanup(policy.close)
        act = policy.act
        def recording(observation):
            action = act(observation)
            actions.append(action)
            return action
        policy.act = recording
        env = make_env("closed", evaluator="inprocess")
        self.addCleanup(env.close)
        started = time.monotonic()
        run_episode(env, policy, 0, {"snapshot_dir": str(SNAPSHOT), "time_limit_s": time_limit_s})
        elapsed = time.monotonic() - started
        policy.close()
        return env.final, actions, elapsed, policy.container_name

    def test_process_spawning_is_bounded_and_nothing_outlives_the_episode(self):
        final, actions, _, name = self.run_attack("children")
        spawned = int(next(a["args"]["expression"] for a in actions if a["action"] == "calculator"))
        self.assertLess(spawned, 128, "--pids-limit 128 must stop the fork storm")
        self.assertEqual(final["status"], "submitted")
        self.assertFalse(container_exists(name), "the container and every process in it are removed")
        leftovers = subprocess.run(["pgrep", "-u", "65534", "-f", "sleep (300|600)"], capture_output=True, text=True)
        self.assertEqual(leftovers.stdout.strip(), "", "no attacker process survives on the host")

    def test_memory_exhaustion_is_a_policy_failure_not_a_score(self):
        final, _, elapsed, name = self.run_attack("memory", memory="256m")
        self.assertEqual(final["status"], "policy_failure")
        self.assertIsNone(final.get("score"))
        self.assertLess(elapsed, 20)
        self.assertFalse(container_exists(name))

    def test_hung_container_is_stopped_at_the_deadline(self):
        final, _, elapsed, name = self.run_attack("hang", time_limit_s=3.0)
        self.assertEqual((final["status"], final["inspection_steps"]), ("truncated_timeout", 0))
        self.assertLess(elapsed, 15)
        self.assertFalse(container_exists(name))

    def test_output_flood_through_the_docker_client_is_rejected(self):
        final, _, elapsed, name = self.run_attack("flood", time_limit_s=10.0)
        self.assertEqual(final["status"], "policy_failure")
        self.assertLess(elapsed, 15)
        self.assertFalse(container_exists(name))


if __name__ == "__main__":
    unittest.main()
