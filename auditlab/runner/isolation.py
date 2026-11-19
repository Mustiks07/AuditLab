"""Policy isolation: the policy lives in another process and sees only JSON Lines on stdin/stdout.

`SubprocessPolicy` is the host-side proxy. It starts the child with an empty environment, a fresh
empty working directory and no inherited handles. This is process-level isolation; filesystem and
network isolation are provided by the container profile in docker/runtime (see its README).
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import tempfile
import threading
from pathlib import Path

from auditlab.contracts import Action, Observation


class PolicyFailure(RuntimeError):
    """The policy crashed, timed out, or broke the channel. Reported apart from the financial result."""


def scrubbed_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "PATH") if key in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra or {})
    return env


def docker_command(image: str, *, cpus: str = "2", memory: str = "4g", pids: int = 128) -> list[str]:
    """Container profile for an untrusted policy: no network, no volumes, read-only rootfs, no caps."""
    return ["docker", "run", "--rm", "-i", "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", str(pids), "--cpus", cpus,
            "--memory", memory, "--user", "65534:65534", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", image]


class SubprocessPolicy:
    """Implements contracts.Policy over a child process. One instance per episode."""

    def __init__(self, command: list[str], *, env: dict[str, str] | None = None, timeout_s: float = 60.0):
        self._scratch = tempfile.TemporaryDirectory(prefix="auditlab-policy-")
        self.timeout_s = timeout_s
        self._proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                      cwd=self._scratch.name, env=env if env is not None else scrubbed_env(),
                                      close_fds=True)
        self._lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for line in self._proc.stdout:  # type: ignore[union-attr]
            self._lines.put(line)
        self._lines.put(None)

    def set_timeout(self, seconds: float) -> None:
        self.timeout_s = seconds

    def act(self, public_observation: Observation) -> Action:
        try:
            self._proc.stdin.write(json.dumps(public_observation) + "\n")  # type: ignore[union-attr]
            self._proc.stdin.flush()  # type: ignore[union-attr]
        except OSError as error:
            raise PolicyFailure("policy closed its input") from error
        try:
            line = self._lines.get(timeout=max(self.timeout_s, 0.001))
        except queue.Empty:
            self.close()
            raise PolicyFailure("policy timed out") from None
        if line is None:
            raise PolicyFailure(f"policy exited ({self._proc.poll()})")
        try:
            action = json.loads(line)
        except ValueError as error:
            raise PolicyFailure("policy sent non-JSON") from error
        return action  # shape is judged by the environment, not here

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.kill()
        self._proc.wait()
        for pipe in (self._proc.stdin, self._proc.stdout):
            try:
                pipe.close()  # type: ignore[union-attr]
            except OSError:
                pass
        self._scratch.cleanup()


def policy_command(factory: str, seed: int, python: str | None = None) -> list[str]:
    """Command that runs auditlab.runner.policy_host for `factory` ("module:callable")."""
    import sys
    return [python or sys.executable, "-m", "auditlab.runner.policy_host", factory, str(seed)]


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment letting the child import auditlab while still hiding everything else."""
    package_parent = str(Path(__file__).resolve().parents[2])
    return scrubbed_env({"PYTHONPATH": package_parent, **(extra or {})})
