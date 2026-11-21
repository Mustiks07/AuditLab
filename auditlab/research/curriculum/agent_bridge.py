"""Trusted adapter from the curriculum controller to Claude 1's #42 executor."""
from __future__ import annotations

import hashlib
import importlib
import math
import os
import re
import subprocess
from copy import deepcopy
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit
from uuid import uuid4

from auditlab.release.transforms import read_json, write_json

from .reduce import snapshot_sha256

CLAUDE_RUNNER_SHA = "61136963764f5d35124ea2177bbbe3d45b8ff932"
PROTOCOL_SECONDS = {"genesis-official-60s": 60.0, "auditlab-research-600s": 600.0}
_MICROS_PER_USD = 1_000_000


def _positive_int(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _money(value):
    try:
        amount = Decimal(str(value))
        if amount.is_finite() and amount >= 0:
            return amount
    except InvalidOperation:
        pass
    return None


def _bridge_failure(kind, reason):
    return {"type": "failure", "kind": kind, "message": reason, "telemetry": None}


class _BoundedBridge:
    """Apply per-attempt controller caps to Claude 1's isolated transport."""

    isolated = True

    def __init__(self, bridge, max_calls, max_calls_per_observation, deadline, clock):
        if getattr(bridge, "isolated", False) is not True:
            bridge.close()
            raise ValueError("the real attempt requires the isolated #42 transport")
        self.bridge = bridge
        self.remaining_calls = max_calls
        self.max_calls_per_observation = max_calls_per_observation
        self.deadline = deadline
        self.clock = clock
        self.unreported_usage = False
        self.known_cost = Decimal(0)
        self.observed_calls = 0

    def send(self, message):
        if message.get("type") != "observation":
            return self.bridge.send(message)
        seconds = min(message["seconds_left"], self.deadline - self.clock())
        if seconds < 1:
            return _bridge_failure("timeout", "curriculum wall-clock limit reached")
        if self.unreported_usage:
            return _bridge_failure("budget_stop", "provider usage unavailable after an observation")
        if self.remaining_calls < self.max_calls_per_observation:
            return _bridge_failure("budget_stop", "insufficient provider calls remaining for an observation")

        # After handing off an observation, a broken transport may hide billable calls.
        self.unreported_usage = True
        reply = self.bridge.send({**message, "seconds_left": seconds})
        telemetry = reply.get("telemetry") if isinstance(reply, dict) else None
        calls = telemetry.get("provider_calls") if isinstance(telemetry, dict) else None
        if not isinstance(calls, list):
            return reply
        self.remaining_calls -= len(calls)
        self.observed_calls += len(calls)
        if self.remaining_calls < 0 or len(calls) > self.max_calls_per_observation:
            return _bridge_failure("budget_stop", "bridge exceeded its reserved provider calls")
        self.unreported_usage = False
        for call in calls:
            amount = _money(call.get("cost_usd")) if isinstance(call, dict) and call.get("status") == "ok" else None
            if amount is None:
                self.unreported_usage = True
            else:
                self.known_cost += amount
        return reply

    def close(self):
        self.bridge.close()

    def cost_micros(self):
        if self.unreported_usage:
            return None
        return int((self.known_cost * _MICROS_PER_USD).to_integral_value(rounding=ROUND_CEILING))


def _claude_public_digest(snapshot):
    digest = hashlib.sha256()
    public = snapshot / "public"
    for file in sorted(path for path in public.rglob("*") if path.is_file()):
        digest.update(file.relative_to(public).as_posix().encode() + b"\0" + file.read_bytes() + b"\0")
    return digest.hexdigest()


def _failure_assessment(snapshot, record, evaluation):
    truth = read_json(snapshot / "private/truth.json")
    matches = {issue_id for _, issue_id in evaluation["matches"]}
    issues = {issue["issue_id"]: issue for issue in truth["issues"]}
    findings = record["final_answer"]["findings"]
    score = evaluation["internal_score"]
    if not isinstance(score, dict) or not matches.issubset(issues):
        raise ValueError("trusted evaluation does not match the snapshot")
    failures = [{"reason": "missed_issue", "type": issue["type"], "affected_ids": issue["affected_ids"],
                 "issue_id": issue_id} for issue_id, issue in issues.items() if issue_id not in matches]
    for index, reason in evaluation["unmatched"].items():
        finding = findings[int(index)]
        failures.append({"reason": "false_positive", "type": finding["type"],
                         "affected_ids": finding["affected_ids"], "diagnostic": reason})
    return failures, score


class ClaudeAttemptRunner:
    """Adapt a trusted #42 executor to ``investigate(snapshot, out, repetition, seconds)``."""

    def __init__(self, config, source_sha, execute, bridge_factory, env_factory, *, clock=monotonic):
        self.config = deepcopy(config)
        self.source_sha = source_sha
        self.protocol_id = config["protocol_id"]
        if self.protocol_id not in PROTOCOL_SECONDS:
            raise ValueError("unknown Claude 1 runner protocol")
        self.max_cost = _positive_int(config["max_attempt_cost_micros"], "max_attempt_cost_micros")
        self.max_calls = _positive_int(config["max_provider_calls"], "max_provider_calls")
        self.max_seconds = config["max_wall_seconds"]
        if isinstance(self.max_seconds, bool) or not (0 < self.max_seconds <= PROTOCOL_SECONDS[self.protocol_id]):
            raise ValueError("max_wall_seconds must be within the selected protocol")
        self.seed_base = config.get("seed_base", 41000)
        if type(self.seed_base) is not int:
            raise ValueError("seed_base must be an integer")
        agent = self.config["agent"]
        if agent.get("kind") == "baseline_policy" or self.config["provider"].get("provider") == "test_double":
            raise ValueError("the Claude 1 bridge requires a real API agent configuration")
        agent.setdefault("max_model_calls_per_action", 1)
        retry = agent.setdefault("retry", {})
        retry.setdefault("max_tries", 1)
        self.calls_per_observation = (
            _positive_int(agent["max_model_calls_per_action"], "max_model_calls_per_action")
            * _positive_int(retry["max_tries"], "max_tries")
        )
        if self.calls_per_observation > self.max_calls:
            raise ValueError("provider call cap cannot cover one configured observation")
        self.execute = execute
        self.bridge_factory = bridge_factory
        self.env_factory = env_factory
        self.clock = clock
        self._families = {}

    def metadata(self):
        return {"kind": "claude_api", "runner_sha": CLAUDE_RUNNER_SHA, "protocol_id": self.protocol_id,
                **{name: deepcopy(self.config[name]) for name in ("agent", "provider", "docker")},
                "max_attempt_cost_micros": self.max_cost, "max_provider_calls": self.max_calls,
                "max_wall_seconds": self.max_seconds, "max_calls_per_observation": self.calls_per_observation,
                "seed_base": self.seed_base}

    def _family(self, snapshot, snapshot_hash):
        candidate_receipt = snapshot.parent / "candidate.json"
        if candidate_receipt.is_file():
            family = read_json(candidate_receipt)["candidate"]["family_id"]
        else:
            provenance = read_json(snapshot / "private/provenance.json")
            parent_hash = (provenance.get("reduction") or {}).get("parent_snapshot_sha256")
            family = self._families.get(parent_hash, provenance.get("family_id", parent_hash or snapshot_hash))
        self._families[snapshot_hash] = family
        return family

    def __call__(self, snapshot, output, repetition, seconds):
        if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("attempt time must be positive and finite")
        snapshot, output = Path(snapshot).resolve(), Path(output).resolve()
        if output == snapshot or snapshot in output.parents:
            raise ValueError("attempt output cannot be inside its snapshot")
        output.mkdir(parents=True, exist_ok=False)
        run_id = f"claude-{uuid4().hex}"
        snapshot_hash = snapshot_sha256(snapshot)
        limit = min(self.max_seconds, seconds)
        start = self.clock()
        raw = output / "claude"
        seed = self.seed_base + repetition
        family = self._family(snapshot, snapshot_hash)
        spec = {"attempt_id": run_id, "episode_ref": str(snapshot), "episode": {"family_id": family, "split": "train"},
                "variant": self.config["agent"].get("variant", "tools"), "agent": deepcopy(self.config["agent"]),
                "provider": deepcopy(self.config["provider"]), "protocol_id": self.protocol_id,
                "repetition": repetition, "seed": seed, "budget_usd": self.max_cost / _MICROS_PER_USD}
        record_path = raw / "attempts" / run_id / "attempt.json"
        evaluation_path = raw / "trusted" / run_id / "evaluation.json"
        receipt = {"schema_version": "1.0", "artifact_id": run_id, "attempt_id": run_id,
                   "source_sha": self.source_sha, "protocol_id": self.protocol_id, "kind": "claude_api",
                   "runner_sha": CLAUDE_RUNNER_SHA, "snapshot_sha256": snapshot_hash, "family_id": family,
                   "artifact_ref": str(output / "attempt.json"), "repetition": repetition, "seed": seed,
                   "status": "interrupted", "raw_status": None, "reason": "attempt_in_progress", "launches": None,
                   "max_elapsed_seconds": limit, "failures": None, "score": None, "cost_micros": None,
                   "provider_calls": None, "tokens": None, "test_double": None}
        write_json(receipt["artifact_ref"], receipt)
        bounded = None

        def make_bridge():
            nonlocal bounded
            bounded = _BoundedBridge(self.bridge_factory(), self.max_calls, self.calls_per_observation,
                                     start + limit, self.clock)
            return bounded

        try:
            record = self.execute(spec, raw, make_bridge, env_factory=lambda: self.env_factory(time_limit_s=limit))
            if record["attempt_id"] != run_id or record["episode"]["episode_sha256"] != _claude_public_digest(snapshot):
                raise ValueError("runner record refers to a different episode or attempt")
            totals = record["totals"]
            count = totals.get("provider_calls")
            known_cost = bounded.cost_micros() if bounded is not None else None
            accounted = bounded is not None and count == bounded.observed_calls and totals.get("unpriced_calls") == 0
            receipt.update(status=record["status"], raw_status=record["status"], reason=record.get("status_detail"),
                           cost_micros=known_cost if accounted else None, reported_cost_usd=totals.get("cost_usd"),
                           provider_calls=count, tokens=totals.get("tokens"), launches=1,
                           actions=totals.get("environment_actions"), test_double=record.get("test_double"))
            evaluation = read_json(evaluation_path)
            receipt.update(env_status=evaluation.get("env_status"), official_score=evaluation.get("official"))
            if record.get("test_double") is not False or record.get("isolated_bridge") is not True or bounded is None:
                receipt.update(status="not_checked", reason="runner_did_not_confirm_real_isolated_agent")
            elif type(count) is not int or count < 0 or count > self.max_calls:
                receipt.update(status="budget_stop", reason="runner_provider_call_limit_not_verified", cost_micros=None)
            elif record["status"] == "completed":
                if evaluation.get("env_status") == "submitted" and count > 0:
                    failures, score = _failure_assessment(snapshot, record, evaluation)
                    receipt.update(status="submitted", failures=failures, score=score)
                else:
                    receipt.update(status=evaluation.get("env_status") or "invalid_response",
                                   reason="no_valid_submission_with_provider_calls")
        except (KeyError, IndexError, AttributeError, OSError, RuntimeError, TypeError, ValueError) as error:
            receipt.update(status="provider_error", reason=f"claude_runner_or_artifact_error:{type(error).__name__}")
        receipt.update(elapsed_seconds=self.clock() - start,
                       raw_attempt_ref=str(record_path) if record_path.is_file() else None,
                       raw_evaluation_ref=str(evaluation_path) if evaluation_path.is_file() else None)
        write_json(receipt["artifact_ref"], receipt)
        return receipt


def _load_claude_checkout(path):
    checkout = Path(path).resolve(strict=True)
    revision = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    status = subprocess.check_output(["git", "-C", str(checkout), "status", "--porcelain", "--",
                                      "auditlab/research/agents"], text=True)
    if revision != CLAUDE_RUNNER_SHA or status:
        raise ValueError(f"the Claude 1 agent checkout must be clean at {CLAUDE_RUNNER_SHA}")
    research = importlib.import_module("auditlab.research")
    directory = checkout / "auditlab/research"
    if str(directory) not in research.__path__:
        research.__path__.append(str(directory))
    modules = [importlib.import_module(f"auditlab.research.agents.{name}") for name in ("execute", "transport")]
    if any(not Path(module.__file__).resolve().is_relative_to(directory) for module in modules):
        raise ValueError("loaded Claude 1 runner does not come from the pinned checkout")
    return modules


def make_claude_runner(config, source_sha):
    """Build an explicitly bounded runner without opening a provider connection."""
    provider = config["provider"]
    key_env = provider.get("api_key_env")
    if provider.get("provider") not in {"anthropic", "openai_compatible"}:
        raise ValueError("configure an installed Claude 1 API provider")
    if not isinstance(key_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
        raise ValueError("provider.api_key_env must name an environment variable")
    if {"api_key", "authorization", "token", "headers"} & provider.keys():
        raise ValueError("provider configuration must use an environment variable for credentials")
    base_url = urlsplit(provider.get("base_url") or "")
    if base_url.username or base_url.password or base_url.query:
        raise ValueError("provider base_url must not embed credentials")
    if not os.environ.get(key_env):
        raise ValueError(f"provider environment variable {key_env} is unset")
    docker = config["docker"]
    if not docker.get("image"):
        raise ValueError("configure a preloaded Claude 1 Docker bridge image")
    execute, transport = _load_claude_checkout(config["runner_checkout"])
    import judge_adapter

    return ClaudeAttemptRunner(config, source_sha, execute.run_attempt,
                               lambda: transport.docker_bridge(docker, provider), judge_adapter.make_env)
