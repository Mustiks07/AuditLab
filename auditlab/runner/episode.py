"""Drive one episode: policy <-> environment. Also the trusted oracle adapter."""
from __future__ import annotations

import json
from pathlib import Path

from auditlab.contracts import Policy, StepResult
from auditlab.evaluator.oracle import oracle_submission
from auditlab.runner.isolation import PolicyTimeout


def run_episode(env, policy: Policy, seed: int, config: dict) -> StepResult:
    """Run to the terminal step. A crashing or hanging policy is aborted, never scored as an auditor error."""
    result: StepResult | None = None
    try:
        observation, _ = env.reset(seed, config)
        while True:
            if env.seconds_left() <= 0:
                return env.expire()
            if hasattr(policy, "set_timeout"):
                policy.set_timeout(env.seconds_left())
            action = policy.act(observation)
            observation, reward, terminated, truncated, info = result = env.step(action)
            if terminated or truncated:
                return result
    except PolicyTimeout:
        return env.expire()  # SPEC §8: time limit -> truncated, empty answer, accepted inspections count
    except Exception as error:  # crash, broken channel, policy bug
        if env._phase is None:
            raise  # reset/configuration failure is an infrastructure error, not policy performance
        return env.abort(f"{type(error).__name__}: {error}")
    finally:
        close = getattr(policy, "close", None)
        if close:
            close()


class OraclePolicy:
    """Trusted adapter: reads the closed truth, so it must never be started inside a policy sandbox.

    `inspections` models the price of steps: it issues that many cheap list_records calls before
    submitting the perfect answer, giving the reward ceiling for a known step cost.
    """

    def __init__(self, snapshot_dir: str | Path, inspections: int = 0):
        truth = json.loads((Path(snapshot_dir) / "private" / "truth.json").read_text(encoding="utf-8"))
        self._submission = oracle_submission(truth)
        self._left = inspections

    def act(self, public_observation):
        if self._left > 0:
            self._left -= 1
            return {"action": "list_records", "args": {"table": "payments", "cursor": None, "limit": 1}}
        return {"action": "submit", "args": self._submission}
