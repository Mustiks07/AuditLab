"""Experiment plans, resumable execution under a spend limit, and an internal summary.

A plan (experiment.json) fixes episodes with family/split, arms (agent + provider config), repetitions,
protocol, transport and budget BEFORE any result exists; its sha256 is stored with every run. Resume:
finalized attempts are never re-run (no second payment); an interrupted attempt stays in the record
as "interrupted" with its paid calls, and its slot is retried under a NEW attempt id.

The summary is AuditLab's own bookkeeping with explicit denominators. The independent comparison
(paired intervals by family, final holdout) belongs to Claude Pro 2's protocol; attempts are exported
in the CONTRACTS attempt.json shape for it. Test-double runs are marked and are never an experiment.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from auditlab.research.agents.execute import (SERVICE_STATUSES, InProcessBridge, PolicyBridge, ensure_safe_output,
                                              finalize_interrupted, run_attempt, source_sha)
from auditlab.research.agents.pricing import price_for
from auditlab.research.agents.protocol import ARTIFACT_SCHEMA_VERSION, get_protocol

FORBIDDEN_SPLITS_FOR_SELECTION = ("final", "test", "holdout")
# Service outcomes (HTTP 429, provider/bridge failures) are retried at the SLOT level with a new attempt id:
# finitely, after a cooldown, every failed attempt kept. Defaults are conservative; a plan may override them.
SERVICE_DEFAULTS = {"max_slot_retries": 2, "cooldown_s": 60.0, "max_cooldown_s": 600.0,
                    "max_consecutive_service_failures": 3, "inter_attempt_cooldown_s": 0.0}
RETRIED_SERVICE_STATUSES = ("rate_limited", "quota_exhausted", "provider_error", "bridge_failure", "interrupted")
TRANSPORTS = ("in_process", "subprocess", "docker")


class PlanError(ValueError):
    pass


def load_plan(path: str | Path) -> tuple[dict, str]:
    raw = Path(path).read_bytes()
    plan = json.loads(raw)
    validate_plan(plan)
    return plan, hashlib.sha256(raw).hexdigest()


def validate_plan(plan: dict) -> None:
    for key in ("schema_version", "artifact_id", "protocol_id", "episodes", "arms", "repetitions"):
        if key not in plan:
            raise PlanError(f"plan misses {key}")
    if plan['schema_version'] != '1.0':
        raise PlanError('unsupported schema version')
    for ident in [plan['artifact_id'], *[a.get('name') for a in plan['arms']]]:
        if not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}',ident):
            raise PlanError('plan and arm identifiers must be portable names')
    for key,value in (plan.get('budget') or {}).items():
        if key.startswith('max_usd') and (type(value) not in (int,float) or not math.isfinite(value) or value < 0):
            raise PlanError('spend limits must be finite non-negative numbers')
    protocol = get_protocol(plan["protocol_id"])
    if not plan["episodes"] or not plan["arms"]:
        raise PlanError("plan needs episodes and arms")
    names = [arm["name"] for arm in plan["arms"]]
    if len(set(names)) != len(names):
        raise PlanError("arm names must be unique")
    for episode in plan["episodes"]:
        for key in ("episode_ref", "family_id", "split"):
            if key not in episode:
                raise PlanError(f"episode misses {key}")
        if not (Path(episode["episode_ref"]) / "public" / "manifest.json").is_file():
            raise PlanError(f"episode {episode['episode_ref']} has no public/manifest.json")
    if plan.get("purpose") == "selection" and any(e["split"] in FORBIDDEN_SPLITS_FOR_SELECTION for e in plan["episodes"]):
        raise PlanError("a selection/tuning plan may not use final/test/holdout episodes")
    transport = plan.get("transport", "in_process")
    if transport not in TRANSPORTS:
        raise PlanError(f"transport must be one of {TRANSPORTS}")
    llm = [arm for arm in plan["arms"] if arm.get("agent", {}).get("kind", "llm") == "llm"]
    if protocol.official and llm and transport == "in_process":
        raise PlanError("the official protocol needs an isolated bridge transport (subprocess is development only, "
                        "docker for official runs)")
    if type(plan["repetitions"]) is not int or plan["repetitions"] < 1:
        raise PlanError("repetitions must be a positive integer")
    if plan.get("slot_order", "arm_major") not in ("arm_major", "balanced"):
        raise PlanError("slot_order must be arm_major or balanced")


def is_paid_arm(arm: dict) -> bool:
    agent = arm.get("agent") or {}
    return agent.get("kind", "llm") == "llm" and (arm.get("provider") or {}).get("provider") != "test_double"


def attempt_slots(plan: dict) -> list[dict]:
    slots = []
    arms, episodes = plan["arms"], plan["episodes"]
    if plan.get("slot_order", "arm_major") == "balanced":
        # Every case/repetition visits all arms, rotating who goes first. No outcome-dependent order.
        schedule = ((arms[(offset + index + repetition) % len(arms)], episode, repetition)
                    for repetition in range(plan["repetitions"])
                    for index, episode in enumerate(episodes) for offset in range(len(arms)))
    else:
        schedule = ((arm, episode, repetition) for arm in arms for episode in episodes
                    for repetition in range(plan["repetitions"]))
    for arm, episode, repetition in schedule:
        key = hashlib.sha256(json.dumps([plan["artifact_id"], arm["name"], episode["episode_ref"], repetition])
                             .encode()).hexdigest()[:16]
        slots.append({"slot": f"{arm['name']}--{key}", "arm": arm, "episode": episode, "repetition": repetition})
    return slots


def dry_run(plan: dict, plan_sha: str) -> dict:
    """No network, no environment: what would run, what it may cost at most, what is missing."""
    slots = attempt_slots(plan)
    paid = [slot for slot in slots if is_paid_arm(slot["arm"])]
    budget = plan.get("budget") or {}
    per_attempt = budget.get("max_usd_per_attempt")
    missing = []
    for arm in {slot["arm"]["name"]: slot["arm"] for slot in paid}.values():
        provider = arm.get("provider") or {}
        if price_for(provider.get("model", ""), provider.get("prices")) is None:
            missing.append(f"arm {arm['name']}: no price for model {provider.get('model')!r}")
        missing.append(f"arm {arm['name']}: secret variable {provider.get('api_key_env', 'ANTHROPIC_API_KEY')} must be set "
                       f"in the controller environment (checked at run time, never stored)")
    if paid and not (budget.get("max_usd_total") or 0) > 0:
        missing.append("budget.max_usd_total must be a positive spend limit")
    if paid and not (per_attempt or 0) > 0:
        missing.append("budget.max_usd_per_attempt must be positive")
    return {"plan_sha256": plan_sha, "protocol_id": plan["protocol_id"], "attempts": len(slots), "paid_attempts": len(paid),
            "worst_case_usd": None if not paid or not per_attempt else per_attempt * len(paid),
            "max_usd_total": budget.get("max_usd_total"), "missing_conditions": missing, "network_calls": 0}


def _bridge_factory(plan: dict, arm: dict, repetition: int, provider_override=None) -> Callable:
    agent = arm.get("agent") or {}
    if agent.get("kind") == "baseline_policy":
        return lambda: PolicyBridge(agent["policy"], int(agent.get("seed", 0)) + repetition)
    transport = plan.get("transport", "in_process")
    if transport == "in_process":
        return lambda: InProcessBridge(provider_override)
    from auditlab.research.agents import transport as transports
    if transport == "subprocess":
        return lambda: transports.subprocess_bridge(arm.get("provider") or {})
    return lambda: transports.docker_bridge(plan.get("docker") or {}, arm.get("provider") or {})


def _spent(out: Path) -> float:
    total = 0.0
    for path in (out / "attempts").glob("*/attempt.json") if (out / "attempts").exists() else []:
        totals = json.loads(path.read_text(encoding="utf-8")).get("totals") or {}
        total += float(totals.get("budget_debit_usd", totals.get("cost_usd")) or 0.0)
    return total


def _service_settings(plan: dict) -> dict:
    settings = {**SERVICE_DEFAULTS, **(plan.get("service") or {})}
    for key in ("max_slot_retries", "max_consecutive_service_failures"):
        if type(settings[key]) is not int or settings[key] < 0:
            raise PlanError(f"service.{key} must be a non-negative integer")
    for key in ("cooldown_s", "max_cooldown_s", "inter_attempt_cooldown_s"):
        if type(settings[key]) not in (int, float) or not math.isfinite(settings[key]) or settings[key] < 0:
            raise PlanError(f"service.{key} must be a finite non-negative number")
    return settings


def _retry_after_of(out: Path, attempt_id: str) -> float | None:
    """Largest provider Retry-After seen in the attempt's failed calls (None if the provider gave none)."""
    path = out / "attempts" / attempt_id / "provider_calls.jsonl"
    waits = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                value = ((json.loads(line).get("error") or {}).get("retry_after"))
                if isinstance(value, (int, float)):
                    waits.append(float(value))
    return max(waits) if waits else None


def _record_invocation(out: Path, entry: dict) -> None:
    """Every run invocation (with its stop reason) is appended to run_manifest.json; nothing is overwritten."""
    path = out / "run_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.setdefault("invocations", []).append(entry)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n")


def run_experiment(plan: dict, plan_sha: str, out: str | Path, *, confirm_paid: bool = False, provider_override=None,
                   env_factory=None, log: Callable[[str], None] = print, stop_requested: Callable[[], bool] | None = None,
                   sleep: Callable[[float], None] = time.sleep) -> dict:
    out = Path(out)
    ensure_safe_output(out.resolve())
    slots = attempt_slots(plan)
    paid_arms = {slot["arm"]["name"]: slot["arm"] for slot in slots if is_paid_arm(slot["arm"])}
    if paid_arms:  # refuse before anything is created: no access, no limit or no confirmation -> no paid run
        problems = [m for m in dry_run(plan, plan_sha)["missing_conditions"] if "checked at run time" not in m]
        problems += [f"arm {name}: secret variable {(arm.get('provider') or {}).get('api_key_env', 'ANTHROPIC_API_KEY')} "
                     f"is not set" for name, arm in paid_arms.items()
                     if not os.environ.get((arm.get("provider") or {}).get("api_key_env", "ANTHROPIC_API_KEY"))]
        if problems or not confirm_paid:
            raise PlanError("paid run refused: " + "; ".join(problems + ([] if confirm_paid else ["--confirm-paid not given"])))
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "run_manifest.json"
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous["plan_sha256"] != plan_sha:
            raise PlanError("this output directory belongs to a different plan; a changed plan needs a new directory")
    else:
        manifest_path.write_text(json.dumps({"schema_version": ARTIFACT_SCHEMA_VERSION, "plan_artifact_id": plan["artifact_id"],
                                             "plan_sha256": plan_sha, "plan": plan}, indent=2, ensure_ascii=False),
                                 encoding="utf-8", newline="\n")
    service = _service_settings(plan)
    invocation = {"started_at": datetime.now(timezone.utc).isoformat(), "source_sha": source_sha(),
                  "attempts_started": [], "service_waits_s": [], "inter_attempt_waits_s": [],
                  "stop_reason": None, "stop_detail": None}
    interrupted = finalize_interrupted(out)
    if interrupted:
        log(f"finalized {len(interrupted)} interrupted attempt(s); their slots are retried with new ids")
        invocation["interrupted_finalized"] = interrupted
    budget = plan.get("budget") or {}
    done = {path.parent.name for path in (out / "attempts").glob("*/attempt.json")} if (out / "attempts").exists() else set()
    consecutive_service = 0
    queue = list(slots)
    while queue and invocation["stop_reason"] is None:
        slot = queue[0]
        if stop_requested and stop_requested():
            invocation.update(stop_reason="stop_requested", stop_detail="stop requested by the controller")
            break
        evaluated = sorted(name for name in done if name == slot["slot"] or name.startswith(slot["slot"] + "--retry"))
        statuses = [json.loads((out / "attempts" / name / "attempt.json").read_text(encoding="utf-8"))["status"]
                    for name in evaluated]
        if any(status not in RETRIED_SERVICE_STATUSES for status in statuses):
            queue.pop(0)  # an auditor outcome exists: never re-run (and never pay again for) a finished slot
            continue
        service_failures = sum(status != "interrupted" for status in statuses)
        if service_failures > service["max_slot_retries"]:
            log(f"{slot['slot']}: {service_failures} service failures; slot left unmeasured (kept in the record)")
            queue.pop(0)
            continue
        wait = 0.0
        wait_kind = "service_waits_s"
        if service_failures and evaluated[-1] in invocation["attempts_started"]:
            # cool down before retrying a slot the service refused in THIS run (a later resume starts fresh)
            wait = min(service["max_cooldown_s"], max(service["cooldown_s"], _retry_after_of(out, evaluated[-1]) or 0.0))
        if invocation["attempts_started"] and service["inter_attempt_cooldown_s"] > wait:
            wait = service["inter_attempt_cooldown_s"]
            wait_kind = "inter_attempt_waits_s"
        if wait:
            # One actual wait, even when both policies require a cooldown. Do not double-count it.
            invocation[wait_kind].append(wait)
            log(f"{slot['slot']}: waiting {wait:.0f}s ({wait_kind}, outside the episode deadline)")
            sleep(wait)
        if stop_requested and stop_requested():
            invocation.update(stop_reason="stop_requested", stop_detail="stop requested during cooldown")
            break
        attempt_id = slot["slot"] if not evaluated else f"{slot['slot']}--retry{len(evaluated)}"
        per_attempt = None
        if is_paid_arm(slot["arm"]):
            remaining = float(budget["max_usd_total"]) - _spent(out)
            per_attempt = min(float(budget["max_usd_per_attempt"]), remaining)
            if per_attempt <= 0:
                log("spend limit reached; stopping before the next paid attempt")
                invocation.update(stop_reason="spend_limit", stop_detail="budget.max_usd_total is used up")
                break
        arm, episode = slot["arm"], slot["episode"]
        spec = {"attempt_id": attempt_id, "episode_ref": episode["episode_ref"],
                "episode": {k: episode[k] for k in episode if k != "episode_ref"}, "variant": arm["name"],
                "agent": arm.get("agent") or {}, "provider": arm.get("provider") or {}, "protocol_id": plan["protocol_id"],
                "repetition": slot["repetition"], "seed": int(plan.get("env_seed", 0)), "budget_usd": per_attempt}
        record = run_attempt(spec, out, _bridge_factory(plan, arm, slot["repetition"], provider_override),
                             env_factory=env_factory)
        done.add(attempt_id)
        invocation["attempts_started"].append(attempt_id)
        log(f"{attempt_id}: {record['status']} actions={record['totals']['environment_actions']} "
            f"cost={record['totals']['cost_usd']}")
        if record["status"] == "quota_exhausted":
            invocation.update(stop_reason="provider_quota_exhausted",
                              stop_detail=f"{attempt_id}: {record.get('status_detail')}")
        elif record["status"] in SERVICE_STATUSES:
            consecutive_service += 1
            if consecutive_service >= service["max_consecutive_service_failures"] > 0:
                invocation.update(stop_reason="consecutive_service_failures",
                                  stop_detail=f"{consecutive_service} service failures in a row; last {attempt_id}: "
                                              f"{record.get('status_detail')}")
        else:
            consecutive_service = 0
    if invocation["stop_reason"] is None:
        invocation["stop_reason"] = "plan_finished" if not queue else "stopped"
    invocation["finished_at"] = datetime.now(timezone.utc).isoformat()
    _record_invocation(out, invocation)
    log(f"stop reason: {invocation['stop_reason']}" + (f" ({invocation['stop_detail']})" if invocation["stop_detail"] else ""))
    summary = summarize(out, plan, plan_sha)
    summary["last_invocation"] = invocation
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8", newline="\n")
    return summary


def _load_attempts(out: Path) -> list[dict]:
    rows = []
    for path in sorted((out / "attempts").glob("*/attempt.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        evaluation_path = out / "trusted" / record["artifact_id"] / "evaluation.json"
        record["_evaluation"] = json.loads(evaluation_path.read_text(encoding="utf-8")) if evaluation_path.exists() else None
        rows.append(record)
    return rows


def _dist(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": None, "median": None, "min": None, "max": None}
    return {"n": len(values), "mean": statistics.fmean(values), "median": statistics.median(values),
            "min": min(values), "max": max(values)}


def summarize(out: str | Path, plan: dict, plan_sha: str) -> dict:
    out = Path(out)
    attempts = _load_attempts(out)
    arms = {}
    for arm in plan["arms"]:
        rows = [a for a in attempts if a.get("variant") == arm["name"]]
        scored = [a for a in rows if (a["_evaluation"] or {}).get("official") and a["_evaluation"]["official"]["tp"] is not None]
        tp = sum(a["_evaluation"]["official"]["tp"] for a in scored)
        fp = sum(a["_evaluation"]["official"]["fp"] for a in scored)
        fn = sum(a["_evaluation"]["official"]["fn"] for a in scored)
        evaluable = [a for a in rows if (a["_evaluation"] or {}).get("official")]
        clean = [a for a in evaluable if a["_evaluation"].get("truth_issue_count") == 0]
        statuses = {}
        for a in rows:
            statuses[a["status"]] = statuses.get(a["status"], 0) + 1
        arms[arm["name"]] = {
            "attempts": len(rows), "statuses": statuses, "evaluable": len(evaluable),
            "not_evaluable": len(rows) - len(evaluable),
            "invalid_submissions": sum(1 for a in evaluable if a["_evaluation"]["official"]["tp"] is None),
            "micro": {"tp": tp, "fp": fp, "fn": fn, "f1": None if not scored else (1.0 if 2 * tp + fp + fn == 0 else 2 * tp / (2 * tp + fp + fn))},
            "return": _dist([a["_evaluation"]["official"]["return"] for a in evaluable]),
            "clean_attempts": len(clean),
            "clean_with_fp": sum(1 for a in clean if (a["_evaluation"]["official"].get("fp") or 0) > 0 or
                                 a["_evaluation"]["official"]["tp"] is None),
            "actions": _dist([a["totals"].get("environment_actions", 0) for a in rows]),
            "provider_calls": _dist([a["totals"].get("provider_calls", 0) for a in rows]),
            "tokens_total": {k: sum((a["totals"].get("tokens") or {}).get(k, 0) for a in rows)
                             for k in ("input", "output", "cache_read", "cache_write")},
            "wall_seconds": _dist([a["totals"].get("wall_seconds", 0.0) for a in rows]),
            "cost_usd_total": None if any(a["totals"].get("unpriced_calls", 0) for a in rows) else round(sum(a["totals"].get("cost_usd") or 0.0 for a in rows), 6),
            "unpriced_calls": sum(a["totals"].get("unpriced_calls", 0) for a in rows),
            "test_double": any(a.get("test_double") for a in rows),
            **_content_breakdown(rows),
        }
    comparisons = [paired_by_family(attempts, pair[0], pair[1], seed=int((plan.get("analysis") or {}).get("seed", 0)),
                                    resamples=int((plan.get("analysis") or {}).get("bootstrap", 2000)))
                   for pair in (plan.get("analysis") or {}).get("pairs", [])]
    test_double = any(arm["test_double"] for arm in arms.values())
    return {"schema_version": ARTIFACT_SCHEMA_VERSION, "kind": "internal_summary", "plan_artifact_id": plan["artifact_id"],
            "plan_sha256": plan_sha, "protocol_id": plan["protocol_id"], "arms": arms, "paired": comparisons,
            "attempts_total": len(attempts), "test_double": test_double, "is_experiment": not test_double,
            "note": "AuditLab internal bookkeeping; independent comparison and final holdout belong to Claude Pro 2. "
                    "Means over evaluable attempts; not_evaluable attempts are listed, never dropped."}


def _content_breakdown(rows: list[dict]) -> dict:
    """Service outcomes vs content outcomes, and what the content errors were (from trusted diagnoses)."""
    service = [a for a in rows if a["status"] in SERVICE_STATUSES]
    content = [a for a in rows if a["status"] not in SERVICE_STATUSES]
    diagnoses = [(a["_evaluation"] or {}).get("diagnosis") for a in content]
    diagnoses = [d for d in diagnoses if d]
    missed, labels = {}, {}
    for d in diagnoses:
        for key, value in d["missed_by_type"].items():
            missed[key] = missed.get(key, 0) + value
        for key, value in d["finding_labels"].items():
            labels[key] = labels.get(key, 0) + value
    clean = [d for d in diagnoses if d["clean"]]
    error = [d for d in diagnoses if not d["clean"]]
    return {"service_outcomes": {"attempts": len(service), "statuses": _count_statuses(service)},
            "content_outcomes": {"attempts": len(content), "diagnosed": len(diagnoses),
                                 "submitted": sum(1 for a in content if a["status"] == "completed"),
                                 "error_variant_attempts": len(error),
                                 "error_attempts_with_a_miss": sum(1 for d in error if any(not i["found"] for i in d["issues"])),
                                 "issues_total": sum(d["truth_issues"] for d in error),
                                 "issues_missed": sum(1 for d in error for i in d["issues"] if not i["found"]),
                                 "missed_by_type": dict(sorted(missed.items())),
                                 "clean_variant_attempts": len(clean),
                                 "clean_attempts_with_false_accusation": sum(
                                     1 for d in clean if d["finding_labels"].get("false_accusation_on_clean")),
                                 "finding_labels": dict(sorted(labels.items()))}}


def _count_statuses(rows: list[dict]) -> dict:
    counts: dict = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return dict(sorted(counts.items()))


def paired_by_family(attempts: list[dict], arm_a: str, arm_b: str, *, seed: int = 0, resamples: int = 2000) -> dict:
    """Mean return difference A-B per family (attempts and clean/error variants averaged inside a family),
    with a percentile bootstrap over families. Families missing an evaluable attempt in either arm are listed."""
    def by_family(arm: str) -> dict:
        groups: dict[str, list[float]] = {}
        for a in attempts:
            official = (a["_evaluation"] or {}).get("official")
            if a.get("variant") == arm and official:
                groups.setdefault(a["episode"].get("family_id"), []).append(official["return"])
        return {family: statistics.fmean(values) for family, values in groups.items()}
    left, right = by_family(arm_a), by_family(arm_b)
    families = sorted(set(left) & set(right))
    diffs = [left[f] - right[f] for f in families]
    result = {"a": arm_a, "b": arm_b, "metric": "return", "families": len(families),
              "missing_families": sorted(set(left) ^ set(right)), "mean_difference": statistics.fmean(diffs) if diffs else None,
              "ci95": None}
    if len(diffs) >= 2:
        rng = random.Random(seed)
        means = sorted(statistics.fmean(rng.choice(diffs) for _ in diffs) for _ in range(resamples))
        result["ci95"] = [means[int(0.025 * resamples)], means[int(0.975 * resamples) - 1]]
    return result


def export_attempts(out: str | Path, target: str | Path) -> int:
    """Copy finalized attempts (public parts) into a new directory for the independent protocol."""
    import shutil
    out, target = Path(out), Path(target)
    target.mkdir(parents=True, exist_ok=False)
    count = 0
    for path in sorted((out / "attempts").glob("*/attempt.json")):
        shutil.copytree(path.parent, target / path.parent.name)
        count += 1
    return count


REVIEW_STATUS = {"completed": "completed", "timeout": "timeout", "action_budget_exhausted": "timeout",
                 "budget_stop": "budget_stop", "provider_error": "provider_error", "provider_refusal": "provider_error",
                 "invalid_response": "invalid_response", "rate_limited": "provider_error",
                 "quota_exhausted": "provider_error"}


def export_for_review(out: str | Path, target: str | Path, plan: dict, comparisons: list[dict] | None = None) -> dict:
    """Write the experiment in the layout of Claude Pro 2's compare_experiment (agent-generalization-1.1):
    experiment.json {protocol_id, source_sha, comparisons, episodes{key: {family_id, variant, episode_dir}}} and
    attempts/<id>.json with the fields that scorer reads. The reviewer re-scores from final_answer and truth.

    Status mapping (recorded per attempt as auditlab_status): action budget exhaustion is scored like a timeout
    (empty findings); a refusal is a provider error; a bridge/controller failure before the agent's first action
    is infra_error_before_first_action, later it is a provider_error. Interrupted attempts are exported too.
    """
    out, target = Path(out), Path(target)
    (target / "attempts").mkdir(parents=True, exist_ok=False)
    episodes, written = {}, 0
    for record in _load_attempts(out):
        episode = record["episode"]
        key = f"{episode.get('family_id')}:{episode.get('variant') or episode.get('label') or 'episode'}:{Path(episode['episode_ref']).name}"
        variant = episode.get("variant") or episode.get("label")
        if variant is None and record["_evaluation"] and record["_evaluation"].get("truth_issue_count") is not None:
            variant = "clean" if record["_evaluation"]["truth_issue_count"] == 0 else "error"  # trusted-side label
        episodes[key] = {"family_id": episode.get("family_id"), "variant": variant, "episode_dir": episode["episode_ref"]}
        status = record["status"]
        actions = (record.get("totals") or {}).get("environment_actions", 0)
        review_status = REVIEW_STATUS.get(status) or ("infra_error_before_first_action" if actions == 0 else "provider_error")
        tokens = (record.get("totals") or {}).get("tokens") or {}
        agent = record.get("agent") or {}
        attempt = {"attempt_id": record["attempt_id"], "arm": record.get("variant"), "episode_key": key,
                   "repetition": record.get("repetition", 0), "status": review_status, "auditlab_status": status,
                   "final_answer": record.get("final_answer"), "inspection_steps": record.get("inspection_steps"),
                   "actions": actions, "input_tokens": tokens.get("input", 0) + tokens.get("cache_read", 0) + tokens.get("cache_write", 0)
                   if agent.get("provider") else None, "output_tokens": tokens.get("output") if agent.get("provider") else None,
                   "wall_time_s": (record.get("totals") or {}).get("wall_seconds"), "cost_usd": (record.get("totals") or {}).get("cost_usd"),
                   "live": bool(agent.get("provider")) and not record.get("test_double"),
                   "test_double": bool(record.get("test_double")), "protocol_id": record.get("protocol_id"),
                   "model": (agent.get("provider") or {}).get("model"), "prompt_hash": agent.get("prompt_hash")}
        (target / "attempts" / f"{record['attempt_id']}.json").write_text(json.dumps(attempt, ensure_ascii=False), encoding="utf-8", newline="\n")
        written += 1
    pairs = comparisons if comparisons is not None else [
        {"name": f"{a}_vs_{b}", "a": a, "b": b} for a, b in (plan.get("analysis") or {}).get("pairs", [])]
    experiment = {"protocol_id": "agent-generalization-1.1", "auditlab_protocol_id": plan["protocol_id"],
                  "source_sha": source_sha(), "plan_artifact_id": plan["artifact_id"], "comparisons": pairs, "episodes": episodes}
    (target / "experiment.json").write_text(json.dumps(experiment, indent=1, ensure_ascii=False), encoding="utf-8", newline="\n")
    return {"attempts": written, "episodes": len(episodes), "target": str(target)}
