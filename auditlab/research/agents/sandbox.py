"""How the trusted controller launches the LLM network bridge (``python -m auditlab.research.agents.bridge``).

The bridge is NOT the ordinary policy sandbox: it needs one provider endpoint. Production topology
(docker_bridge_commands)::

    controller --stdin/stdout JSONL--> bridge container   (network: <name>, --internal: no gateway)
                                         | HTTPS_PROXY=http://<proxy>:3128
                                         v
                                       egress proxy container (networks: <name> + <name>-egress)
                                         | CONNECT only to the allowlisted host:port
                                         v
                                       provider API (TLS end to end; the proxy never sees plaintext)

The bridge gets no volume, no episode/private/trusted path and no secret value in argv: the key is
named with ``-e NAME`` so Docker copies it from the controller's environment. Its config (agent and
provider settings, no secrets, no paths) is the first stdin line. This module is trusted-side code;
the bridge itself must not import it.

Wire contract with bridge.py (BridgeProcess):
    -> {"type": "init", "config": {...}, "journal": [...] | null}     first line, exactly once
    <- {"type": "ready", ...}            or {"type": "error", ...}    (error closes the bridge)
    -> {"type": "observation", "observation": <public observation>}  once per turn
    <- {"action": <official action>, "telemetry": {...}}              optional "type": "action"
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from auditlab.research.agents.egress_proxy import format_target, parse_target, upstream_address
from auditlab.research.agents.protocol import get_protocol
from auditlab.runner.isolation import (PolicyFailure, SubprocessPolicy, launch_label_args,
                                       scrubbed_env)

BRIDGE_MODULE = "auditlab.research.agents.bridge"
PROXY_MODULE = "auditlab.research.agents.egress_proxy"
PROXY_PORT = 3128
BASE_ENV_KEYS = ("PATH", "SYSTEMROOT", "PYTHONIOENCODING", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH")
# `extra` may override these; HTTPS_PROXY lets a dev bridge use a local egress_proxy.
EXTRA_ENV_KEYS = frozenset({"PATH", "SYSTEMROOT", "PYTHONIOENCODING", "HTTPS_PROXY"})
_SECRET_NAME = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
_RESERVED_PREFIXES = ("PYTHON", "DOCKER", "AUDITLAB", "HTTP", "NO_PROXY", "ALL_PROXY", "PATH", "SYSTEMROOT",
                      "LD_", "SSL_", "REQUESTS_", "CURL_")
_DOCKER_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}")
_PACKAGE_PARENT = str(Path(__file__).resolve().parents[3])
DOCKER_CLIENT_KEYS = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH",
                      "DOCKER_TLS_VERIFY", "HOME")


def _check_secret_name(name: str) -> str:
    if not isinstance(name, str) or not _SECRET_NAME.fullmatch(name) or name.startswith(_RESERVED_PREFIXES):
        raise ValueError("secret_env must be an upper-case variable name such as ANTHROPIC_API_KEY")
    return name


def _under(path_text: str, roots: list[Path]) -> bool:
    try:
        candidate = os.path.normcase(str(Path(path_text).resolve(strict=False)))
    except (OSError, ValueError, RuntimeError):
        return False
    for root in roots:
        if candidate == root or candidate.startswith(root.rstrip("\\/") + os.sep):
            return True
    return False


def bridge_env(secret_env: str | None = None, extra: dict[str, str] | None = None, *,
               forbidden_roots=()) -> dict[str, str]:
    """Minimal environment for a bridge subprocess.

    Only PATH/SYSTEMROOT/PYTHONIOENCODING/PYTHONDONTWRITEBYTECODE/PYTHONPATH (= the package parent)
    plus, if named, the secret copied from THIS process's environment. Raises ValueError if any
    value names a path under one of ``forbidden_roots`` (episode dir, private dirs, trusted output).
    """
    unknown = set(extra or {}) - EXTRA_ENV_KEYS
    if unknown:
        raise ValueError(f"bridge_env does not accept {sorted(unknown)}")
    env = scrubbed_env({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": _PACKAGE_PARENT})
    env.update(extra or {})
    if secret_env is not None:
        _check_secret_name(secret_env)
        if not os.environ.get(secret_env):
            raise ValueError(f"secret variable {secret_env} is not set in the controller environment")
        env[secret_env] = os.environ[secret_env]
    roots = [os.path.normcase(str(Path(root).resolve(strict=False))) for root in forbidden_roots]
    for key, value in env.items():
        if not isinstance(value, str):
            raise ValueError(f"bridge_env value for {key} is not a string")
        if roots and key != secret_env and (
                any(_under(part, roots) for part in value.split(os.pathsep) if os.path.isabs(part))
                or any(root in os.path.normcase(value) for root in roots)):
            raise ValueError(f"bridge_env {key} references a forbidden directory")
    return env


def _protocol_id(config) -> str:
    if isinstance(config, (str, os.PathLike)):
        # Read on the trusted side only; the path is never passed to the bridge.
        config = json.loads(Path(config).read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("bridge config must be a JSON object")
    protocol = config.get("protocol_id")
    if protocol is None and isinstance(config.get("protocol"), dict):
        protocol = config["protocol"].get("protocol_id")
    if not isinstance(protocol, str):
        raise ValueError("bridge config has no protocol_id")
    return protocol


def dev_bridge_command(config, python: str | None = None) -> list[str]:
    """UNSAFE development launch: a host subprocess, NOT a network/filesystem sandbox.

    ``config`` is the bridge config dict or a JSON file read here (never passed on). Like the
    runner's ``--unsafe-dev-subprocess`` (train only), this is refused for any official protocol;
    an official run must use docker_bridge_commands.
    """
    if get_protocol(_protocol_id(config)).official:
        raise ValueError("the development bridge subprocess is not allowed for an official protocol")
    return [python or sys.executable, "-m", BRIDGE_MODULE]


@dataclass(frozen=True)
class DockerBridgePlan:
    """docker CLI argv lists. Run ``setup`` in order, then ``bridge_create`` with docker_client_env
    (it reads the secret by name), attach with ``bridge_attach``, finally ``teardown`` (ignore errors)."""
    image: str
    proxy_image: str
    network: str
    egress_network: str
    proxy_name: str
    bridge_name: str
    setup: tuple[tuple[str, ...], ...]
    bridge_create: tuple[str, ...]
    bridge_attach: tuple[str, ...]
    proxy_logs: tuple[str, ...]
    teardown: tuple[tuple[str, ...], ...]

    def all_argv(self) -> list[tuple[str, ...]]:
        return [*self.setup, self.bridge_create, self.bridge_attach, self.proxy_logs, *self.teardown]


_HARDENING = ("--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
              "--user", "65534:65534", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m")


def docker_bridge_commands(image: str, proxy_image: str | None = None, network_name: str | None = None,
                           secret_env: str | None = "ANTHROPIC_API_KEY", allow=("api.anthropic.com:443",), *,
                           cpus: str = "1", memory: str = "1g", pids: int = 64,
                           proxy_cpus: str = "0.5", proxy_memory: str = "128m",
                           egress_network: str | None = None, upstream_proxy: str | None = None) -> DockerBridgePlan:
    """Build (but do not run) the internal-network + egress-proxy + bridge topology.

    ``proxy_image`` defaults to ``image`` (the bridge image contains the stdlib proxy module).
    """
    for value in (image, proxy_image or image):
        if not isinstance(value, str) or not value or value.startswith("-") or any(c.isspace() for c in value):
            raise ValueError("invalid image reference")
    network = network_name or "auditlab-bridge-" + uuid.uuid4().hex[:16]
    egress = egress_network or network + "-egress"
    for name in (network, egress):
        if not _DOCKER_NAME.fullmatch(name):
            raise ValueError("invalid docker network name")
    if egress == network:
        raise ValueError("egress network must differ from the internal network")
    if secret_env is not None:
        _check_secret_name(secret_env)
    allow = [format_target(*parse_target(entry)) for entry in allow]
    if not allow:
        raise ValueError("allowlist is empty")
    for text in (cpus, memory, proxy_cpus, proxy_memory):
        if not re.fullmatch(r"[0-9.]+[bkmg]?", str(text)):
            raise ValueError("invalid resource limit")
    labels = tuple(launch_label_args())
    proxy, bridge = network + "-proxy", network + "-bridge"
    proxy_args = [arg for entry in allow for arg in ("--allow", entry)]
    if upstream_proxy is not None:
        upstream_address(upstream_proxy)
        proxy_args += ['--upstream-proxy', upstream_proxy]
    # `docker start` returns before Python has bound its socket. Wait inside the
    # proxy container, without granting the bridge any additional network access.
    readiness = (
        "import socket,time\n"
        "deadline=time.monotonic()+10\n"
        "while True:\n"
        " try:\n"
        f"  socket.create_connection(('127.0.0.1',{PROXY_PORT}),timeout=.5).close(); break\n"
        " except OSError:\n"
        "  if time.monotonic()>=deadline: raise\n"
        "  time.sleep(.05)\n"
    )
    setup = (
        ("docker", "network", "create", "--internal", *labels, network),
        ("docker", "network", "create", *labels, egress),
        ("docker", "create", "--pull=never", "--name", proxy, "--network", network, *_HARDENING,
         "--pids-limit", "64", "--cpus", proxy_cpus, "--memory", proxy_memory, "--memory-swap", proxy_memory,
         "--label", "org.auditlab.role=llm-egress-proxy", *labels, "--entrypoint", "python",
         proxy_image or image, "-m", PROXY_MODULE, "--listen", f"0.0.0.0:{PROXY_PORT}", *proxy_args),
        ("docker", "network", "connect", egress, proxy),
        ("docker", "start", proxy),
        ("docker", "exec", proxy, "python", "-c", readiness),
    )
    secret_args = ("-e", secret_env) if secret_env else ()
    proxy_env = tuple(arg for key in ('HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy')
                      for arg in ('-e', f'{key}=http://{proxy}:{PROXY_PORT}'))
    no_bypass = tuple(arg for key in ('NO_PROXY', 'no_proxy', 'ALL_PROXY', 'all_proxy') for arg in ('-e', f'{key}='))
    create = ("docker", "create", "-i", "--pull=never", "--name", bridge, "--network", network, *_HARDENING,
              "--pids-limit", str(int(pids)), "--cpus", cpus, "--memory", memory, "--memory-swap", memory,
              "--log-driver", "none", *proxy_env, *no_bypass,
              *secret_args, "--label", "org.auditlab.role=llm-bridge", *labels, image)
    teardown = (("docker", "rm", "--force", bridge), ("docker", "rm", "--force", proxy),
                ("docker", "network", "rm", network), ("docker", "network", "rm", egress))
    return DockerBridgePlan(image, proxy_image or image, network, egress, proxy, bridge, setup, create,
                            ("docker", "start", "--attach", "--interactive", bridge),
                            ("docker", "logs", proxy), teardown)


def docker_client_env(secret_env: str | None = None) -> dict[str, str]:
    """Environment for the docker CLI: client settings plus the secret, so ``-e NAME`` resolves."""
    env = scrubbed_env({key: os.environ[key] for key in DOCKER_CLIENT_KEYS if key in os.environ})
    if secret_env:
        _check_secret_name(secret_env)
        if not os.environ.get(secret_env):
            raise ValueError(f"secret variable {secret_env} is not set in the controller environment")
        env[secret_env] = os.environ[secret_env]
    return env


class BridgeProcess(SubprocessPolicy):
    """Bounded JSONL transport to the bridge (SubprocessPolicy limits, deadlines and failures).

    ``init`` is sent as the first line and must be acknowledged with ``{"type": "ready"}``.
    ``act`` returns ``{"action": ..., "telemetry": {...}}``; the controller strips telemetry before
    ``env.step``. Crash, malformed or oversized output -> PolicyFailure; deadline -> PolicyTimeout.
    """

    def __init__(self, command: list[str], *, env: dict[str, str] | None = None, timeout_s: float = 60.0,
                 init: dict | None = None, journal: list | None = None, init_timeout_s: float | None = None):
        super().__init__(command, env=env if env is not None else bridge_env(), timeout_s=timeout_s)
        self.ready: dict | None = None
        if init is None:
            return
        try:
            self.set_timeout(timeout_s if init_timeout_s is None else init_timeout_s)
            reply = SubprocessPolicy.act(self, {"type": "init", "config": init, "journal": journal})
            self.set_timeout(timeout_s)
            if not isinstance(reply, dict) or reply.get("type") != "ready":
                raise PolicyFailure("bridge rejected its configuration")
            self.ready = reply
        except BaseException:
            self.close()
            raise

    def act(self, public_observation, seconds_left: float | None = None) -> dict:
        message = {"type": "observation", "observation": public_observation}
        if seconds_left is not None:  # the attempt deadline, so the bridge can bound model calls and retries
            message["seconds_left"] = float(seconds_left)
        reply = super().act(message)
        if isinstance(reply, dict) and reply.get("type") == "failure":
            # The agent could not act (provider error, budget, deadline...): a typed outcome, not a crash.
            if set(reply) - {"type", "kind", "message", "error", "telemetry"} or not isinstance(reply.get("kind"), str):
                self.close()
                raise PolicyFailure("bridge failure reply has unexpected fields")
            return reply
        try:
            if not isinstance(reply, dict) or "action" not in reply:
                raise PolicyFailure("bridge reply has no action")
            if set(reply) - {"type", "action", "telemetry"} or reply.get("type", "action") != "action":
                raise PolicyFailure("bridge reply has unexpected fields")
            telemetry = reply.get("telemetry", {})
            if not isinstance(telemetry, dict):
                raise PolicyFailure("bridge telemetry is not an object")
        except PolicyFailure:
            self.close()
            raise
        return {"action": reply["action"], "telemetry": telemetry}


class DockerBridgeProcess(BridgeProcess):
    """Runs a DockerBridgePlan around BridgeProcess; tears the topology down on close.

    Not exercised against a real daemon in this repository yet (see docker/bridge/README.md).
    """

    def __init__(self, plan: DockerBridgePlan, *, secret_env: str | None, timeout_s: float = 60.0,
                 init: dict | None = None, journal: list | None = None, init_timeout_s: float | None = None):
        self.plan = plan
        self._docker_env = docker_client_env(None)
        try:
            for image in {plan.image, plan.proxy_image}:
                if self._docker(("docker", "image", "inspect", image)):
                    raise PolicyFailure("local bridge/proxy image is unavailable; load it first")
            for argv in plan.setup:
                if self._docker(argv, timeout=20):
                    raise PolicyFailure("bridge network or egress proxy could not be started")
            if self._docker(plan.bridge_create, env=docker_client_env(secret_env), timeout=20):
                raise PolicyFailure("bridge container could not be created")
            super().__init__(list(plan.bridge_attach), env=self._docker_env, timeout_s=timeout_s,
                             init=init, journal=journal, init_timeout_s=init_timeout_s)
        except BaseException:
            self._teardown()
            raise

    def _docker(self, argv, env=None, timeout: float = 10) -> int:
        try:
            return subprocess.run(list(argv), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  env=env or self._docker_env, timeout=timeout).returncode
        except (OSError, subprocess.TimeoutExpired):
            return 1

    def _teardown(self) -> None:
        for argv in self.plan.teardown:
            self._docker(argv)

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        try:
            super().close()
        finally:
            self._teardown()
