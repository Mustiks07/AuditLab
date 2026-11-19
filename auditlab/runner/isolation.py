"""Bounded JSON Lines transport and the Docker boundary for untrusted policies.

SubprocessPolicy alone is a development transport, NOT a filesystem/network sandbox.
Production callers use DockerPolicy, which never falls back to a host subprocess.
"""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from auditlab.contracts import Action, Observation

MAX_ACTION_BYTES = 256 * 1024
MAX_OBSERVATION_BYTES = 1024 * 1024

# Run in the existing policy container. No fallback to memory.current/docker stats:
# those are current usage, not the kernel's recorded peak.
_CGROUP_PROBE = """
import json
from pathlib import Path
root = Path('/sys/fs/cgroup')
values, unavailable = {}, {}
for name in ('memory.peak', 'memory.max', 'cpu.stat', 'cpu.max', 'cpuset.cpus.effective'):
    try:
        values[name] = (root / name).read_text().strip()
    except OSError as error:
        unavailable[name] = type(error).__name__
print(json.dumps({'values': values, 'unavailable': unavailable}))
"""


def _parse_cgroup_measurements(payload: dict) -> dict:
    """Parse cgroup v2 counters; unsupported/missing counters stay explicitly unknown."""
    values = payload.get("values", {})
    unavailable = dict(payload.get("unavailable", {}))

    def integer(value, name):
        if value is None:
            unavailable.setdefault(name, "missing")
            return None
        try:
            result = int(value)
            if result < 0:
                raise ValueError
            return result
        except (ValueError, TypeError):
            unavailable[name] = "not_a_nonnegative_integer"
            return None

    peak = integer(values.get("memory.peak"), "memory.peak")
    memory = integer(values.get("memory.max"), "memory.max")
    stats = {}
    for line in str(values.get("cpu.stat", "")).splitlines():
        columns = line.split()
        if len(columns) == 2:
            stats[columns[0]] = columns[1]
    usage = integer(stats.get("usage_usec"), "cpu.stat/usage_usec")
    user = integer(stats.get("user_usec"), "cpu.stat/user_usec")
    system = integer(stats.get("system_usec"), "cpu.stat/system_usec")
    quota_parts = str(values.get("cpu.max", "")).split()
    quota = integer(quota_parts[0] if len(quota_parts) == 2 else None, "cpu.max/quota")
    period = integer(quota_parts[1] if len(quota_parts) == 2 else None, "cpu.max/period")
    status = "measured" if peak is not None and usage is not None else (
        "partial" if peak is not None or usage is not None else "not_checked")
    return {"status": status, "peak_memory_bytes": peak, "cpu_usage_usec": usage,
            "cpu_user_usec": user, "cpu_system_usec": system, "memory_limit_bytes": memory,
            "cpu_quota_usec": quota, "cpu_period_usec": period,
            "cpuset_cpus": values.get("cpuset.cpus.effective"), "unavailable": unavailable,
            "reason": None if status == "measured" else "cgroup_v2_counters_unavailable_or_invalid"}


class PolicyFailure(RuntimeError):
    """The policy crashed or broke the channel; separate from financial performance."""


class PolicyTimeout(PolicyFailure):
    """No complete exchange before the episode deadline."""


def scrubbed_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "PATH") if key in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra or {})
    return env


def docker_command(image: str, *, cpus: str = "2", memory: str = "4g", pids: int = 128,
                   container_name: str | None = None, cpuset: str | None = None) -> list[str]:
    """No registry access, network, host mounts, writable rootfs, GPU or capabilities."""
    command = ["docker", "run", "--rm", "-i", "--pull=never", "--network", "none", "--read-only",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", str(pids),
               "--cpus", cpus, "--memory", memory, "--memory-swap", memory, "--user", "65534:65534",
               "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--log-driver", "none"]
    if container_name:
        command += ["--name", container_name]
    if cpuset is not None:
        command += ["--cpuset-cpus", cpuset]
    return command + [image]


class SubprocessPolicy:
    """Development stdio transport. The child retains host filesystem permissions.

    Both input and output have a byte ceiling, only one reply may be pending, and
    the same deadline covers writing the observation and reading the action.
    """

    def __init__(self, command: list[str], *, env: dict[str, str] | None = None, timeout_s: float = 60.0):
        self._scratch = tempfile.TemporaryDirectory(prefix="auditlab-policy-")
        self.timeout_s = timeout_s
        self._closed = False
        self._lock = threading.Lock()
        self._awaiting = False
        self._failure: str | None = None
        self._lines: queue.Queue = queue.Queue(maxsize=1)
        self._writers: list[threading.Thread] = []
        options = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        try:
            self._proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL, bufsize=0,
                                          cwd=self._scratch.name,
                                          env=env if env is not None else scrubbed_env(),
                                          close_fds=True, **options)
        except BaseException:
            self._scratch.cleanup()
            raise
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _kill_process(self) -> None:
        if os.name != "nt":
            # Also reap descendants retaining a copy of either pipe.
            try:
                os.killpg(self._proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif self._proc.poll() is None:
            # This backend is development-only. DockerPolicy also removes its container.
            try:
                subprocess.run(["taskkill", "/PID", str(self._proc.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                self._proc.kill()

    def _fail(self, reason: str) -> None:
        with self._lock:
            if self._failure is None:
                self._failure = reason
        self._kill_process()
        try:
            self._lines.put_nowait(None)
        except queue.Full:
            pass

    def _pump(self) -> None:
        try:
            while not self._closed:
                # Raw binary IO keeps the cap in bytes, including invalid UTF-8.
                line = self._proc.stdout.readline(MAX_ACTION_BYTES + 1)
                if not line:
                    try:
                        self._lines.put_nowait(None)
                    except queue.Full:
                        pass
                    return
                if len(line) > MAX_ACTION_BYTES or not line.endswith(b"\n"):
                    self._fail("policy message exceeds byte limit or is not newline terminated")
                    return
                with self._lock:
                    permitted = self._awaiting
                    self._awaiting = False
                if not permitted:
                    self._fail("policy sent an unsolicited or duplicate reply")
                    return
                try:
                    self._lines.put_nowait(line)
                except queue.Full:
                    self._fail("policy reply buffer is full")
                    return
        except (OSError, ValueError):
            if not self._closed:
                self._fail("policy output channel failed")

    def set_timeout(self, seconds: float) -> None:
        self.timeout_s = seconds

    def act(self, public_observation: Observation) -> Action:
        deadline = time.monotonic() + max(self.timeout_s, 0)
        if self._closed:
            raise PolicyFailure("policy is closed")
        payload = (json.dumps(public_observation, allow_nan=False) + "\n").encode("utf-8")
        if len(payload) > MAX_OBSERVATION_BYTES:
            raise PolicyFailure("observation exceeds transport byte limit")
        with self._lock:
            if self._failure:
                raise PolicyFailure(self._failure)
            if self._awaiting or not self._lines.empty():
                raise PolicyFailure("policy exchange is not synchronized")
            self._awaiting = True
        written = threading.Event()
        errors: list[Exception] = []

        def write() -> None:
            try:
                pending = memoryview(payload)
                while pending:
                    count = self._proc.stdin.write(pending)
                    if not count:
                        raise OSError("closed input")
                    pending = pending[count:]
            except (OSError, ValueError) as error:
                errors.append(error)
            finally:
                written.set()

        writer = threading.Thread(target=write, daemon=True)
        self._writers = [thread for thread in self._writers if thread.is_alive()]
        self._writers.append(writer)
        writer.start()
        try:
            if not written.wait(max(0, deadline - time.monotonic())):
                raise PolicyTimeout("policy input timed out")
            if errors:
                raise PolicyFailure("policy closed its input")
            try:
                line = self._lines.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise PolicyTimeout("policy output timed out") from None
            with self._lock:
                failure = self._failure
            if failure:
                raise PolicyFailure(failure)
            if line is None:
                raise PolicyFailure("policy exited before replying")
            if time.monotonic() >= deadline:
                raise PolicyTimeout("policy exchange exceeded deadline")
            try:
                return json.loads(line.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(
                    ValueError("nonfinite JSON")))
            except (UnicodeError, ValueError, RecursionError) as error:
                raise PolicyFailure("policy sent invalid JSON") from error
        except PolicyFailure:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._kill_process()
        try:
            self._proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=3)
        self._reader.join(timeout=1)
        for thread in self._writers:
            thread.join(timeout=1)
        for pipe in (self._proc.stdin, self._proc.stdout):
            try:
                pipe.close()
            except OSError:
                pass
        self._scratch.cleanup()


class DockerPolicy(SubprocessPolicy):
    """One isolated container per episode; local image only, explicit container cleanup."""

    def __init__(self, image: str, name: str = "reconcile", seed: int = 0, *,
                 factory: str | None = None, timeout_s: float = 60.0,
                 cpus: str = "2", memory: str = "4g", cpuset: str | None = None):
        self.container_name = "auditlab-policy-" + uuid.uuid4().hex
        self.measurements = {
            "status": "not_checked", "source": "cgroup_v2_via_docker_exec",
            "peak_memory_bytes": None, "cpu_usage_usec": None, "cpu_user_usec": None,
            "cpu_system_usec": None, "memory_limit_bytes": None,
            "cpu_quota_usec": None, "cpu_period_usec": None, "cpuset_cpus": None,
            "requested_limits": {"cpus": cpus, "memory": memory, "cpuset": cpuset},
            "image_ref": image, "image_id": None, "image_repo_digests": [],
            "reason": "not_collected", "unavailable": {},
            "note": "Policy-container cgroup only; includes the short measurement process. "
                    "Does not measure trusted runtime or combined simultaneous peak.",
        }
        # Docker client configuration is trusted host configuration, not passed into the container.
        self._docker_env = scrubbed_env({key: os.environ[key] for key in
            ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY", "HOME")
            if key in os.environ})
        checked = subprocess.run(["docker", "image", "inspect", image],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 env=self._docker_env, timeout=10)
        if checked.returncode:
            raise PolicyFailure("local policy image is unavailable; load the release image first")
        try:
            metadata = json.loads(checked.stdout)[0]
            self.measurements["image_id"] = metadata.get("Id")
            self.measurements["image_repo_digests"] = metadata.get("RepoDigests") or []
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            # Optional provenance must not change an otherwise usable backend.
            self.measurements["image_metadata_reason"] = "image_inspect_metadata_unavailable"
        arguments = ["--factory", factory, str(seed)] if factory else [name, str(seed)]
        command = docker_command(image, cpus=cpus, memory=memory,
                                 container_name=self.container_name, cpuset=cpuset) + arguments
        # Create first so cleanup cannot race a daemon still creating a container after
        # its `docker run` client was killed. Starting attaches only to an existing ID.
        created = subprocess.run(["docker", "create", *command[2:]],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 env=self._docker_env, timeout=10)
        if created.returncode:
            raise PolicyFailure("policy container could not be created")
        try:
            super().__init__(["docker", "start", "--attach", "--interactive", self.container_name],
                             env=self._docker_env, timeout_s=timeout_s)
        except BaseException:
            self._remove_container()
            raise

    def _remove_container(self) -> None:
        removed = subprocess.run(["docker", "rm", "--force", self.container_name],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 env=self._docker_env, timeout=5)
        if removed.returncode:
            # --rm may already have deleted a normally exited container.
            present = subprocess.run(["docker", "ps", "--all", "--filter",
                                      "name=^/" + self.container_name + "$", "--format", "{{.ID}}"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     env=self._docker_env, timeout=5)
            if present.returncode or present.stdout.strip():
                raise PolicyFailure("policy container cleanup failed or could not be verified")

    def close(self) -> None:
        if self._closed:
            return
        # Killing only `docker run` leaves the daemon's container alive. Remove by unique name.
        try:
            self._collect_measurements()
            self._remove_container()
        finally:
            super().close()

    def _collect_measurements(self) -> None:
        """Capture real kernel counters before container deletion; telemetry is best effort."""
        try:
            measured = subprocess.run(["docker", "exec", self.container_name,
                                       "python", "-c", _CGROUP_PROBE],
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      env=self._docker_env, timeout=2)
            if measured.returncode:
                self.measurements["reason"] = "container_unavailable_or_cgroup_probe_failed"
                return
            self.measurements.update(_parse_cgroup_measurements(json.loads(measured.stdout)))
        except Exception as error:
            # A stopped container, old kernel or unavailable daemon must not prevent cleanup.
            self.measurements["reason"] = "measurement_failed:" + type(error).__name__


def policy_command(name: str, seed: int, python: str | None = None) -> list[str]:
    import sys
    return [python or sys.executable, "-m", "auditlab.runner.policy_host", name, str(seed)]


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Unsafe development imports; never use this as the production isolation boundary."""
    package_parent = str(Path(__file__).resolve().parents[2])
    return scrubbed_env({"PYTHONPATH": package_parent, **(extra or {})})
