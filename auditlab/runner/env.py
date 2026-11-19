"""Trusted reset/step facade (SPEC §7-§8, §11).

Single source of truth for the budget is `self._inspections`: the number of accepted exploration
actions (unknown tools and failed lookups included, submit excluded). Everything else (remaining
steps, trajectory step numbers, truncation at the 100th inspection) is derived at the point of use.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable

from auditlab.contracts import MAX_STEPS, Action, Observation, StepResult, ToolReply
from auditlab.evaluator.matcher import validate_truth
from auditlab.runner import export as export_mod

VERSIONS = {"runner": "1.0", "evaluator": "1.0", "public_schema": "1.0"}
TOOLS = ("list_records", "get_record", "get_document", "calculator")
TOOL_DOC = {
    "list_records": {"args": {"table": "str", "cursor": "str|null", "limit": "1..50"}},
    "get_record": {"args": {"table": "str", "id": "str"}},
    "get_document": {"args": {"doc_id": "str"}},
    "calculator": {"args": {"expression": "str"}},
    "submit": {"args": {"episode_id": "str", "findings": "list (<=30)"}},
}
RUNNER_KEYS = ("snapshot_dir", "export_dir", "policy_name", "run_id", "time_limit_s")
# Detailed outcome -> run.json status (INTERFACES 1.1). Only a submitted episode is "completed";
# truncation and crashes are exported for the record but the console never lists them.
EXPORT_STATUS = {"submitted": "completed", "invalid_submission": "completed",
                 "truncated_budget": "truncated", "truncated_timeout": "truncated",
                 "policy_failure": "failed", "infra_error": "failed"}
COMPLETED = {"submitted", "invalid_submission", "truncated_budget", "truncated_timeout"}
DEFAULT_TIME_LIMIT_S = 60.0
_PACKAGE_PARENT = str(Path(__file__).resolve().parents[2])


class RunnerError(RuntimeError):
    """Trusted-side misconfiguration (never shown to a policy)."""


def _error(code: str, message: str) -> ToolReply:
    return {"ok": False, "error": {"code": code, "message": message}}


def _clean_reply(reply: object) -> ToolReply:
    """Only ok/result/error cross to the policy; the session cannot smuggle extra fields."""
    if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
        return _error("internal_error", "Internal error.")
    if reply["ok"]:
        return {"ok": True, "result": reply.get("result")}
    err = reply.get("error")
    if isinstance(err, dict) and isinstance(err.get("code"), str) and isinstance(err.get("message"), str):
        return {"ok": False, "error": {"code": err["code"], "message": err["message"]}}
    return _error("internal_error", "Internal error.")


def _default_opener(public_dir: Path):
    try:
        from auditlab.environment import open_session
    except ImportError as error:  # GPT Pro 1 has not merged the real module yet
        raise RunnerError("auditlab.environment.open_session is unavailable; pass session_opener=") from error
    return open_session(public_dir)


def _default_generator(seed: int, config: dict, output_dir: Path) -> None:
    try:
        from auditlab.generator import generate_episode
    except ImportError as error:
        raise RunnerError("auditlab.generator.generate_episode is unavailable; use runner.snapshot_dir") from error
    generate_episode(seed, config, output_dir)


class SubprocessEvaluator:
    """Runs `python -m auditlab.evaluator` per episode. It reads truth from the closed volume itself."""

    def __init__(self, timeout_s: float = 30.0):
        self.timeout_s = timeout_s

    def __call__(self, private_dir: Path, submission: object, inspection_steps: int) -> dict:
        env = {"PYTHONPATH": _PACKAGE_PARENT, "PYTHONIOENCODING": "utf-8"}
        for key in ("SYSTEMROOT", "PATH"):
            if key in os.environ:
                env[key] = os.environ[key]
        request = json.dumps({"private_dir": str(private_dir), "submission": submission,
                              "inspection_steps": inspection_steps})
        done = subprocess.run([sys.executable, "-m", "auditlab.evaluator"], input=request, text=True,
                              encoding="utf-8", capture_output=True, timeout=self.timeout_s, env=env,
                              cwd=_PACKAGE_PARENT, check=False)
        try:
            reply = json.loads(done.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError) as error:
            raise RunnerError(f"evaluator produced no reply (exit {done.returncode})") from error
        if not reply.get("ok"):
            raise RunnerError(f"evaluator failed: {reply.get('error')}")
        return reply["result"]


class InProcessEvaluator:
    """Same contract without a child process, for fast unit tests."""

    def __call__(self, private_dir: Path, submission: object, inspection_steps: int) -> dict:
        from auditlab.evaluator.service import handle
        return handle({"private_dir": str(private_dir), "submission": submission,
                       "inspection_steps": inspection_steps})


class AuditEnvironment:
    """Implements contracts.Environment. `mode` is "train" (terminal reward visible) or "closed"."""

    def __init__(self, mode: str, *, session_opener: Callable[[Path], object] | None = None,
                 generator: Callable[[int, dict, Path], None] | None = None,
                 clock: Callable[[], float] = time.monotonic, evaluator: object = "subprocess",
                 trusted_dir: str | Path | None = None):
        if mode not in ("train", "closed"):
            raise ValueError("mode must be 'train' or 'closed'")
        self.mode = mode
        self._opener = session_opener or _default_opener
        self._generator = generator or _default_generator
        self._clock = clock
        backends = {"subprocess": SubprocessEvaluator, "inprocess": InProcessEvaluator}
        if isinstance(evaluator, str):
            if evaluator not in backends:
                raise ValueError("unknown evaluator backend")
            evaluator = backends[evaluator]()
        self._evaluate = evaluator
        self._export_dir: Path | None = None
        self.trusted_dir = Path(trusted_dir) if trusted_dir else None
        self._tmp: tempfile.TemporaryDirectory | None = None
        self._phase: str | None = None  # None -> "active" -> "done"
        self.final: dict | None = None  # trusted record of the one finalization

    # ---------------------------------------------------------------- lifecycle
    def reset(self, seed: int, config: dict) -> tuple[Observation, dict]:
        """reset(seed, {snapshot_dir, export_dir, policy_name}) per INTERFACES 1.1.

        Trusted keys (never echoed to the policy): snapshot_dir, export_dir, policy_name, run_id,
        time_limit_s. Every other key is generator configuration, used only without snapshot_dir.
        """
        if self._phase == "active":  # an unfinished attempt is recorded as such, never as a result
            self._events.append({"event": "abandoned_by_reset"})
            self._persist_trusted("abandoned", None, None)
        self._phase = None  # if anything below fails, no stale episode stays steppable
        self.close()
        if not isinstance(config, dict):
            raise RunnerError("config must be an object")
        config = dict(config)
        options = {key: config.pop(key) for key in RUNNER_KEYS if key in config}
        time_limit = float(options.get("time_limit_s", DEFAULT_TIME_LIMIT_S))
        if not time_limit > 0:
            raise RunnerError("time_limit_s must be positive")
        export_dir = options.get("export_dir")
        if export_dir is not None:
            if self.mode != "train":
                raise RunnerError("closed runs are never exported publicly; use trusted_dir")
            if os.path.lexists(export_dir):
                raise RunnerError("export_dir must be a new, not yet existing directory")
        self._export_dir = Path(export_dir) if export_dir is not None else None
        snapshot = options.get("snapshot_dir")
        if snapshot is None:
            # The generator publishes only into a NEW directory: give it a fresh path, do not create it.
            self._tmp = tempfile.TemporaryDirectory(prefix="auditlab-")
            snapshot = Path(self._tmp.name) / "snapshot"
            self._generator(seed, config, snapshot)
        self._public_dir = Path(snapshot) / "public"
        self._private_dir = Path(snapshot) / "private"
        truth = json.loads((self._private_dir / "truth.json").read_text(encoding="utf-8"))
        validate_truth(truth)  # a malformed reference is a generator defect: fail before the policy sees anything
        manifest = json.loads((self._public_dir / "manifest.json").read_text(encoding="utf-8"))
        policy_file = self._public_dir / "policy.json"
        policy = json.loads(policy_file.read_text(encoding="utf-8")) if policy_file.is_file() else None
        self._episode_id = manifest["episode_id"]
        if truth["episode_id"] != self._episode_id:
            raise RunnerError("truth and manifest episode ids differ")
        self._session = self._opener(self._public_dir)
        self._run_id = options.get("run_id") or uuid.uuid4().hex[:12]
        self._policy_name = str(options.get("policy_name", "unknown"))
        self._seed = seed
        self._time_limit = time_limit
        self._started = self._clock()
        self._deadline = self._started + self._time_limit
        self._inspections = 0
        self._trajectory: list[dict] = []
        self._events: list[dict] = []  # trusted-only diagnostics
        self._infra: list[str] = []
        self.final = None
        self._phase = "active"
        observation = {"task": "Audit the company's documents for the target period using the listed tools, "
                               "then call submit once with your findings (possibly none).",
                       "tools": TOOL_DOC, "manifest": manifest, "policy": policy,
                       "remaining_steps": MAX_STEPS}
        return observation, {"episode_id": self._episode_id, "max_steps": MAX_STEPS, "mode": self.mode}

    def close(self) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None

    # ---------------------------------------------------------------- stepping
    def step(self, action: Action) -> StepResult:
        if self._phase != "active":
            return self._already_finished()
        if self._clock() >= self._deadline:
            return self._finish("truncated_timeout", action, _error("time_limit", "Time limit exceeded."))
        if not self._well_formed(action):
            # Damaged transport packet: not an accepted action, so no budget and no trajectory line.
            self._events.append({"event": "malformed_action"})
            return self._protocol("malformed_action", "Action must be an object with 'action' and 'args'.",
                                  False, False, remaining=True)
        name, args = action["action"], action.get("args")
        if name == "submit":
            return self._submit(action, args)  # a recognised submit with a broken body is judged, not dropped
        self._inspections += 1  # the single budget counter
        if name in TOOLS and not isinstance(args, dict):
            reply = _error("invalid_request", "Invalid request.")
        elif name in TOOLS:
            try:
                reply = _clean_reply(self._session.inspect({"action": name, "args": args}))
            except Exception as error:  # session defect: generic public error, details stay trusted
                self._events.append({"event": "session_exception", "detail": f"{type(error).__name__}: {error}"})
                self._infra.append("session_exception")
                reply = _error("internal_error", "Internal error.")
        else:
            reply = _error("unknown_action", "Unknown action.")
        observation = {**reply, "remaining_steps": MAX_STEPS - self._inspections}
        if self._inspections >= MAX_STEPS:
            return self._finish("truncated_budget", action, reply)
        return self._emit(action, observation, 0.0, False, False, {"inspection_steps": self._inspections})

    def abort(self, reason: str) -> StepResult:
        """Trusted driver hook: the policy crashed or broke the channel. Not a financial result.

        If the deadline has already passed, the published time-limit rule applies instead (SPEC §8):
        a policy that hangs until the deadline is a timeout, not a crash.
        """
        if self._phase != "active":
            return self._already_finished()
        if self._clock() >= self._deadline:
            return self.expire()
        self._events.append({"event": "abort", "reason": reason})
        return self._finish("policy_failure", None, _error("aborted", "Episode aborted."))

    def expire(self) -> StepResult:
        """Trusted driver hook: the deadline passed while waiting for the policy. Truncate with an empty answer."""
        if self._phase != "active":
            return self._already_finished()
        return self._finish("truncated_timeout", None, _error("time_limit", "Time limit exceeded."))

    def seconds_left(self) -> float:
        return max(0.0, self._deadline - self._clock()) if self._phase == "active" else 0.0

    # ---------------------------------------------------------------- internals
    @staticmethod
    def _well_formed(action: object) -> bool:
        return isinstance(action, dict) and isinstance(action.get("action"), str) \
            and set(action) <= {"action", "args"}

    def _already_finished(self) -> StepResult:
        if self._phase is None:
            return self._protocol("no_active_episode", "Call reset first.", False, False)
        return self._protocol("episode_finished", "The episode is already finished.",
                              self.final["terminated"], self.final["truncated"])

    def _protocol(self, code: str, message: str, terminated: bool, truncated: bool, remaining: bool = False) -> StepResult:
        observation: Observation = dict(_error(code, message))
        if remaining:
            observation["remaining_steps"] = MAX_STEPS - self._inspections
        return observation, 0.0, terminated, truncated, {"protocol_error": code}

    def _submit(self, action: Action, body: dict) -> StepResult:
        result = self._grade(body, self._inspections)
        if result is None:
            return self._finish("infra_error", action, _error("internal_error", "Internal error."))
        if result["status"] == "invalid_submission":
            return self._finish("invalid_submission", action,
                                _error("invalid_submission", "Submission failed schema validation."), result)
        return self._finish("submitted", action, {"ok": True, "result": {"status": "submitted"}}, result)

    def _grade(self, submission: object, inspection_steps: int) -> dict | None:
        try:
            return self._evaluate(self._private_dir, submission, inspection_steps)
        except Exception as error:
            self._events.append({"event": "evaluator_failure", "detail": f"{type(error).__name__}: {error}"})
            self._infra.append("evaluator_failure")
            return None

    def _finish(self, status: str, action: Action | None, reply: ToolReply, graded: dict | None = None) -> StepResult:
        """The one and only finalization. Scores exactly once and freezes the record."""
        submitted = status in ("submitted", "invalid_submission")
        if graded is None and status != "infra_error":
            graded = self._grade({"episode_id": self._episode_id, "findings": []}, self._inspections)
            if graded is None:
                status = "infra_error"
        used = self._inspections + (1 if submitted else 0)
        observation = {**reply, "remaining_steps": max(0, MAX_STEPS - used)}
        true_reward = graded["score"]["reward"] if graded else 0.0
        terminated = submitted
        truncated = not submitted
        public_reward = true_reward if self.mode == "train" else 0.0
        info: dict[str, object] = {"status": status}
        if self.mode == "train" and graded and status in COMPLETED:
            info["score"] = graded["score"]
        self._phase = "done"
        step = self._emit(action, observation, public_reward, terminated, truncated, info)
        self.final = {"status": status, "terminated": terminated, "truncated": truncated,
                      "inspection_steps": self._inspections, "graded": graded, "reward": true_reward,
                      "submission": copy.deepcopy(action.get("args")) if submitted and action
                      else {"episode_id": self._episode_id, "findings": []}}
        self._persist(status, graded)
        return step

    def _emit(self, action: object, observation: Observation, reward: float, terminated: bool,
              truncated: bool, info: dict) -> StepResult:
        # Deep copies: a policy mutating its action or observation later cannot rewrite the record.
        self._trajectory.append({"episode_id": self._episode_id, "step": len(self._trajectory) + 1,
                                 "action": copy.deepcopy(action), "observation": copy.deepcopy(observation),
                                 "reward": reward, "terminated": terminated, "truncated": truncated})
        return observation, reward, terminated, truncated, info

    def _run_record(self, status: str) -> dict:
        return {"run_id": self._run_id, "episode_id": self._episode_id, "policy": self._policy_name,
                "mode": self.mode, "status": status}

    def _persist_trusted(self, status: str, graded: dict | None, true_reward: float | None) -> None:
        if self.trusted_dir is None:
            return
        report = {**self._run_record(status), "versions": VERSIONS, "seed": self._seed,
                  "inspection_steps": self._inspections, "elapsed_s": self._clock() - self._started,
                  "evaluation": graded, "events": self._events, "infra": self._infra,
                  "true_reward": true_reward, "completed": status in COMPLETED and not self._infra}
        export_mod.write_trusted_report(self.trusted_dir, self._run_id, report, self._trajectory)

    def _persist(self, status: str, graded: dict | None) -> None:
        self._persist_trusted(status, graded, self.final["reward"])
        if self.mode != "train" or self._export_dir is None:
            return
        export_status = "failed" if self._infra else EXPORT_STATUS[status]
        run = {**self._run_record(export_status), "outcome": status,
               "inspection_steps": self._inspections}
        written = export_mod.write_public_run(self._export_dir, run, self._public_dir, self._trajectory,
                                              self.final["submission"], graded["score"] if graded else None)
        if written is None:
            self._events.append({"event": "export_exists_not_overwritten"})
