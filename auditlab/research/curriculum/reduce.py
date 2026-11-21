"""Reduce a validated financial case and remeasure a specific failure of an auditor.

This module is on the trusted side. Its attempt callback opens a fresh policy
session and gives the policy access only to public observations. The independent
validator and the attempt runner are supplied by the caller.
"""
from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from math import isfinite, sqrt
from pathlib import Path
from time import monotonic
from typing import Any

from auditlab.release.rehearsal import PUBLIC_FILES, canonical_hash, file_hashes
from auditlab.release.transforms import read_json, write_json
from auditlab.verifier.crosscheck import _read_units, _write_units

from .export import snapshot_sha256 as _public_sha256

VERSION = "1.0"
TABLES = ("ledger", "invoices", "payments", "fx")
RecordKey = tuple[str, str]
Receipt = Mapping[str, Any]
Validation = Callable[[Path, Path], Receipt]
Investigation = Callable[[Path, Path, float], Receipt]


@dataclass(frozen=True)
class FailureTarget:
    reason: str
    issue_type: str
    affected_ids: tuple[str, ...]
    issue_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.affected_ids, (list, tuple)):
            raise ValueError("affected_ids must contain public entity IDs")
        if not all(type(value) is str and value for value in (self.reason, self.issue_type, *self.affected_ids)):
            raise ValueError("failure target requires nonempty strings")
        if self.issue_id is not None and (type(self.issue_id) is not str or not self.issue_id):
            raise ValueError("issue_id must be a nonempty string or null")
        object.__setattr__(self, "affected_ids", tuple(sorted(set(self.affected_ids))))

    def matches(self, failure: Receipt) -> bool:
        affected = failure.get("affected_ids")
        return (
            failure.get("reason") == self.reason
            and failure.get("type") == self.issue_type
            and isinstance(affected, (list, tuple))
            and all(isinstance(key, str) for key in affected)
            and tuple(sorted(set(affected))) == self.affected_ids
            and (self.issue_id is None or failure.get("issue_id") == self.issue_id)
        )


@dataclass(frozen=True)
class ReductionBudget:
    repetitions: int = 3
    min_matches: int = 2
    max_candidates: int = 20
    max_runs: int = 63
    max_seconds: float = 300.0
    cost_budget_micros: int | None = None
    max_attempt_cost_micros: int | None = None

    def __post_init__(self):
        counts = (self.repetitions, self.min_matches, self.max_candidates, self.max_runs)
        if any(type(value) is not int for value in counts) or not (
            2 <= self.repetitions <= self.max_runs and 2 <= self.min_matches <= self.repetitions
            and self.max_candidates >= 0
        ):
            raise ValueError("budget requires at least two matching repeats and room for a baseline")
        if type(self.max_seconds) not in (int, float) or not isfinite(self.max_seconds) or self.max_seconds <= 0:
            raise ValueError("max_seconds must be positive and finite")
        costs = (self.cost_budget_micros, self.max_attempt_cost_micros)
        if costs != (None, None) and any(type(value) is not int or value < 0 for value in costs):
            raise ValueError("cost budget and per-attempt cap must both be nonnegative integers")


@dataclass(frozen=True)
class ReductionCandidate:
    snapshot: Path
    target: FailureTarget
    source_sha: str
    protocol_id: str
    budget: ReductionBudget = ReductionBudget()


def snapshot_sha256(snapshot: Path) -> str:
    """Digest of the six public files, compatible with runner-side attempt receipts."""
    return _public_sha256(Path(snapshot) / "public")


@dataclass
class _Book:
    directory: Path
    units: dict
    keys: dict[str, list[RecordKey]]
    links: dict[RecordKey, set[str]]

    @classmethod
    def read(cls, directory: Path) -> _Book:
        if directory.is_symlink():
            raise ValueError("reduction expects a regular public-format directory")
        paths = list(directory.iterdir())
        if ({path.name for path in paths} != set(PUBLIC_FILES)
                or any(path.is_symlink() or not path.is_file() for path in paths)):
            raise ValueError("reduction expects exactly the six regular public-format files")
        units = _read_units(directory)
        keys = {table: [] for table in TABLES}
        links = {}
        for table in TABLES:
            for packed in units[table]:
                record = json.loads(packed) if table in ("invoices", "payments") else packed
                key = _record_key(table, record)
                if key in links:
                    raise ValueError(f"duplicate record in {table}: {key[1]}")
                keys[table].append(key)
                links[key] = _links(key, record)
        return cls(directory, units, keys, links)

    def project(self, removed: set[RecordKey]) -> dict:
        return {
            **self.units,
            **{table: [record for key, record in zip(self.keys[table], self.units[table])
                       if key not in removed] for table in TABLES},
        }


def _record_key(table: str, record: dict) -> RecordKey:
    if table == "fx":
        return table, "/".join(record[field] for field in ("date", "source_currency", "target_currency"))
    return table, record["row_id" if table == "ledger" else "doc_id"]


def _links(key: RecordKey, record: dict) -> set[str]:
    links = {"/".join(key)}
    for namespace in ("invoice", "payment", "journal", "entity"):
        value = record.get(f"{namespace}_id")
        if value not in (None, "", "null"):
            links.add(f"{namespace}/{value}")
    if record.get("canonical_key"):
        links.add(f"canonical_key/{record['canonical_key']}")
    links.update(f"invoice/{value}" for value in record.get("invoice_refs", []))
    links.update(f"invoice/{item['invoice_id']}" for item in record.get("allocation_instructions", []))
    links.update(f"ledger/{value}" for value in record.get("settlement_index", {}).get("unapplied_row_ids", []))
    return links


def _removable_groups(books: list[_Book], protected: set[str]) -> list[tuple[RecordKey, ...]]:
    # Link both the public and source books. A source-only journal can still be
    # part of the same history, and a settlement index can link across tables.
    relations = defaultdict(set)
    records = defaultdict(set)
    for book in books:
        for key, links in book.links.items():
            relations[key].update(links)
            for link in links:
                records[link].add(key)
    visited = set()
    groups = []
    for root in sorted(relations):
        if root in visited:
            continue
        group, component_links = {root}, set()
        pending = [root]
        while pending:
            key = pending.pop()
            for link in relations[key] - component_links:
                unseen = records[link] - group
                group.update(unseen)
                pending.extend(unseen)
                component_links.add(link)
        visited.update(group)
        if component_links.isdisjoint(protected):
            groups.append(tuple(sorted(group)))
    return groups


def _episode_id(manifest: dict, removed: set[RecordKey]) -> str:
    # Same public ID shape as other profile 1.1 episodes. The original ID
    # commits to the original case; the removed record keys fix the projection.
    return "E" + canonical_hash([VERSION, manifest["episode_id"], sorted(removed)])[:32].upper()


@dataclass
class _Projection:
    public: _Book
    clean: _Book | None
    truth: dict
    provenance: dict
    transition: dict | None
    parent_sha256: str

    @classmethod
    def read(cls, snapshot: Path, public_sha256: str) -> _Projection:
        private = snapshot / "private"
        clean = private / "source_book"
        transition = private / "source_transition.json"
        provenance = private / "provenance.json"
        return cls(
            _Book.read(snapshot / "public"),
            _Book.read(clean) if clean.is_dir() else None,
            read_json(private / "truth.json"),
            read_json(provenance) if provenance.is_file() else {},
            read_json(transition) if transition.is_file() else None,
            public_sha256,
        )

    def groups(self, target: FailureTarget) -> list[tuple[RecordKey, ...]]:
        protected = set(target.affected_ids)
        for issue in self.truth["issues"]:
            protected.update(issue["affected_ids"])
            protected.update(key for evidence in issue["evidence_sets"] for key in evidence)
        for change in (self.transition or {}).get("changes", []):
            protected.add(f"{change['table']}/{change['id']}")
        books = [self.public, self.clean] if self.clean else [self.public]
        return _removable_groups(books, protected)

    def write(self, snapshot: Path, removed: set[RecordKey]) -> dict:
        public, private = snapshot / "public", snapshot / "private"
        snapshot.mkdir(parents=True)
        private.mkdir()
        units = self.public.project(removed)
        episode_id = self._write_book(self.public, public, units, removed)
        write_json(private / "truth.json", {**self.truth, "episode_id": episode_id})
        provenance = {
            "episode_id": episode_id,
            "reduction": {"version": VERSION, "parent_snapshot_sha256": self.parent_sha256},
        }
        if self.clean is not None and self.transition is not None:
            clean = private / "source_book"
            source_episode_id = self._write_book(self.clean, clean, self.clean.project(removed), removed)
            transition = {**self.transition, "schema_version": VERSION, "target_episode_id": episode_id,
                          "source_episode_id": source_episode_id}
            transition_path = private / "source_transition.json"
            write_json(transition_path, transition)
            provenance["mutations"] = self.provenance.get("mutations", [])
            if "generator_version" in self.provenance:
                provenance["generator_version"] = self.provenance["generator_version"]
                provenance["source_book"] = {
                    "schema_version": VERSION,
                    "path": clean.name,
                    "episode_id": source_episode_id,
                    "files_sha256": file_hashes(clean),
                    "target_files_sha256": file_hashes(public),
                    "transition_path": transition_path.name,
                    "transition_sha256": sha256(transition_path.read_bytes()).hexdigest(),
                }
        write_json(private / "provenance.json", provenance)
        return _sizes(units)

    @staticmethod
    def _write_book(book: _Book, directory: Path, units: dict, removed: set[RecordKey]) -> str:
        _write_units(book.directory, directory, units)
        manifest = read_json(directory / "manifest.json")
        manifest["episode_id"] = _episode_id(manifest, removed)
        write_json(directory / "manifest.json", manifest)
        return manifest["episode_id"]


def _sizes(units: dict) -> dict:
    return {"ledger_rows": len(units["ledger"]), "documents": len(units["invoices"]) + len(units["payments"]),
            "fx_rows": len(units["fx"])}


def _receipt(callback, *args) -> dict:
    try:
        result = callback(*args)
        if not isinstance(result, Mapping):
            raise TypeError("callback did not return a receipt mapping")
        receipt = dict(result)
        # Refuse non-JSON and non-finite data before persisting it as a measured result.
        return json.loads(json.dumps(receipt, allow_nan=False))
    except Exception as error:
        return {"status": "callback_error", "reason": type(error).__name__}


def _rate(matches: int, attempts: int) -> dict | None:
    if not attempts:
        return None
    frequency = matches / attempts
    z = 1.959963984540054
    factor = 1 + z * z / attempts
    center = (frequency + z * z / (2 * attempts)) / factor
    half = z * sqrt(frequency * (1 - frequency) / attempts + z * z / (4 * attempts**2)) / factor
    return {"observed": frequency, "wilson_95": [max(0.0, center - half), min(1.0, center + half)]}


class _Measurements:
    def __init__(self, candidate: ReductionCandidate, validate: Validation, investigate: Investigation):
        self.target = candidate.target
        self.budget = candidate.budget
        self.validate = validate
        self.investigate = investigate
        self.started = monotonic()
        self.attempt_ids = set()
        self.runs = 0
        self.known_cost_micros = 0
        self.unknown_costs = 0
        self.cost_stop = None

    def remaining(self) -> float:
        return self.budget.max_seconds - (monotonic() - self.started)

    def batch_blocker(self) -> str | None:
        budget = self.budget
        if self.cost_stop is not None:
            return self.cost_stop
        if self.remaining() <= 0:
            return "time_budget"
        if self.runs + budget.repetitions > budget.max_runs:
            return "run_budget"
        if budget.cost_budget_micros is not None:
            reservation = budget.repetitions * budget.max_attempt_cost_micros
            if self.known_cost_micros + reservation > budget.cost_budget_micros:
                return "cost_budget"
        return None

    def record_cost(self, attempt: dict) -> None:
        cost = attempt.get("cost_micros")
        cap = self.budget.max_attempt_cost_micros
        if type(cost) is not int or cost < 0:
            self.unknown_costs += 1
            if cap is not None:
                self.cost_stop = "unreported_attempt_cost"
            return
        self.known_cost_micros += cost
        if cap is not None and cost > cap:
            self.cost_stop = "attempt_cost_exceeded_reservation"

    def measure(self, snapshot: Path, output: Path, sizes: dict) -> dict:
        snapshot_hash = snapshot_sha256(snapshot)
        before = file_hashes(snapshot)
        result = {"snapshot": str(snapshot), "snapshot_sha256": snapshot_hash, "sizes": sizes, "attempts": []}
        result["validation"] = _receipt(self.validate, snapshot, output / "validation")
        self._check_immutable(snapshot, before)
        result["reason"] = ("validation_not_passed" if result["validation"].get("status") != "passed"
                            else self.batch_blocker())
        if result["reason"] is not None:
            write_json(output / "measurement.json", result)
            return result
        for repetition in range(1, self.budget.repetitions + 1):
            remaining = self.remaining()
            if remaining <= 0:
                break
            run_dir = output / f"run-{repetition:02}"
            receipt_path = output / f"receipt-{repetition:02}.json"
            attempt_record = {"repetition": repetition, "output_dir": str(run_dir)}
            write_json(receipt_path, {**attempt_record, "receipt": {"status": "in_progress"}})
            self.runs += 1
            attempt = _receipt(self.investigate, snapshot, run_dir, remaining)
            self._check_immutable(snapshot, before)
            self.record_cost(attempt)
            if self.cost_stop is not None:
                assessed, matched, issue = False, False, self.cost_stop
            elif self.remaining() <= 0:
                assessed, matched, issue = False, False, "reduction_deadline_exceeded"
            else:
                assessed, matched, issue = self._assess(attempt, snapshot_hash)
            attempt_record.update(assessed=assessed, matched_target=matched, reason=issue, receipt=attempt)
            result["attempts"].append(attempt_record)
            write_json(receipt_path, attempt_record)
            if self.cost_stop is not None:
                break
        attempted = len(result["attempts"])
        assessed = sum(attempt["assessed"] for attempt in result["attempts"])
        matched = sum(attempt["matched_target"] for attempt in result["attempts"])
        result["frequency"] = {
            "matched": matched, "attempted": attempted, "assessed": assessed,
            "per_attempt": _rate(matched, attempted), "per_assessed": _rate(matched, assessed),
        }
        if self.cost_stop is not None:
            result["reason"] = self.cost_stop
        elif attempted != self.budget.repetitions:
            result["reason"] = "incomplete_repetitions"
        elif assessed < self.budget.min_matches:
            result["reason"] = "insufficient_assessed_repetitions"
        elif matched < self.budget.min_matches:
            result["reason"] = "target_not_reproduced"
        else:
            result["reason"] = None
        write_json(output / "measurement.json", result)
        return result

    @staticmethod
    def _check_immutable(snapshot: Path, original_hashes: dict) -> None:
        if original_hashes != file_hashes(snapshot):
            raise RuntimeError(f"callback changed the measured snapshot: {snapshot}")

    def _assess(self, attempt: dict, snapshot_hash: str) -> tuple[bool, bool, str | None]:
        if attempt.get("status") != "submitted":
            return False, False, attempt.get("reason") or "attempt_not_submitted"
        attempt_id = attempt.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id or attempt_id in self.attempt_ids:
            return False, False, "missing_or_reused_attempt_id"
        self.attempt_ids.add(attempt_id)
        if attempt.get("snapshot_sha256") != snapshot_hash:
            return False, False, "attempt_snapshot_mismatch"
        failures = attempt.get("failures")
        if not isinstance(failures, list) or not all(isinstance(failure, dict) for failure in failures):
            return False, False, "missing_failure_assessment"
        matched = any(self.target.matches(failure) for failure in failures)
        return True, matched, None if matched else "different_outcome"


def reduce(candidate_ref: ReductionCandidate, failure_predicate: Investigation,
           validate_candidate: Validation, output_dir: Path) -> dict:
    """Validate and measure baseline, then propose deletions in deterministic batches.

    Each callback gets a fresh output path. A submitted attempt must return a
    unique attempt_id, snapshot_sha256 and structured failures (reason, type,
    affected_ids and optional issue_id). Other statuses stay unassessed.
    The runner enforces the passed wall time and its own execution limits. A
    caller configuring a cost budget must also enforce the per-attempt cap in
    the runner and return a measured integer cost_micros in each receipt.
    """
    source = Path(candidate_ref.snapshot).resolve()
    output = Path(output_dir).resolve()
    if output.is_relative_to(source):
        raise ValueError("reduction output must be outside the source snapshot")
    output.mkdir(parents=True, exist_ok=False)
    budget = candidate_ref.budget
    measurements = _Measurements(candidate_ref, validate_candidate, failure_predicate)
    manifest = read_json(source / "public" / "manifest.json")
    original_sizes = _sizes(_read_units(source / "public"))
    baseline_dir = output / "baseline"
    baseline_dir.mkdir()
    baseline = measurements.measure(source, baseline_dir, original_sizes)
    artifact_key = [baseline["snapshot_sha256"], asdict(candidate_ref.target), candidate_ref.source_sha,
                    candidate_ref.protocol_id, asdict(budget)]
    report = {
        "schema_version": VERSION,
        "artifact_id": f"reduction-{canonical_hash(artifact_key)[:24]}",
        "source_sha": candidate_ref.source_sha, "protocol_id": candidate_ref.protocol_id,
        "episode_id": manifest["episode_id"], "target": asdict(candidate_ref.target),
        "budget": asdict(budget), "baseline": baseline, "proposals": [],
        "selected_snapshot": baseline["snapshot"], "selected_snapshot_sha256": baseline["snapshot_sha256"],
        "selected_sizes": original_sizes,
    }
    if baseline["reason"] is None:
        _reduce_valid_baseline(source, output, measurements, report)
    else:
        report["status"] = "not_reproduced" if baseline["reason"] == "target_not_reproduced" else "not_checked"
        report["reason"] = baseline["reason"]
    report["runs_used"] = measurements.runs
    report["cost"] = {
        "cost_micros": None if measurements.unknown_costs else measurements.known_cost_micros,
        "known_cost_micros": measurements.known_cost_micros,
        "unknown_attempt_costs": measurements.unknown_costs,
    }
    report["elapsed_seconds"] = monotonic() - measurements.started
    write_json(output / "reduction.json", report)
    return read_json(output / "reduction.json")


def _reduce_valid_baseline(source: Path, output: Path, measurements: _Measurements, report: dict) -> None:
    baseline = report["baseline"]
    projection = _Projection.read(source, baseline["snapshot_sha256"])
    groups = projection.groups(measurements.target)
    pending = deque([groups] if groups else [])
    removed = set()
    stop = "no_more_proposals"
    while pending:
        reason = measurements.batch_blocker()
        if reason is not None or len(report["proposals"]) >= measurements.budget.max_candidates:
            stop = reason or "candidate_budget"
            break
        batch = pending.popleft()
        drop = {key for group in batch for key in group}
        proposal = output / f"proposal-{len(report['proposals']) + 1:03}"
        snapshot = proposal / "snapshot"
        sizes = projection.write(snapshot, removed | drop)
        result = measurements.measure(snapshot, proposal, sizes)
        result["transformation"] = {"kind": "remove_linked_components", "records": sorted(drop)}
        if result["reason"] is None and result["frequency"]["matched"] < baseline["frequency"]["matched"]:
            result["reason"] = "observed_frequency_below_baseline"
        accepted = result["reason"] is None
        result["accepted"] = accepted
        report["proposals"].append(result)
        if measurements.cost_stop is not None:
            stop = measurements.cost_stop
            break
        if accepted:
            removed.update(drop)
            report.update(selected_snapshot=result["snapshot"], selected_snapshot_sha256=result["snapshot_sha256"],
                          selected_sizes=sizes)
        elif len(batch) > 1:
            middle = len(batch) // 2
            pending.extendleft((batch[middle:], batch[:middle]))
    report["status"] = "reduced" if removed else "unchanged"
    report["reason"] = stop
