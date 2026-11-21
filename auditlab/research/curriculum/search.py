"""Compare adaptive and uniform candidate selection on one preregistered search pool."""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


def _count(value: int, name: str, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer greater than or equal to {minimum}")


def _label(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    family_id: str
    split: str
    stratum: str
    seed: int
    config: dict[str, Any]

    def __post_init__(self) -> None:
        for name in ("candidate_id", "family_id", "split", "stratum"):
            _label(getattr(self, name), name)
        _count(self.seed, "candidate seed")
        if not isinstance(self.config, dict):
            raise ValueError("candidate config must be a JSON object")
        _json(self.config)


@dataclass(frozen=True)
class SearchConfig:
    candidates: tuple[Candidate, ...]
    candidate_budget: int
    attempt_budget: int
    repetitions: int
    seed: int
    source_sha: str
    protocol_id: str
    cost_budget_micros: int | None = None
    max_attempt_cost_micros: int | None = None
    min_weak_repetitions: int = 1

    def __post_init__(self) -> None:
        if not self.candidates:
            raise ValueError("search requires a nonempty candidate pool")
        object.__setattr__(self, "candidates", tuple(self.candidates))
        if any(candidate.split != "train" for candidate in self.candidates):
            raise ValueError("only train candidates may enter the search pool")
        ids = {candidate.candidate_id for candidate in self.candidates}
        if len(ids) != len(self.candidates):
            raise ValueError("candidate IDs must be unique within the search pool")
        for name in ("candidate_budget", "attempt_budget", "repetitions", "min_weak_repetitions"):
            _count(getattr(self, name), name, minimum=1)
        if self.min_weak_repetitions > self.repetitions:
            raise ValueError("min_weak_repetitions cannot exceed repetitions")
        if self.attempt_budget < self.repetitions:
            raise ValueError("attempt_budget must cover all repetitions for at least one candidate")
        _count(self.seed, "search seed")
        _label(self.source_sha, "source_sha")
        _label(self.protocol_id, "protocol_id")
        if (self.cost_budget_micros is None) != (self.max_attempt_cost_micros is None):
            raise ValueError("both monetary limits must be set together")
        if self.cost_budget_micros is not None:
            _count(self.cost_budget_micros, "cost_budget_micros")
            _count(self.max_attempt_cost_micros, "max_attempt_cost_micros")


@dataclass(frozen=True)
class Validation:
    status: str
    reason: str
    artifact_ref: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"passed", "failed", "not_checked"}:
            raise ValueError("unsupported validation status")
        _label(self.reason, "validation reason")
        if self.artifact_ref is not None:
            _label(self.artifact_ref, "validation artifact_ref")


@dataclass(frozen=True)
class Attempt:
    status: str
    weak: bool | None = None
    cost_micros: int | None = None
    artifact_ref: str | None = None
    reason: str | None = None
    launches: int | None = 1

    def __post_init__(self) -> None:
        if self.status == "completed":
            if type(self.weak) is not bool or self.launches != 1:
                raise ValueError("a completed attempt requires a scored agent launch")
        elif self.status not in {"timeout", "provider_error", "invalid_response", "budget_stopped"}:
            raise ValueError("unsupported attempt status")
        elif self.weak is not None or not self.reason:
            raise ValueError("an unscored attempt requires an operational reason and no financial result")
        if self.cost_micros is not None:
            _count(self.cost_micros, "attempt cost_micros")
        if self.launches is not None:
            _count(self.launches, "attempt launches")
            if self.launches > 1:
                raise ValueError("each callback may launch at most one agent attempt")
        for name in ("artifact_ref", "reason"):
            value = getattr(self, name)
            if value is not None:
                _label(value, name)


RunAttempt = Callable[[Candidate, int, Path], Attempt]
ValidateCandidate = Callable[[Candidate, Path], Validation]


@dataclass
class _Stratum:
    offered: int = 0
    scored: int = 0
    weak: int = 0

    def priority(self, total_offered: int) -> float:
        rate = (self.weak + 1) / (self.scored + 2)
        exploration = math.sqrt(2 * math.log(2 + total_offered) / (1 + self.offered))
        return rate + exploration


@dataclass
class _Arm:
    candidates: list[dict[str, Any]] = field(default_factory=list)
    attempts: int = 0
    scored: int = 0
    weak: int = 0
    known_launches: int = 0
    unknown_launches: int = 0
    known_cost_micros: int = 0
    unknown_costs: int = 0
    attempt_in_progress: bool = False
    stop_reason: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "candidates": self.candidates,
            "offered": len(self.candidates),
            "accepted": sum(row["status"] == "accepted" for row in self.candidates),
            "attempt_callbacks": self.attempts,
            "scored_attempts": self.scored,
            "weak_attempts": self.weak,
            "agent_runs": self.known_launches if not (self.unknown_launches or self.attempt_in_progress) else None,
            "known_agent_runs": self.known_launches,
            "unknown_agent_runs": self.unknown_launches,
            "cost_micros": self.known_cost_micros if not (self.unknown_costs or self.attempt_in_progress) else None,
            "known_cost_micros": self.known_cost_micros,
            "unknown_costs": self.unknown_costs,
            "attempt_in_progress": self.attempt_in_progress,
            "stop_reason": self.stop_reason,
        }

    def record(self, attempt: Attempt) -> None:
        self.attempt_in_progress = False
        if attempt.weak is not None:
            self.scored += 1
            self.weak += int(attempt.weak)
        if attempt.launches is None:
            self.unknown_launches += 1
        else:
            self.known_launches += attempt.launches
        if attempt.cost_micros is None:
            self.unknown_costs += 1
        else:
            self.known_cost_micros += attempt.cost_micros


def _validate(callback: ValidateCandidate, candidate: Candidate, output: Path) -> Validation:
    try:
        result = callback(candidate, output)
        if not isinstance(result, Validation):
            raise TypeError("validator did not return Validation")
        return result
    except Exception as error:
        return Validation("not_checked", f"validator_error:{type(error).__name__}")


def _attempt(callback: RunAttempt, candidate: Candidate, repetition: int, output: Path) -> Attempt:
    try:
        result = callback(candidate, repetition, output)
        if not isinstance(result, Attempt):
            raise TypeError("runner did not return Attempt")
        return result
    except Exception as error:
        return Attempt("provider_error", reason=f"runner_error:{type(error).__name__}", launches=None)


def _classify(row: dict[str, Any], config: SearchConfig) -> tuple[str, str]:
    attempts = row["attempts"]
    scored = [attempt["weak"] for attempt in attempts if attempt["status"] == "completed"]
    if len(scored) != config.repetitions:
        return "not_checked", "planned_repetitions_not_scored"
    weakness_count = sum(scored)
    if weakness_count >= config.min_weak_repetitions:
        return "accepted", "weakness_threshold_met"
    if weakness_count:
        return "rejected", "weakness_threshold_not_met"
    return "rejected", "no_weakness_observed"


def _select(pool: list[Candidate], adaptive: bool, strata: dict[str, _Stratum], offered: int) -> int:
    if not adaptive:
        return 0
    priorities = {stratum: history.priority(offered) for stratum, history in strata.items()}
    return max(range(len(pool)), key=lambda index: priorities[pool[index].stratum])


def _run_arm(
    config: SearchConfig,
    arm: _Arm,
    pool: list[Candidate],
    adaptive: bool,
    run_attempt: RunAttempt,
    validate_candidate: ValidateCandidate,
    folder: Path,
    save: Callable[[], None],
) -> None:
    strata: dict[str, _Stratum] = defaultdict(_Stratum)
    for candidate in pool:
        strata[candidate.stratum]

    while pool and len(arm.candidates) < config.candidate_budget:
        if arm.attempts + config.repetitions > config.attempt_budget:
            arm.stop_reason = "attempt_budget"
            break
        # Reserve an entire cohort at the advertised worst case before selecting it.
        limit = config.max_attempt_cost_micros
        if limit is not None:
            reserved = config.repetitions * limit
            if arm.known_cost_micros + reserved > config.cost_budget_micros:
                arm.stop_reason = "cost_budget"
                break

        index = _select(pool, adaptive, strata, len(arm.candidates))
        candidate = pool.pop(index)
        history = strata[candidate.stratum]
        priority = history.priority(len(arm.candidates)) if adaptive else None
        history.offered += 1
        candidate_folder = folder / f"{len(arm.candidates) + 1:04d}"
        candidate_folder.mkdir(parents=True)
        row = {
            "candidate_id": candidate.candidate_id,
            "stratum": candidate.stratum,
            "priority": priority,
            "status": "in_progress",
            "reason": None,
            "validation": None,
            "attempts": [],
        }
        arm.candidates.append(row)
        save()

        validation = _validate(validate_candidate, candidate, candidate_folder / "validation")
        row["validation"] = asdict(validation)
        if validation.status != "passed":
            row["status"] = "rejected" if validation.status == "failed" else "not_checked"
            row["reason"] = validation.reason
            save()
            continue

        for repetition in range(1, config.repetitions + 1):
            arm.attempts += 1
            arm.attempt_in_progress = True
            row["attempts"].append({"repetition": repetition, "status": "in_progress"})
            save()
            attempt = _attempt(run_attempt, candidate, repetition, candidate_folder / f"attempt-{repetition:04d}")
            row["attempts"][-1].update(asdict(attempt))
            arm.record(attempt)
            if attempt.weak is not None:
                history.scored += 1
                history.weak += int(attempt.weak)
            if limit is not None:
                if attempt.cost_micros is None:
                    arm.stop_reason = "unreported_attempt_cost"
                elif attempt.cost_micros > limit:
                    arm.stop_reason = "attempt_cost_exceeded_reservation"
            save()
            if arm.stop_reason is not None:
                break

        row["status"], row["reason"] = _classify(row, config)
        if arm.stop_reason is not None:
            row["status"] = "not_checked"
            row["reason"] = arm.stop_reason
        save()
        if arm.stop_reason is not None:
            break

    if arm.stop_reason is None:
        arm.stop_reason = "candidate_budget" if pool else "pool_exhausted"
    save()


def search(
    search_config: SearchConfig,
    run_attempt: RunAttempt,
    validate_candidate: ValidateCandidate,
    output_dir: str | Path,
) -> dict[str, Any]:
    """Run equal budget caps per arm and persist selections, measurements, and rejections.

    The validator is called before any runner launch. Each runner invocation may launch
    one repetition and must enforce the configured per-attempt monetary cap, when set.
    Unscored runs are kept as operational results. An interrupted output directory is
    deliberately never replayed, since the last in-flight callback might have been billed.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    config = search_config
    pool = sorted(config.candidates, key=lambda candidate: candidate.candidate_id)
    plan = asdict(config)
    plan["candidates"] = [asdict(candidate) for candidate in pool]
    random.Random(config.seed).shuffle(pool)
    arms = {"adaptive": _Arm(), "uniform": _Arm()}
    receipt = {
        "schema_version": "1.0",
        "artifact_id": f"search-{uuid4().hex}",
        "source_sha": config.source_sha,
        "protocol_id": config.protocol_id,
        "status": "in_progress",
        "plan": plan,
        "plan_sha256": hashlib.sha256(_json(plan).encode("utf-8")).hexdigest(),
        "method": {
            "adaptive": "smoothed_scored_weakness_rate_plus_stratum_exploration",
            "uniform": "seeded_candidate_permutation_without_replacement",
            "arm_order": list(arms),
            "adaptive_tie_break": "same_seeded_candidate_permutation_as_uniform",
            "budget_scope": "per_arm",
        },
        "arms": {},
    }

    def save() -> None:
        receipt["arms"] = {name: arm.summary() for name, arm in arms.items()}
        staged = output_dir / "search.json.tmp"
        staged.write_text(_json(receipt) + "\n", encoding="utf-8")
        staged.replace(output_dir / "search.json")

    save()
    for name, arm in arms.items():
        _run_arm(config, arm, pool.copy(), name == "adaptive", run_attempt, validate_candidate, output_dir / name, save)
        if arm.stop_reason in {"unreported_attempt_cost", "attempt_cost_exceeded_reservation"}:
            receipt["status"] = "stopped_cost_contract"
            if name == "adaptive":
                arms["uniform"].stop_reason = "prior_arm_cost_contract"
            save()
            return receipt

    receipt["status"] = "completed"
    save()
    return receipt
