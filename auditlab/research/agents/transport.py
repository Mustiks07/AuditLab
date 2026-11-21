"""Controller-side transports to the network bridge, with the executor's send(message) interface.

    in_process  InProcessBridge (execute.py)   development/tests, research protocol only
    subprocess  SubprocessBridge               a host process with a scrubbed env; NOT a network sandbox
    docker      DockerBridge                   internal network + allowlisting egress proxy (sandbox.py)

The secret is never in a message: the bridge reads it from its environment, which receives only the
variable NAMED in the provider config. Messages carry public observations and settings only.
"""
from __future__ import annotations

from auditlab.research.agents import sandbox
from auditlab.runner.isolation import PolicyFailure, PolicyTimeout

INIT_TIMEOUT_S = 30.0


class _BridgeTransport:
    isolated = False

    def __init__(self):
        self.process = None

    def _start(self, init: dict):
        raise NotImplementedError

    def send(self, message: dict) -> dict:
        kind = message.get("type")
        try:
            if kind == "init":
                self.process = self._start(message["config"])
                return self.process.ready
            if kind == "observation":
                seconds = float(message.get("seconds_left", 60.0))
                self.process.set_timeout(max(1.0, seconds + 1.0))
                return self.process.act(message["observation"], seconds_left=seconds)
            return {"type": "closed", "telemetry": None}
        except PolicyTimeout as error:
            return {"type": "failure", "kind": "timeout", "message": f"bridge did not answer in time: {error}",
                    "telemetry": None}
        except (PolicyFailure, ValueError, OSError) as error:
            return {"type": "failure", "kind": "bridge_failure", "message": f"{type(error).__name__}: {error}",
                    "telemetry": None}

    def close(self) -> None:
        if self.process is not None:
            self.process.close()


class SubprocessBridge(_BridgeTransport):
    """Host subprocess with a scrubbed environment. Development only: refused for the official protocol."""

    def __init__(self, provider_config: dict):
        super().__init__()
        self.secret_env = provider_config.get("api_key_env") if provider_config.get("provider") != "test_double" else None

    def _start(self, init: dict):
        command = sandbox.dev_bridge_command(init)
        return sandbox.BridgeProcess(command, env=sandbox.bridge_env(self.secret_env), init=init,
                                     init_timeout_s=INIT_TIMEOUT_S)


class DockerBridge(_BridgeTransport):
    """Bridge container on an internal network whose only way out is the allowlisting egress proxy."""

    isolated = True

    def __init__(self, docker_config: dict, provider_config: dict):
        super().__init__()
        self.docker_config = docker_config
        self.secret_env = provider_config.get("api_key_env", "ANTHROPIC_API_KEY")
        self.allow = tuple(docker_config.get("allow") or ("api.anthropic.com:443",))

    def _start(self, init: dict):
        plan = sandbox.docker_bridge_commands(self.docker_config["image"], self.docker_config.get("proxy_image"),
                                              secret_env=self.secret_env, allow=self.allow,
                                              cpus=str(self.docker_config.get("cpus", "1")),
                                              memory=str(self.docker_config.get("memory", "1g")))
        return sandbox.DockerBridgeProcess(plan, secret_env=self.secret_env, init=init, init_timeout_s=INIT_TIMEOUT_S)


def subprocess_bridge(provider_config: dict) -> SubprocessBridge:
    return SubprocessBridge(provider_config)


def docker_bridge(docker_config: dict, provider_config: dict) -> DockerBridge:
    if not docker_config.get("image"):
        raise ValueError("docker transport needs docker.image (a preloaded local bridge image)")
    return DockerBridge(docker_config, provider_config)
