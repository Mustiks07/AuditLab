"""Network bridge: the only AuditLab process that talks to a model provider.

    python -m auditlab.research.agents.bridge        # JSON Lines on stdin/stdout

It receives control lines from the trusted controller and answers each with exactly one line:
    {"type": "init", "config": {"agent", "provider", "budget_usd", "protocol_id"}, "journal": null}
                                                                -> {"type": "ready", "agent", "prompt"}
    {"type": "observation", "observation": {...}, "seconds_left": float}
                                                                -> {"type": "action", "action", "telemetry"}
                                                                   or {"type": "failure", "kind", "message", ...}
    {"type": "close"}                                           -> {"type": "closed", "telemetry"}
(sandbox.BridgeProcess enforces this shape and its size and time limits on the controller side.)
The observation is the public observation of the official adapter, nothing else. The bridge imports
no evaluator, runner, generator or episode loader; it gets no paths, seed or truth, and it reads its
provider key from the environment variable whose NAME the config gives. Telemetry carries usage,
costs and the public transcript back to the controller, which owns all files.
"""
from __future__ import annotations

import json
import sys

from auditlab.research.agents.agent import AgentFailure, LLMAuditor
from auditlab.research.agents.providers import ProviderError, ProviderResponse, ScriptedProvider, make_provider
from auditlab.research.agents.reliability import RetryPolicy, SpendGuard

PROVIDER_FAILURE = {"budget": "budget_stop", "deadline": "timeout", "refusal": "provider_refusal",
                    "invalid_response": "invalid_response", "rate_limit": "rate_limited",
                    "quota_exhausted": "quota_exhausted"}


def _provider(config: dict):
    if config.get("provider") == "test_double":  # explicitly labelled scripted turns, for tests only
        return ScriptedProvider([ProviderResponse.from_record(item) for item in config.get("script", [])],
                                model=config.get("model", "test-double"))
    return make_provider(config)


class BridgeSession:
    def __init__(self, provider=None):
        self._injected = provider
        self.agent: LLMAuditor | None = None

    def handle(self, message: dict) -> dict:
        kind = message.get("type")
        if kind == "init":
            return self._init(message)
        if self.agent is None:
            return {"type": "failure", "kind": "bridge_failure", "message": "init first", "telemetry": None}
        if kind == "observation":
            self.agent.set_deadline(float(message.get("seconds_left", 0.0)))
            try:
                action = self.agent.act(message["observation"])
            except AgentFailure as error:
                return {"type": "failure", "kind": error.kind, "message": str(error), "telemetry": self.agent.drain_telemetry()}
            except ProviderError as error:
                return {"type": "failure", "kind": PROVIDER_FAILURE.get(error.kind, "provider_error"),
                        "message": str(error), "error": error.to_dict(), "telemetry": self.agent.drain_telemetry()}
            return {"type": "action", "action": action, "telemetry": self.agent.drain_telemetry()}
        if kind == "close":
            self.agent.close()
            return {"type": "closed", "telemetry": self.agent.drain_telemetry()}
        return {"type": "failure", "kind": "bridge_failure", "message": f"unknown message {kind!r}", "telemetry": None}

    def _init(self, message: dict) -> dict:
        message = message.get("config") or {}
        provider_config = message.get("provider") or {}
        try:
            provider = self._injected or _provider(provider_config)
        except ProviderError as error:
            return {"type": "error", "kind": "provider_error", "message": str(error)}
        agent_config = message.get("agent") or {}
        retry = RetryPolicy(**agent_config.get("retry", {}))
        guard = SpendGuard(message.get("budget_usd"), provider_config.get("prices"),
                           allow_unpriced=bool(provider.is_test_double))
        self.agent = LLMAuditor(provider, agent_config.get("variant", "tools"),
                                max_tokens=int(agent_config.get("max_tokens", 16000)),
                                max_model_calls_per_action=int(agent_config.get("max_model_calls_per_action", 12)),
                                max_prechecks=int(agent_config.get("max_prechecks", 2)),
                                max_nudges=int(agent_config.get("max_nudges", 2)), retry=retry, guard=guard,
                                prompt_variant=str(agent_config.get("prompt_variant", "A")))
        return {"type": "ready", "agent": self.agent.describe(), "prompt": self.agent.prompt_record()}


def main() -> int:
    session = BridgeSession()
    for line in sys.stdin:
        try:
            reply = session.handle(json.loads(line))
        except Exception as error:  # never leak a traceback with environment details to the stream
            reply = {"type": "failure", "kind": "bridge_failure", "message": type(error).__name__, "telemetry": None}
        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
