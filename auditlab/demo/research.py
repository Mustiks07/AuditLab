"""Read-only projection of research artifacts (docs/plans/agent-improvement/CONTRACTS.md) for the console.

The web process never opens truth, private directories or secrets. It reads JSON artifacts under the
configured research roots, validates their common fields, strips private keys, joins the documented
references (experiment → attempts → failure → reduction → curriculum → training → comparison) and
exposes public snapshot records for exact evidence navigation. It does not compute financial
results or alternative aggregate metrics: amounts come from attempts, validations and the existing
console derivation; comparison metrics come from comparison.json as published.

Until the integrator fixes JSON Schema 1.0, an artifact is recognised by ``artifact_kind`` or by its
file name (``experiment.json``, ``attempt-*.json`` …) or parent directory (``attempts/…``).
Any artifact that cannot be read or validated is listed as incompatible with a reason, never guessed.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

from . import derive, engines
from .snapshot import Snapshot

KINDS = ("experiment", "attempt", "failure", "reduction", "curriculum", "training", "comparison",
         "validation", "candidate")
PLURALS = {kind + ("s" if kind != "curriculum" else ""): kind for kind in KINDS}
PLURALS.update({"curricula": "curriculum", "training": "training", "trainings": "training"})
SUPPORTED_MAJOR = 1
MAX_BYTES = 8 * 1024 * 1024
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
# Keys that belong to the trusted side. Removed recursively from every projection.
PRIVATE_KEYS = {"truth", "truth_ref", "private", "private_ref", "trusted_ref", "trusted_dir", "seed", "seed_hex",
                "generator_config", "config", "api_key", "api_keys", "secret", "secrets", "password",
                "authorization", "bearer", "api_key_env", "evaluation_ref", "episode_ref", "answer_key", "expected_findings", "closed_answer"}
# Counters must be non-negative integers; a float, bool or negative value makes the artifact incompatible.
COUNTERS = {"planned", "completed", "running", "timeout", "provider_error", "invalid_response", "budget_stop",
            "tp", "fp", "fn", "count", "denominator", "total", "timeouts", "errors", "repetitions",
            "families", "episodes", "ledger_rows", "documents", "inspection_steps", "train", "dev", "holdout",
            "input_tokens", "output_tokens", "tokens", "failed_count"}
PROVENANCE = {"dev_fixture": "Фикстура разработки", "measured": "Измерение", "test_double": "Тестовая замена"}


class ResearchError(LookupError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def strip_private(value):
    if isinstance(value, dict):
        return {k: strip_private(v) for k, v in value.items()
                if k not in PRIVATE_KEYS and not (isinstance(k, str) and k.endswith("_secret"))}
    if isinstance(value, list):
        return [strip_private(v) for v in value]
    return value


def _value_problem(value, key: str | None = None, path: str = "") -> str | None:
    if isinstance(value, float) and not math.isfinite(value):
        return f"{path or key}: не конечное число"
    if key in COUNTERS and value is not None and not isinstance(value, (dict, list)):
        if type(value) is not int or value < 0:
            return f"{path or key}: счётчик должен быть целым неотрицательным числом"
    if isinstance(value, dict):
        for k, v in value.items():
            problem = _value_problem(v, k if isinstance(k, str) else None, f"{path}.{k}" if path else str(k))
            if problem:
                return problem
    if isinstance(value, list):
        for i, v in enumerate(value):
            problem = _value_problem(v, None, f"{path}[{i}]")
            if problem:
                return problem
    return None


def _kind_of(path: Path, data: dict) -> str | None:
    kind = data.get("artifact_kind")
    if isinstance(kind, str):
        return kind if kind in KINDS else None
    stem = path.stem.split("-", 1)[0]
    if stem in KINDS:
        return stem
    return PLURALS.get(path.parent.name)


class ResearchStore:
    def __init__(self, roots: list[Path] | tuple = ()):
        self.roots = [Path(r).resolve() for r in roots]
        self.artifacts: dict[str, dict[str, dict]] = {kind: {} for kind in KINDS}
        self.incompatible: list[dict] = []
        self.snapshots: dict[str, Path] = {}
        self._origin: dict[tuple[str, str], Path] = {}
        self._scan()
        self._register_snapshots()

    def _register_snapshots(self) -> None:
        for kind, items in self.artifacts.items():
            for artifact_id, data in items.items():
                refs = [data.get("snapshot_ref")]
                for name in ("original", "reduced"):
                    if isinstance(data.get(name), dict):
                        refs.append(data[name].get("snapshot_ref"))
                refs += [f.get("snapshot_ref") for f in data.get("families") or [] if isinstance(f, dict)]
                for ref in refs:
                    if ref is not None:
                        self.snapshot_id(kind, artifact_id, ref)

    # ------------------------------------------------------------------ loading
    def _scan(self) -> None:
        for root in self.roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.json")):
                rel = path.relative_to(root)
                if {"snapshots", "trusted", "private", "public"} & set(rel.parts[:-1]):
                    continue
                self._load(root, path, rel)

    def _load(self, root: Path, path: Path, rel: Path) -> None:
        def bad(reason: str, kind: str | None = None):
            self.incompatible.append({"file": rel.as_posix(), "kind": kind, "reason": reason})
        try:
            resolved = path.resolve()
            if path.is_symlink() or root not in resolved.parents or {"trusted", "private"} & set(resolved.relative_to(root).parts[:-1]):
                return bad("путь вне публичного корня")
            if path.stat().st_size > MAX_BYTES:
                return bad("файл больше допустимого размера")
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, RecursionError) as error:
            return bad(f"не читается как JSON: {type(error).__name__}")
        if not isinstance(data, dict):
            return bad("ожидается JSON-объект")
        kind = _kind_of(path, data)
        if kind is None:
            return bad("тип артефакта не распознан")
        version = data.get("schema_version")
        major = version.split(".", 1)[0] if isinstance(version, str) else None
        if not (major and major.isdigit() and int(major) == SUPPORTED_MAJOR):
            return bad(f"версия схемы {version!r} не поддерживается (ожидается {SUPPORTED_MAJOR}.x)", kind)
        artifact_id = data.get("artifact_id")
        if not isinstance(artifact_id, str) or not ID_RE.match(artifact_id):
            return bad("нет корректного artifact_id", kind)
        for field in ("source_sha", "protocol_id"):
            if not isinstance(data.get(field), str) or not data[field]:
                return bad(f"нет поля {field}", kind)
        problem = _value_problem(data)
        if problem:
            return bad(problem, kind)
        if artifact_id in self.artifacts[kind]:
            return bad(f"повтор artifact_id {artifact_id}", kind)
        data = strip_private(data)
        data["_provenance"] = self._provenance(data)
        self.artifacts[kind][artifact_id] = data
        self._origin[(kind, artifact_id)] = root

    @staticmethod
    def _provenance(data: dict) -> dict:
        source = data.get("provenance") if isinstance(data.get("provenance"), dict) else {}
        kind = source.get("kind") if source.get("kind") in PROVENANCE else "unknown"
        note = source.get("note") if isinstance(source.get("note"), str) else None
        return {"kind": kind, "label": PROVENANCE.get(kind, "Происхождение не указано"), "note": note}

    def get(self, kind: str, artifact_id: str) -> dict:
        found = self.artifacts.get(kind, {}).get(artifact_id)
        if found is None:
            raise ResearchError("not_found", f"No {kind} artifact with this id.")
        return found

    def find(self, kind: str, **match) -> list[dict]:
        return [a for a in self.artifacts[kind].values() if all(a.get(k) == v for k, v in match.items())]

    # ------------------------------------------------------------------ snapshots
    def snapshot_id(self, kind: str, artifact_id: str, ref: object) -> str | None:
        """Opaque id of a public snapshot referenced by an artifact; refuses anything outside its root."""
        if not isinstance(ref, str) or not ref or ref.startswith("/") or "\\" in ref:
            return None
        parts = Path(ref).parts
        if ".." in parts or {"private", "trusted"} & set(parts) or parts[-1] != "public":
            return None
        root = self._origin.get((kind, artifact_id))
        if root is None:
            return None
        path = (root / ref).resolve()
        if root not in path.parents or {"private", "trusted"} & set(path.relative_to(root).parts) or not (path / "manifest.json").is_file():
            return None
        if any(p.is_symlink() for p in path.iterdir()):
            return None
        sid = hashlib.sha256(str(path).encode()).hexdigest()[:16]
        self.snapshots[sid] = path
        return sid

    def snapshot(self, sid: str) -> Snapshot:
        path = self.snapshots.get(sid)
        if path is None:
            raise ResearchError("not_found", "No public snapshot with this id.")
        return Snapshot(path)

    # ------------------------------------------------------------------ projections
    def summary(self, kind: str, data: dict) -> dict:
        keep = {"artifact_id", "title", "status", "state", "kind", "created_at", "_provenance", "protocol_id",
                "progress", "budget", "frequency", "experiment_id", "curriculum_id", "variant_id", "verification"}
        return {k: v for k, v in data.items() if k in keep}


def received_keys(steps: list[dict]) -> dict[str, int]:
    """Public keys the agent actually received, with the first step: opened documents and listed records."""
    seen: dict[str, int] = {}
    for step in steps:
        action = step.get("action") or {}
        obs = step.get("observation") or {}
        if not isinstance(obs, dict) or obs.get("ok") is not True:
            continue
        number = step.get("step")
        result = obs.get("result")
        args = action.get("args") if isinstance(action.get("args"), dict) else {}
        name = action.get("action")
        if name == "list_records" and isinstance(result, dict):
            table = args.get("table")
            for record in result.get("records") or []:
                if not isinstance(record, dict):
                    continue
                if table == "fx":
                    local = "/".join(str(record.get(k)) for k in ("date", "source_currency", "target_currency"))
                elif table == "policy":
                    local = record.get("id")
                else:
                    local = record.get("row_id") or record.get("doc_id")
                if isinstance(local, str):
                    seen.setdefault(f"{table}/{local}", number)
        elif name == "get_document" and isinstance(result, dict):
            doc = result.get("doc_id")
            table = "payments" if "payment_id" in result and "invoice_refs" in result else "invoices"
            if isinstance(doc, str):
                seen.setdefault(f"{table}/{doc}", number)
        elif name == "get_record" and isinstance(args.get("table"), str) and isinstance(args.get("id"), str):
            seen.setdefault(f"{args['table']}/{args['id']}", number)
    return seen


def _allocation_summary(calc: dict) -> dict | None:
    """Received / allocated-before-boundary / residual of the existing console derivation, one currency only."""
    if calc.get("type") != "payment_allocation":
        return None
    operands = calc.get("operands") or []
    currency = calc.get("currency")
    if not operands or any(op.get("currency") != currency or type(op.get("value_minor")) is not int for op in operands):
        return None
    rule = calc.get("rule") or {}
    return {"currency": currency,
            "received_minor": sum(op["value_minor"] for op in operands if op.get("sign", 0) > 0),
            "allocated_minor": sum(op["value_minor"] for op in operands if op.get("sign", 0) < 0),
            "residual_minor": calc.get("result_minor"),
            "boundary": rule.get("boundary"), "boundary_exclusive": rule.get("boundary_exclusive"),
            "after_boundary": [{"key": c.get("key"), "value_minor": c.get("value_minor")} for c in calc.get("context") or []]}


def _step_view(step: dict) -> dict:
    action = step.get("action") or {}
    obs = step.get("observation") or {}
    args = action.get("args") if isinstance(action.get("args"), dict) else {}
    result = obs.get("result") if isinstance(obs, dict) else None
    view = {"step": step.get("step"), "action": action.get("action"), "ok": obs.get("ok") if isinstance(obs, dict) else None,
            "remaining_steps": obs.get("remaining_steps") if isinstance(obs, dict) else None,
            "table": args.get("table"), "cursor": args.get("cursor") is not None}
    if action.get("action") == "list_records" and isinstance(result, dict):
        view["records"] = len(result.get("records") or [])
        view["more"] = result.get("next_cursor") is not None
    if action.get("action") == "get_document" and isinstance(result, dict) and isinstance(result.get("doc_id"), str):
        table = "payments" if "payment_id" in result and "invoice_refs" in result else "invoices"
        view["opened_key"] = f"{table}/{result['doc_id']}"
    if action.get("action") == "get_record" and isinstance(args.get("table"), str) and isinstance(args.get("id"), str):
        view["opened_key"] = f"{args['table']}/{args['id']}"
    if action.get("action") == "submit":
        view["findings"] = len(args.get("findings") or [])
    if isinstance(obs, dict) and obs.get("ok") is False:
        view["error"] = (obs.get("error") or {}).get("code") if isinstance(obs.get("error"), dict) else "error"
    return view


class Projection:
    """Screen-shaped views over a ResearchStore; every reference is resolved or reported as missing."""

    def __init__(self, store: ResearchStore):
        self.store = store

    def links(self, experiment_id: object = None, failure_id: object = None, reduction_id: object = None) -> dict:
        """Ids of the related artifacts of each research stage, resolved from documented references only."""
        s = self.store
        failure = s.artifacts["failure"].get(failure_id) if isinstance(failure_id, str) else None
        if failure is None and isinstance(experiment_id, str):
            found = sorted(s.find("failure", experiment_id=experiment_id), key=lambda f: f["artifact_id"])
            failure = found[0] if found else None
        if failure is not None:
            experiment_id = experiment_id or failure.get("experiment_id")
            reduction_id = reduction_id or failure.get("reduction_id")
        reduction = s.artifacts["reduction"].get(reduction_id) if isinstance(reduction_id, str) else None
        if reduction is not None:
            experiment_id = experiment_id or reduction.get("experiment_id")
        curricula = [c for c in s.artifacts["curriculum"].values()
                     if (reduction is not None and reduction["artifact_id"] in (c.get("reduction_ids") or []))
                     or (failure is not None and failure["artifact_id"] in (c.get("failure_ids") or []))
                     or (isinstance(experiment_id, str) and c.get("experiment_id") == experiment_id)]
        comparisons = [c for c in s.artifacts["comparison"].values()
                       if isinstance(experiment_id, str) and c.get("experiment_id") == experiment_id]
        exists = isinstance(experiment_id, str) and experiment_id in s.artifacts["experiment"]
        first = lambda items: sorted(items, key=lambda a: a["artifact_id"])[0]["artifact_id"] if items else None
        return {"experiment": experiment_id if exists else None,
                "failure": failure["artifact_id"] if failure else None,
                "reduction": reduction["artifact_id"] if reduction else None,
                "curriculum": first(curricula), "comparison": first(comparisons)}

    def _attempt_summary(self, attempt: dict) -> dict:
        evaluation = attempt.get("evaluation") if isinstance(attempt.get("evaluation"), dict) else None
        shown = evaluation if evaluation and evaluation.get("visibility") == "open" else None
        episode = attempt.get("episode") if isinstance(attempt.get("episode"), dict) else {}
        return {"artifact_id": attempt["artifact_id"], "variant_id": attempt.get("variant_id"),
                "repetition": attempt.get("repetition"), "status": attempt.get("status"),
                "company_label": episode.get("company_label"), "split": episode.get("split"),
                "family_id": episode.get("family_id"),
                "evaluation": {k: shown.get(k) for k in ("tp", "fp", "fn", "reward", "inspection_steps")} if shown
                else None,
                "evaluation_hidden": bool(evaluation) and shown is None,
                "claims": [{k: f.get(k) for k in ("type", "claimed_amount_minor", "currency")}
                           for f in ((attempt.get("submission") or {}).get("findings") or []) if isinstance(f, dict)],
                "usage": attempt.get("usage"), "steps": len(attempt.get("steps") or []),
                "error": {k: v for k, v in attempt["error"].items() if k in ("code", "message")}
                if isinstance(attempt.get("error"), dict) else None,
                "_provenance": attempt["_provenance"]}

    def status(self, controller: dict) -> dict:
        counts = {kind: len(items) for kind, items in self.store.artifacts.items()}
        kinds = {a["_provenance"]["kind"] for items in self.store.artifacts.values() for a in items.values()}
        return {"roots_configured": bool(self.store.roots), "counts": counts,
                "incompatible": len(self.store.incompatible), "provenance": sorted(kinds), "controller": controller}

    def overview(self) -> dict:
        s = self.store
        cards = [self.experiment_card(e) for e in s.artifacts["experiment"].values()]
        cards.sort(key=lambda c: (-c["failures"], c["artifact_id"]))
        return {"experiments": cards,
                "curricula": [s.summary("curriculum", c) for c in s.artifacts["curriculum"].values()],
                "training": [s.summary("training", t) for t in s.artifacts["training"].values()],
                "comparisons": [s.summary("comparison", c) for c in s.artifacts["comparison"].values()],
                "incompatible": s.incompatible}

    def experiment_card(self, experiment: dict) -> dict:
        failures = self.store.find("failure", experiment_id=experiment["artifact_id"])
        return {**{k: experiment.get(k) for k in ("artifact_id", "title", "status", "stop_reason", "agents", "mode",
                                                  "progress", "budget", "timing", "cost", "repetitions", "split",
                                                  "protocol_id", "source_sha", "_provenance")},
                "failures": len(failures)}

    def experiment(self, artifact_id: str) -> dict:
        s = self.store
        experiment = s.get("experiment", artifact_id)
        attempts = s.find("attempt", experiment_id=artifact_id)
        listed = experiment.get("attempt_ids") if isinstance(experiment.get("attempt_ids"), list) else []
        missing = sorted(set(a for a in listed if isinstance(a, str)) - {a["artifact_id"] for a in attempts})
        failures = s.find("failure", experiment_id=artifact_id)
        reductions = s.find("reduction", experiment_id=artifact_id)
        curricula = s.find("curriculum", experiment_id=artifact_id)
        comparisons = s.find("comparison", experiment_id=artifact_id)
        return {"experiment": experiment, "links": self.links(artifact_id),
                "attempts": [self._attempt_summary(a) for a in sorted(attempts, key=lambda a: a["artifact_id"])],
                "missing_attempts": missing,
                "failures": [{**s.summary("failure", f), "finding_type": f.get("finding_type"),
                              "family_id": f.get("family_id"), "reduction_id": f.get("reduction_id")} for f in failures],
                "reductions": [s.summary("reduction", r) for r in reductions],
                "curricula": [s.summary("curriculum", c) for c in curricula],
                "comparisons": [s.summary("comparison", c) for c in comparisons]}

    def _validation_view(self, validation_id: object) -> dict | None:
        if not isinstance(validation_id, str):
            return None
        try:
            v = self.store.get("validation", validation_id)
        except ResearchError:
            return {"artifact_id": validation_id, "missing": True}
        sid = self.store.snapshot_id("validation", validation_id, v.get("snapshot_ref"))
        return {**v, "snapshot_id": sid}

    def _situation(self, snapshot_id: str | None, finding_type: str | None) -> dict | None:
        """Console derivation from public documents (existing financial chain), for the failure's type."""
        if snapshot_id is None:
            return None
        snapshot = self.store.snapshot(snapshot_id)
        calcs = [c for c in derive.scan(snapshot) if c.get("type") == finding_type]
        for calc in calcs:
            calc["summary"] = _allocation_summary(calc)
        manifest = snapshot.manifest
        return {"currency_exponents": manifest.get("currency_exponents") or {},
                "period": {"start": manifest.get("period_start"), "end": manifest.get("period_end"),
                           "timezone": manifest.get("timezone")},
                "calculations": calcs}

    def attempt(self, artifact_id: str) -> dict:
        attempt = self.store.get("attempt", artifact_id)
        sid = self.store.snapshot_id("attempt", artifact_id, attempt.get("snapshot_ref"))
        summary = self._attempt_summary(attempt)
        submission = attempt.get("submission") if summary["evaluation"] is not None or not summary["evaluation_hidden"] \
            else None
        snapshot = self.store.snapshot(sid) if sid else None
        received = received_keys(attempt.get("steps") or [])
        return {**summary, "snapshot_id": sid,
                "currency_exponents": (snapshot.manifest.get("currency_exponents") if snapshot else None) or {},
                "steps_detail": [_step_view(step) for step in attempt.get("steps") or [] if isinstance(step, dict)],
                "received": [{"key": k, "step": n, "exists": bool(snapshot and snapshot.get(k))}
                             for k, n in received.items()],
                "submission": submission, "error": attempt.get("error") if isinstance(attempt.get("error"), dict) else None,
                "started_at": attempt.get("started_at"),
                "finished_at": attempt.get("finished_at"), "episode": attempt.get("episode"),
                "raw": {k: attempt.get(k) for k in ("artifact_id", "schema_version", "source_sha", "protocol_id",
                                                    "experiment_id", "variant_id", "snapshot_ref", "episode")}}

    def failure(self, artifact_id: str) -> dict:
        s = self.store
        failure = s.get("failure", artifact_id)
        attempts = []
        for aid in failure.get("attempt_ids") or []:
            try:
                attempts.append(self.attempt(aid))
            except ResearchError:
                attempts.append({"artifact_id": aid, "missing": True})
        validation = self._validation_view(failure.get("validation_id"))
        snapshot_id = (validation or {}).get("snapshot_id") or next(
            (a.get("snapshot_id") for a in attempts if a.get("snapshot_id")), None)
        situation = self._situation(snapshot_id, failure.get("finding_type"))
        needed = []
        for calc in (situation or {}).get("calculations", []):
            keys = [op["key"] for op in calc.get("operands") or []] + [calc["rule"]["key"], calc["subject"]]
            needed.extend(k for k in keys if k not in needed)
        for attempt in attempts:
            if attempt.get("missing"):
                continue
            got = {row["key"] for row in attempt["received"]}
            attempt["needed_received"] = [{"key": k, "received": k in got} for k in needed]
        independent = None
        if validation and not validation.get("missing"):
            found = [f for f in (validation.get("independent") or {}).get("findings") or []
                     if isinstance(f, dict) and f.get("type") == failure.get("finding_type")]
            independent = {"engine": (validation.get("independent") or {}).get("engine"),
                           "status": (validation.get("independent") or {}).get("status"), "findings": found}
        return {"failure": failure, "links": self.links(failure.get("experiment_id"), artifact_id),
                "attempts": attempts, "validation": validation, "independent": independent,
                "situation": situation, "snapshot_id": snapshot_id, "needed": needed}

    def snapshot_view(self, sid: str) -> dict:
        snapshot = self.store.snapshot(sid)
        manifest = snapshot.manifest
        tables: dict[str, int] = {}
        for record in snapshot.records.values():
            tables[record.table] = tables.get(record.table, 0) + 1
        return {"snapshot_id": sid, "tables": tables, "currency_exponents": manifest.get("currency_exponents") or {},
                "period": {"start": manifest.get("period_start"), "end": manifest.get("period_end"),
                           "timezone": manifest.get("timezone")}}

    def reduction(self, artifact_id: str) -> dict:
        s = self.store
        reduction = s.get("reduction", artifact_id)

        def side(name: str) -> dict:
            part = reduction.get(name) if isinstance(reduction.get(name), dict) else {}
            sid = s.snapshot_id("reduction", artifact_id, part.get("snapshot_ref"))
            return {**part, "snapshot_id": sid, "validation": self._validation_view(part.get("validation_id")),
                    "snapshot": self.snapshot_view(sid) if sid else None}

        def attempts_of(block: dict) -> list[dict]:
            out = []
            for aid in block.get("attempt_ids") or []:
                try:
                    out.append(self._attempt_summary(s.get("attempt", aid)))
                except ResearchError:
                    out.append({"artifact_id": aid, "missing": True})
            return out

        repeats = {}
        for name, block in (reduction.get("repeats") or {}).items():
            if isinstance(block, dict):
                repeats[name] = {**block, "attempts": attempts_of(block)}
        families = []
        for family in reduction.get("families") or []:
            if not isinstance(family, dict):
                continue
            sid = s.snapshot_id("reduction", artifact_id, family.get("snapshot_ref"))
            actual = []
            for outcome in family.get("actual") or []:
                if isinstance(outcome, dict):
                    try:
                        summary = self._attempt_summary(s.get("attempt", outcome.get("attempt_id")))
                    except ResearchError:
                        summary = {"artifact_id": outcome.get("attempt_id"), "missing": True}
                    actual.append(summary)
            families.append({**family, "snapshot_id": sid, "validation": self._validation_view(family.get("validation_id")),
                             "actual": actual,
                             "currency_exponents": self.snapshot_view(sid)["currency_exponents"] if sid else {}})
        failure = s.artifacts["failure"].get(reduction.get("failure_id")) if isinstance(reduction.get("failure_id"), str) else None
        experiment = s.artifacts["experiment"].get(reduction.get("experiment_id")) if isinstance(reduction.get("experiment_id"), str) else None
        variants = {a.get("variant_id"): a.get("label") for a in (experiment or {}).get("agents") or []
                    if isinstance(a, dict) and isinstance(a.get("variant_id"), str)}
        return {"reduction": reduction, "finding_type": (failure or {}).get("finding_type"), "variants": variants,
                "links": self.links(reduction.get("experiment_id"), reduction.get("failure_id"), artifact_id),
                "original": side("original"), "reduced": side("reduced"),
                "repeats": repeats, "families": families}

    def curriculum(self, artifact_id: str) -> dict:
        curriculum = self.store.get("curriculum", artifact_id)
        training = self.store.find("training", curriculum_id=artifact_id)
        links = self.links(curriculum.get("experiment_id"), (curriculum.get("failure_ids") or [None])[0],
                           (curriculum.get("reduction_ids") or [None])[0])
        return {"curriculum": curriculum, "training": training, "links": {**links, "curriculum": artifact_id}}

    def comparison(self, artifact_id: str) -> dict:
        comparison = self.store.get("comparison", artifact_id)
        return {"comparison": comparison, "links": {**self.links(comparison.get("experiment_id")), "comparison": artifact_id}}


# ---------------------------------------------------------------------- controller
def controller_module():
    """The integrator's controller (``auditlab.research.controller``) when merged; else None."""
    module = engines._module("auditlab.research.controller")
    if module is not None and callable(getattr(module, "configured", None)) and not module.configured():
        return None
    if module is not None and all(callable(getattr(module, name, None)) for name in ("status", "start", "stop")):
        return module
    return None


def controller_status() -> dict:
    module = controller_module()
    if module is None:
        return {"connected": False, "reason": "controller_not_connected",
                "budget_configured": None, "api_key_configured": None}
    try:
        raw = module.status()
    except Exception as error:  # the integrator's component failed: report, never fake readiness
        return {"connected": True, "reason": "controller_error", "error": type(error).__name__,
                "budget_configured": None, "api_key_configured": None}
    raw = raw if isinstance(raw, dict) else {}
    return {"connected": True, "reason": raw.get("reason"),
            "budget_configured": raw.get("budget_configured") if isinstance(raw.get("budget_configured"), bool) else None,
            "api_key_configured": raw.get("api_key_configured") if isinstance(raw.get("api_key_configured"), bool) else None,
            "live": strip_private({k: v for k, v in raw.items() if k in ("experiments", "updated_at")})}


def controller_call(action: str, experiment_id: str) -> dict:
    module = controller_module()
    if module is None:
        raise ResearchError("controller_not_connected", "The integrator controller is not connected.")
    status = controller_status()
    if action == "start" and status.get("budget_configured") is not True:
        raise ResearchError("budget_not_configured", "No server-side budget is configured.")
    if action == "start" and status.get("api_key_configured") is not True:
        raise ResearchError("api_key_missing", "No provider access is configured on the server.")
    try:
        reply = getattr(module, action)(experiment_id)
    except Exception as error:
        code = getattr(error, "code", None)
        raise ResearchError(code if isinstance(code, str) else "controller_error", type(error).__name__) from error
    return strip_private(reply) if isinstance(reply, dict) else {"ok": True}
