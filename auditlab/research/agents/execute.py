"""Trusted controller for one attempt: official adapter <-> agent bridge, artifacts, closed evaluation.

The controller owns the environment (judge_adapter.make_env, closed mode) and every file. The agent
side (a bridge process for API models, or a local baseline policy) receives only the public
observations the adapter returns. The financial evaluation is read from the adapter's trusted result
and written to a separate trusted directory; the public attempt record only links to it.

Layout under an output root:
    attempts/<attempt_id>/attempt.json, prompt.json, steps.jsonl, transcript.jsonl, provider_calls.jsonl
    trusted/<attempt_id>/evaluation.json
An attempt is written to attempts/<id>.partial/ and renamed only after it is complete, so a crash
leaves a partial directory that resume() turns into an "interrupted" record (its paid calls kept).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from auditlab.research.agents.protocol import ARTIFACT_SCHEMA_VERSION, get_protocol

OPERATIONAL_STATUSES = ("completed", "action_budget_exhausted", "timeout", "provider_error", "provider_refusal",
                        "invalid_response", "budget_stop", "bridge_failure", "interrupted")
EVALUABLE = ("completed", "action_budget_exhausted", "timeout")  # the published rules define their result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def source_sha() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10,
                             cwd=Path(__file__).resolve().parents[3])
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def public_digest(episode_dir: Path) -> str:
    """sha256 over the episode's PUBLIC files only (names and bytes)."""
    digest = hashlib.sha256()
    public = Path(episode_dir) / "public"
    for file in sorted(p for p in public.rglob("*") if p.is_file()):
        digest.update(file.relative_to(public).as_posix().encode() + b"\0" + file.read_bytes() + b"\0")
    return digest.hexdigest()


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _append(path: Path, rows) -> None:
    with path.open("a", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


class Bridge(Protocol):
    def send(self, message: dict) -> dict: ...

    def close(self) -> None: ...


class InProcessBridge:
    """Development/test transport: the bridge session in this process. NOT a network sandbox; it
    still receives only public observations, but the official protocol requires an isolated bridge."""

    isolated = False

    def __init__(self, provider=None):
        from auditlab.research.agents.bridge import BridgeSession
        self.session = BridgeSession(provider)

    def send(self, message: dict) -> dict:
        return json.loads(json.dumps(self.session.handle(json.loads(json.dumps(message)))))

    def close(self) -> None:
        pass


class PolicyBridge:
    """Baseline policies (empty, flag_all, reconcile, random) behind the same messages, zero cost.

    AuditLab policies speak the internal {"action","args"} envelope; this translates them 1:1 to the
    official {"name","args"} envelope (submit args -> {"findings": args})."""

    isolated = False

    def __init__(self, name: str, seed: int = 0):
        self.name, self.seed = name, seed
        self.policy = None

    def send(self, message: dict) -> dict:
        kind = message.get("type")
        if kind == "init":
            if self.name != "empty":
                from auditlab.policies import make_policy
                self.policy = make_policy(self.name, self.seed)
            return {"type": "ready", "agent": {"kind": "baseline_policy", "policy": self.name, "seed": self.seed,
                                               "test_double": False}, "prompt": None}
        if kind == "observation":
            observation = message["observation"]
            if self.name == "empty":  # submits no findings as its first and only action
                episode = (observation.get("manifest") or {}).get("episode_id")
                return {"type": "action", "action": {"name": "submit", "args": {"findings": {
                    "episode_id": episode, "findings": []}}}, "telemetry": None}
            internal = self.policy.act(_internal_observation(observation))
            name, args = internal.get("action"), internal.get("args")
            action = {"name": "submit", "args": {"findings": args}} if name == "submit" else {"name": name, "args": args}
            return {"type": "action", "action": action, "telemetry": None}
        return {"type": "closed", "telemetry": None}

    def close(self) -> None:
        pass


def _internal_observation(observation: dict) -> dict:
    """Official observation -> the runner's internal shape that auditlab.policies were written for."""
    if "manifest" in observation:
        return {"task": observation.get("task"), "manifest": observation["manifest"], "policy": observation.get("policy"),
                "remaining_steps": observation["remaining_steps"]}
    if observation.get("error") is None:
        return {"ok": True, "result": observation.get("result"), "remaining_steps": observation["remaining_steps"]}
    return {"ok": False, "error": observation["error"], "remaining_steps": observation["remaining_steps"]}


def _status_from_env(env_status: str) -> str:
    return {"submitted": "completed", "invalid_submission": "completed", "truncated_budget": "action_budget_exhausted",
            "truncated_timeout": "timeout"}.get(env_status, "bridge_failure")


def run_attempt(spec: dict, output_root: str | Path, bridge_factory: Callable[[], Bridge], *,
                env_factory: Callable | None = None, clock: Callable[[], float] = time.monotonic) -> dict:
    """Run one attempt end to end and return the finalized public attempt record.

    spec: {attempt_id, episode_ref (episode dir), episode: {family_id, split, label}, variant, agent (config),
           provider (config without secrets), protocol_id, repetition, seed, budget_usd}
    """
    protocol = get_protocol(spec["protocol_id"])
    root = Path(output_root)
    attempt_id = spec["attempt_id"]
    final_dir, partial = root / "attempts" / attempt_id, root / "attempts" / f"{attempt_id}.partial"
    trusted = root / "trusted" / attempt_id
    if final_dir.exists():
        raise FileExistsError(f"attempt {attempt_id} is already finalized; a repeat needs a new attempt_id")
    if partial.exists():
        raise FileExistsError(f"attempt {attempt_id} has a partial directory; run resume first")
    partial.mkdir(parents=True)
    trusted.mkdir(parents=True, exist_ok=True)
    episode_dir = Path(spec["episode_ref"])
    if env_factory is None:
        import judge_adapter
        env_factory = lambda: judge_adapter.make_env(time_limit_s=protocol.time_limit_s)  # noqa: E731
    record = {"schema_version": ARTIFACT_SCHEMA_VERSION, "artifact_id": attempt_id, "attempt_id": attempt_id, "kind": "attempt",
              "source_sha": source_sha(), "protocol_id": protocol.protocol_id, "protocol": protocol.to_dict(),
              "episode": {"episode_ref": str(episode_dir), "episode_sha256": public_digest(episode_dir),
                          **(spec.get("episode") or {})},
              "variant": spec.get("variant"), "repetition": spec.get("repetition", 0), "seed": spec.get("seed", 0),
              "budget_usd": spec.get("budget_usd"), "started_at": _now(), "finished_at": None,
              "status": None, "status_detail": None, "agent": None, "test_double": False,
              "totals": {}, "final_action": None, "final_answer": None, "inspection_steps": None, "evaluation_ref": str(Path("trusted") / attempt_id / "evaluation.json"),
              "isolated_bridge": None}
    _write_json(partial / "attempt.json", record)
    env = env_factory()
    bridge = bridge_factory()
    record["isolated_bridge"] = bool(getattr(bridge, "isolated", False))
    if protocol.official and not record["isolated_bridge"] and spec.get("agent", {}).get("kind") != "baseline_policy":
        raise ValueError("the official protocol requires an isolated bridge for API agents")
    started = clock()
    steps, tokens, cost, calls, failure = 0, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}, 0.0, 0, None
    unpriced = 0
    try:
        observation, info = env.reset(int(spec.get("seed", 0)), {"mode": "closed", "episode_dir": str(episode_dir),
                                                                  "team_config": {}})
        ready = bridge.send({"type": "init", "journal": None, "config": {
            "agent": spec.get("agent") or {}, "provider": spec.get("provider") or {},
            "budget_usd": spec.get("budget_usd"), "protocol_id": protocol.protocol_id}})
        if not isinstance(ready, dict) or ready.get("type") != "ready":
            failure = (ready.get("kind", "bridge_failure"), ready.get("message"))
        else:
            record["agent"] = ready.get("agent")
            if spec.get("teacher_source") and isinstance(record["agent"], dict):
                record["agent"]["kind"] = spec["teacher_source"]  # training demonstration, never an agent result
            record["test_double"] = bool((ready.get("agent") or {}).get("test_double"))
            if ready.get("prompt"):
                _write_json(partial / "prompt.json", ready["prompt"])
        result = None
        while failure is None:
            seconds_left = env.seconds_left()
            if seconds_left <= 0:
                result = env.expire()
                break
            reply = bridge.send({"type": "observation", "observation": observation, "seconds_left": seconds_left})
            telemetry = reply.get("telemetry") or {}
            if telemetry:
                _append(partial / "transcript.jsonl", telemetry.get("transcript") or [])
                for call in telemetry.get("provider_calls") or []:
                    calls += 1
                    for key, name in (("input", "input_tokens"), ("output", "output_tokens"),
                                      ("cache_read", "cache_read_input_tokens"), ("cache_write", "cache_creation_input_tokens")):
                        tokens[key] += int((call.get("usage") or {}).get(name) or 0)
                    if call.get("cost_usd") is None and call.get("status") == "ok":
                        unpriced += 1
                    cost += float(call.get("cost_usd") or 0.0)
                    _append(partial / "provider_calls.jsonl", [call])
                if telemetry.get("memory") is not None:
                    _write_json(partial / "memory.json", telemetry["memory"])
            if reply.get("type", "action") != "action" or "action" not in reply:
                failure = (reply.get("kind", "bridge_failure"), reply.get("message") or "no action in the bridge reply")
                break
            action = reply["action"]
            step_started = clock()
            observation, reward, terminated, truncated, info = result = env.step(action)
            steps += 1
            _append(partial / "steps.jsonl", [{"index": steps, "action": action, "observation": observation,
                                               "reward": reward, "terminated": terminated, "truncated": truncated,
                                               "info": info, "env_seconds": round(clock() - step_started, 6)}])
            record["final_action"] = action
            if action.get("name") == "submit" and isinstance(action.get("args"), dict):
                record["final_answer"] = action["args"].get("findings")  # the findings.json object as sent
            if terminated or truncated:
                break
        if failure is not None:
            kind, message = failure
            result = env.expire() if kind == "timeout" else env.abort(f"{kind}: {message}")
        try:
            closing = bridge.send({"type": "close"})
            telemetry = closing.get("telemetry") or {}
            _append(partial / "transcript.jsonl", telemetry.get("transcript") or [])
        except Exception:  # a dead bridge cannot change the recorded outcome
            pass
    except Exception as error:
        failure = ("bridge_failure", f"{type(error).__name__}: {error}")
        try:
            env.abort(failure[1])
        except Exception:
            pass
    finally:
        bridge.close()
    trusted_result = env.trusted_result()
    env.close()
    env_status = trusted_result["status"] if trusted_result else None
    if failure is None:
        status = _status_from_env(env_status)
    else:
        status = failure[0] if failure[0] in OPERATIONAL_STATUSES else "bridge_failure"
    evaluable = status in EVALUABLE and trusted_result is not None
    evaluation = {"schema_version": ARTIFACT_SCHEMA_VERSION, "attempt_id": attempt_id, "status": status,
                  "env_status": env_status, "official": trusted_result["score"] if evaluable else None,
                  # agent-generalization-1.1 scores EVERY attempt by the official rule (failures as the
                  # environment graded them: empty findings). Kept apart from "official", never merged.
                  "official_by_rule": trusted_result["score"] if trusted_result else None,
                  "not_evaluable_reason": None if evaluable else f"attempt ended with {status}",
                  "inspection_steps": trusted_result["inspection_steps"] if trusted_result else None,
                  "internal_score": (trusted_result.get("evaluation") or {}).get("score") if trusted_result else None,
                  "matches": (trusted_result.get("evaluation") or {}).get("matches") if trusted_result else None,
                  "unmatched": (trusted_result.get("evaluation") or {}).get("unmatched") if trusted_result else None,
                  "truth_issue_count": _truth_count(trusted_result)}
    _write_json(trusted / "evaluation.json", evaluation)
    record["inspection_steps"] = evaluation["inspection_steps"]
    if env_status not in ("submitted", "invalid_submission"):
        record["final_answer"] = None  # a submit the environment never accepted is not an answer
    record.update(finished_at=_now(), status=status, status_detail=None if failure is None else failure[1],
                  totals={"environment_actions": steps, "provider_calls": calls, "tokens": tokens,
                          "cost_usd": round(cost, 6), "unpriced_calls": unpriced,
                          "wall_seconds": round(clock() - started, 3),
                          "inspection_steps": evaluation["inspection_steps"]})
    _write_json(partial / "attempt.json", record)
    partial.rename(final_dir)
    return record


def _truth_count(trusted_result: dict | None) -> int | None:
    score = ((trusted_result or {}).get("evaluation") or {}).get("score")
    return None if not score else int(score["tp"]) + int(score["fn"])


def finalize_interrupted(output_root: str | Path) -> list[str]:
    """Turn every attempts/<id>.partial directory into a finalized 'interrupted' record (paid calls kept)."""
    root = Path(output_root) / "attempts"
    done = []
    for partial in sorted(root.glob("*.partial")) if root.exists() else []:
        attempt_id = partial.name[: -len(".partial")]
        path = partial / "attempt.json"
        record = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"artifact_id": attempt_id}
        calls = [json.loads(line) for line in (partial / "provider_calls.jsonl").read_text(encoding="utf-8").splitlines()
                 if line.strip()] if (partial / "provider_calls.jsonl").exists() else []
        record.update(status="interrupted", status_detail="controller stopped before finalization; resumed later",
                      finished_at=_now(), totals={**(record.get("totals") or {}), "provider_calls": len(calls),
                                                  "cost_usd": round(sum(float(c.get("cost_usd") or 0) for c in calls), 6)})
        _write_json(path, record)
        target = root / attempt_id
        if target.exists():
            shutil.rmtree(partial)
            continue
        partial.rename(target)
        trusted = Path(output_root) / "trusted" / attempt_id
        trusted.mkdir(parents=True, exist_ok=True)
        if not (trusted / "evaluation.json").exists():
            _write_json(trusted / "evaluation.json", {"schema_version": ARTIFACT_SCHEMA_VERSION, "attempt_id": attempt_id,
                                                      "status": "interrupted", "official": None,
                                                      "not_evaluable_reason": "interrupted before finalization"})
        done.append(attempt_id)
    return done


def ensure_safe_output(root: Path) -> None:
    """Refuse output roots inside an episode directory (artifacts never mix with episode files)."""
    if any((parent / "private").is_dir() and (parent / "public").is_dir() for parent in [root, *root.parents]):
        raise ValueError("output directory must not be inside an episode directory")


__all__ = ["InProcessBridge", "PolicyBridge", "finalize_interrupted", "run_attempt", "public_digest", "source_sha",
           "ensure_safe_output", "OPERATIONAL_STATUSES"]
