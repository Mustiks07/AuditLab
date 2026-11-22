"""Financial laboratory over published research artifacts: related situations, error dossiers, results map.

Read-only. The browser never recomputes financial truth: every amount comes from an independent validation
artifact, from the evaluator counts inside an attempt, or from the existing console derivation over public
documents (``derive``), and each source is named. Attempts are joined to companies by the content of their
public files, never by private paths. Missing measurements stay ``not_checked``; nothing becomes zero.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import derive
from .research import Projection, ResearchError, _allocation_summary, received_keys
from .snapshot import Snapshot

PUBLIC_FILES = ("manifest.json", "policy.json", "ledger.csv", "invoices.jsonl", "payments.jsonl", "fx.csv")
# Transformations the backend can produce (auditlab.research.curriculum.families.boundary_variants) and
# the lab change of the judge workspace. The UI shows every slot; a slot without artifacts is "not available".
TRANSFORMATIONS = ("id_order", "lawful_refund", "fixed", "currency", "boundary")
OUTCOMES = ("correct", "miss", "false_accusation", "wrong_amount", "wrong_evidence", "unverified", "technical",
            "not_evaluated")
DIMENSIONS = ("violation", "interaction", "clean", "currency", "boundary", "difficulty", "transformation")
COVERED_TYPES = {"payment_allocation", "fx_conversion"}  # types the console derivation can recompute


def content_key(public: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        for name in PUBLIC_FILES:
            digest.update(name.encode() + b"\0" + hashlib.sha256((public / name).read_bytes()).digest())
    except OSError:
        return None
    return digest.hexdigest()


def _claims(attempt: dict) -> list[dict]:
    return [f for f in ((attempt.get("submission") or {}).get("findings") or []) if isinstance(f, dict)]


class Laboratory:
    def __init__(self, projection: Projection):
        self.p = projection
        self.s = projection.store
        self._keys: dict[str, str | None] = {}
        self._snapshots: dict[str, Snapshot] = {}
        self._labels: dict[str, str] | None = None
        self._ref_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------ joins
    def _sid(self, kind: str, data: dict) -> str | None:
        return self.s.snapshot_id(kind, data["artifact_id"], data.get("snapshot_ref"))

    def key_of(self, kind: str, data: dict) -> str | None:
        sid = self._sid(kind, data)
        if sid is None:
            return None
        if sid not in self._keys:
            self._keys[sid] = content_key(self.s.snapshots[sid])
        return self._keys[sid]

    def snapshot(self, sid: str) -> Snapshot:
        if sid not in self._snapshots:
            self._snapshots[sid] = self.s.snapshot(sid)
        return self._snapshots[sid]

    def candidates_by_key(self) -> dict[str, dict]:
        return {k: c for c in self.s.artifacts["candidate"].values() if (k := self.key_of("candidate", c))}

    def attempts_by_key(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for attempt in self.s.artifacts["attempt"].values():
            key = self.key_of("attempt", attempt)
            if key:
                out.setdefault(key, []).append(attempt)
        return out

    def validation_for(self, key: str | None, candidate: dict | None = None) -> dict | None:
        if candidate and isinstance(candidate.get("validation_id"), str):
            found = self.s.artifacts["validation"].get(candidate["validation_id"])
            if found:
                return found
        if key is None:
            return None
        return next((v for v in self.s.artifacts["validation"].values() if self.key_of("validation", v) == key), None)

    def labels(self) -> dict[str, str]:
        """Human company names; internal ids stay in technical details."""
        if self._labels is None:
            names = {"period_boundary": "Граница периода", "standard_book": "Обычная книга",
                     "cross_currency": "Две валюты", "archive_controls": "Архивные копии"}
            variants = {"id_order": "переименованы документы", "lawful_refund": "законный возврат",
                        "fixed": "исправлено до границы", "currency": "договор в другой валюте"}
            labels, counters = {}, {}
            originals = sorted((c for c in self.s.artifacts["candidate"].values() if c.get("role") != "variant"),
                               key=lambda c: c["artifact_id"])
            for c in originals:
                stratum = (c.get("design") or {}).get("stratum")
                group = names.get(stratum, "Компания")
                counters[group] = counters.get(group, 0) + 1
                labels[c["artifact_id"]] = f"{group} · компания {counters[group]}"
            for c in self.s.artifacts["candidate"].values():
                if c.get("role") == "variant":
                    parent = labels.get(c.get("parent_id"), "Компания")
                    labels[c["artifact_id"]] = f"{parent} · {variants.get(c.get('transformation'), 'вариант')}"
            self._labels = labels
        return self._labels

    def variant_labels(self) -> dict[str, str]:
        out = {}
        for experiment in self.s.artifacts["experiment"].values():
            for agent in experiment.get("agents") or []:
                if isinstance(agent, dict) and isinstance(agent.get("variant_id"), str):
                    out[f"{experiment['artifact_id']}/{agent['variant_id']}"] = agent.get("label") or agent["variant_id"]
        return out

    # ------------------------------------------------------------------ reference and situation
    def reference(self, sid: str | None, key: str | None, candidate: dict | None = None) -> dict:
        """The correct answer as published: independent validation first, then the console derivation."""
        cache = f"{sid}:{key}:{(candidate or {}).get('artifact_id')}"
        if cache in self._ref_cache:
            return self._ref_cache[cache]
        validation = self.validation_for(key, candidate)
        design = (candidate or {}).get("design") or {}
        if validation and (validation.get("independent") or {}).get("status") == "passed":
            ind = validation["independent"]
            ref = {"source": "independent", "status": "passed", "engine": ind.get("engine"),
                   "validation_id": validation["artifact_id"], "findings": ind.get("findings") or [],
                   "arithmetic": ind.get("arithmetic") or [], "complete": True}
        elif sid is not None:
            snapshot = self.snapshot(sid)
            found = [{"type": c["type"], "claimed_amount_minor": c["result_minor"], "currency": c["currency"],
                      "affected_ids": c.get("affected_ids") or [], "evidence_ids": c.get("evidence") or []}
                     for c in derive.scan(snapshot)]
            types = set(design.get("issue_types") or [])
            ref = {"source": "console", "status": "derived", "engine": "Расчёт консоли по публичным документам",
                   "findings": found, "arithmetic": [], "complete": bool(design) and types <= COVERED_TYPES,
                   "note": "Консоль пересчитывает только остаток платежа и курсовую оценку; другие типы не проверены."}
        else:
            ref = {"source": "none", "status": "not_checked", "findings": [], "arithmetic": [], "complete": False}
        if design:
            ref["design_clean"] = bool(design.get("clean"))
        self._ref_cache[cache] = ref
        return ref

    def situation(self, sid: str | None, reference: dict, fallback_payment: str | None = None) -> dict | None:
        """Received / allocated before the boundary / residual of the designated payment (console derivation)."""
        if sid is None:
            return None
        snapshot = self.snapshot(sid)
        payment = None
        for finding in reference.get("findings") or []:
            if finding.get("type") == "payment_allocation":
                payment = next((snapshot.resolve_affected(a) for a in finding.get("affected_ids") or []
                                if str(a).startswith("payment/")), None)
                if payment is None:
                    payment = next((snapshot.get(e) for e in finding.get("evidence_ids") or [] if str(e).startswith("payments/")), None)
                if payment:
                    break
        if payment is None and fallback_payment:
            payment = snapshot.get(fallback_payment)
        if payment is None:
            return {"designated": None, "currency_exponents": snapshot.manifest.get("currency_exponents") or {},
                    "period": self._period(snapshot), "currencies": sorted(snapshot.manifest.get("currency_exponents") or {})}
        calc = derive.allocation(snapshot, payment)
        return {"designated": {"payment_key": payment.key, "summary": _allocation_summary(calc),
                               "evidence": calc.get("evidence") or [], "formula": calc.get("formula"),
                               "operands": calc.get("operands") or [], "context": calc.get("context") or []},
                "currency_exponents": snapshot.manifest.get("currency_exponents") or {},
                "period": self._period(snapshot), "currencies": sorted(snapshot.manifest.get("currency_exponents") or {})}

    @staticmethod
    def _period(snapshot: Snapshot) -> dict:
        m = snapshot.manifest
        return {"start": m.get("period_start"), "end": m.get("period_end"), "timezone": m.get("timezone")}

    # ------------------------------------------------------------------ outcome
    def outcome(self, attempt: dict, reference: dict) -> dict:
        """Observed outcome of one attempt from evaluator counts and the published reference answer."""
        status = attempt.get("status")
        error = attempt.get("error") if isinstance(attempt.get("error"), dict) else None
        if status != "completed":
            return {"primary": "technical", "kinds": ["technical"], "claims": [], "error": error, "status": status}
        evaluation = attempt.get("evaluation") if isinstance(attempt.get("evaluation"), dict) else None
        if not evaluation or evaluation.get("visibility") != "open" or evaluation.get("tp") is None:
            return {"primary": "not_evaluated", "kinds": ["not_evaluated"], "claims": [], "error": error, "status": status}
        tp, fp, fn = evaluation.get("tp", 0), evaluation.get("fp", 0), evaluation.get("fn", 0)
        if any(type(v) is not int for v in (tp, fp, fn)):
            return {"primary": "not_evaluated", "kinds": ["not_evaluated"], "claims": [], "error": error, "status": status}
        diagnosis = {d['index']: d['label'] for d in evaluation.get('finding_diagnostics', [])}
        labels = {'correct': 'accepted', 'wrong_amount': 'wrong_amount', 'wrong_currency': 'wrong_amount',
                  'evidence_incomplete': 'wrong_evidence', 'evidence_unavailable': 'wrong_evidence',
                  'false_accusation_on_clean': 'false_accusation', 'duplicate': 'false_accusation'}
        findings = reference.get("findings") or []
        claims = []
        for index, claim in enumerate(_claims(attempt)):
            affected = set(claim.get("affected_ids") or [])
            same = [f for f in findings if f.get("type") == claim.get("type")
                    and (not affected or not f.get("affected_ids") or affected & set(f["affected_ids"]))]
            exact = [f for f in same if f.get("claimed_amount_minor") == claim.get("claimed_amount_minor")
                     and f.get("currency") == claim.get("currency")]
            if index in diagnosis:
                kind = labels.get(diagnosis[index], 'unverified')
            elif tp and fp:
                # Aggregate counts cannot tell which particular finding matched.
                kind = 'unverified'
            elif fp == 0:
                kind = "accepted"
            elif not same and (reference.get("complete") or claim.get("type") in COVERED_TYPES
                               or reference.get("source") == "independent"
                               or any(f.get("type") == claim.get("type") for f in findings)):
                kind = "false_accusation"
            elif not same:
                kind = "unverified"
            elif not exact:
                kind = "wrong_amount"
            else:
                kind = "wrong_evidence"
            claims.append({"type": claim.get("type"), "claimed_amount_minor": claim.get("claimed_amount_minor"),
                           "currency": claim.get("currency"), "evidence_ids": claim.get("evidence_ids") or [],
                           "affected_ids": sorted(affected),
                           "kind": kind, "reference": (exact or same or [None])[0]})
        kinds = []
        for kind in ("wrong_amount", "wrong_evidence", "false_accusation"):
            if any(c["kind"] == kind for c in claims):
                kinds.append(kind)
        if fp and not kinds:
            kinds.append("unverified" if any(c["kind"] == "unverified" for c in claims) else "false_accusation")
        if fn > 0:
            kinds.append("miss")
        if not kinds:
            kinds.append("correct")
        return {"primary": kinds[0], "kinds": kinds, "claims": claims, "error": None, "status": status,
                "evaluation": {k: evaluation.get(k) for k in ("tp", "fp", "fn", "reward", "inspection_steps")}}

    def _attempt_row(self, attempt: dict, reference: dict, labels: dict) -> dict:
        evaluation = attempt.get("evaluation") if isinstance(attempt.get("evaluation"), dict) else None
        experiment = attempt.get("experiment_id")
        return {"artifact_id": attempt["artifact_id"], "experiment_id": experiment,
                "variant_id": attempt.get("variant_id"), "agent": labels.get(f"{experiment}/{attempt.get('variant_id')}") or "Агент",
                "repetition": attempt.get("repetition"), "status": attempt.get("status"),
                "family_id": (attempt.get("episode") or {}).get("family_id"),
                "outcome": self.outcome(attempt, reference),
                "claims": [{k: f.get(k) for k in ("type", "claimed_amount_minor", "currency")} for f in _claims(attempt)],
                "evaluation": {k: evaluation.get(k) for k in ("tp", "fp", "fn", "reward")}
                if evaluation and evaluation.get("visibility") == "open" else None,
                "usage": attempt.get("usage"), "error": attempt.get("error"),
                "_provenance": attempt.get("_provenance")}

    # ------------------------------------------------------------------ families (related situations)
    def overview(self) -> dict:
        labels = self.labels()
        families = []
        attempts = self.attempts_by_key()
        for c in sorted(self.s.artifacts["candidate"].values(), key=lambda c: c["artifact_id"]):
            if c.get("role") == "variant":
                continue
            variants = [v for v in self.s.artifacts["candidate"].values() if v.get("parent_id") == c["artifact_id"]]
            keys = [self.key_of("candidate", x) for x in [c, *variants]]
            families.append({"candidate_id": c["artifact_id"], "label": labels.get(c["artifact_id"]),
                             "design": c.get("design"), "split": c.get("split"),
                             "transformations": sorted(v.get("transformation") for v in variants),
                             "attempts": sum(len(attempts.get(k, [])) for k in keys if k),
                             "_provenance": c["_provenance"]})
        families.sort(key=lambda f: (-len(f["transformations"]), f["label"] or ""))
        return {"families": families, "experiments": [{"artifact_id": e["artifact_id"], "title": e.get("title"),
                "status": e.get("status"), "progress": e.get("progress"), "_provenance": e["_provenance"],
                "agents": [a.get("label") for a in e.get("agents") or [] if isinstance(a, dict) and a.get("label")]}
                for e in self.s.artifacts["experiment"].values()]}

    def _situation_card(self, candidate: dict, attempts: dict, labels: dict, agent_labels: dict,
                        fallback_payment: str | None = None) -> dict:
        key = self.key_of("candidate", candidate)
        sid = self._sid("candidate", candidate)
        reference = self.reference(sid, key, candidate)
        rows = [self._attempt_row(a, reference, agent_labels) for a in sorted(attempts.get(key, []), key=lambda a: a["artifact_id"])]
        return {"candidate_id": candidate["artifact_id"], "label": labels.get(candidate["artifact_id"]),
                "role": candidate.get("role", "original"), "transformation": candidate.get("transformation"),
                "change": candidate.get("change"), "reason": candidate.get("reason"), "design": candidate.get("design"),
                "expected": candidate.get("expected"), "expected_check": candidate.get("expected_check"),
                "snapshot_id": sid, "reference": reference,
                "situation": self.situation(sid, reference, fallback_payment),
                "attempts": rows, "_provenance": candidate["_provenance"],
                "raw": {k: candidate.get(k) for k in ("artifact_id", "family_id", "split", "snapshot_ref", "source_sha",
                                                      "protocol_id", "validation_id", "parent_id")}}

    def family(self, candidate_id: str) -> dict:
        original = self.s.get("candidate", candidate_id)
        if original.get("role") == "variant":
            original = self.s.get("candidate", original["parent_id"])
        labels, agent_labels, attempts = self.labels(), self.variant_labels(), self.attempts_by_key()
        base = self._situation_card(original, attempts, labels, agent_labels)
        payment = ((base.get("situation") or {}).get("designated") or {}).get("payment_key")
        variants = sorted((v for v in self.s.artifacts["candidate"].values() if v.get("parent_id") == original["artifact_id"]),
                          key=lambda v: TRANSFORMATIONS.index(v["transformation"]) if v.get("transformation") in TRANSFORMATIONS else 99)
        cards = [self._situation_card(v, attempts, labels, agent_labels, payment) for v in variants]
        present = {c["transformation"] for c in cards}
        return {"original": base, "variants": cards,
                "unavailable": [t for t in TRANSFORMATIONS if t not in present],
                "agents": agent_labels}

    # ------------------------------------------------------------------ dossier
    def dossier(self, attempt_id: str) -> dict:
        attempt = self.s.get("attempt", attempt_id)
        detail = self.p.attempt(attempt_id)
        key = self.key_of("attempt", attempt)
        sid = detail.get("snapshot_id")
        candidate = self.candidates_by_key().get(key) if key else None
        reference = self.reference(sid, key, candidate)
        agent_labels = self.variant_labels()
        outcome = self.outcome(attempt, reference)
        situation = self.situation(sid, reference)
        needed = []
        designated = (situation or {}).get("designated")
        for finding in reference.get("findings") or []:
            for k in finding.get("evidence_ids") or []:
                if k not in needed:
                    needed.append(k)
        if not needed and designated:
            needed = list(designated["evidence"])
        got = received_keys(attempt.get("steps") or [])
        evidence = [{"key": k, "received": k in got, "step": got.get(k)} for k in needed]
        claimed = []
        order = {"wrong_amount": 0, "wrong_evidence": 1, "accepted": 2, "unverified": 3, "false_accusation": 4}
        for claim in sorted(outcome["claims"], key=lambda c: order.get(c["kind"], 5)):
            claimed.append({**claim, "evidence": [{"key": k, "received": k in got, "step": got.get(k),
                                                   "in_reference": k in needed} for k in claim["evidence_ids"]]})
        same_company = self.attempts_by_key().get(key, []) if key else []
        repeats = [self._attempt_row(a, reference, agent_labels) for a in sorted(same_company, key=lambda a: a["artifact_id"])
                   if a.get("variant_id") == attempt.get("variant_id") and a.get("experiment_id") == attempt.get("experiment_id")]
        others = [self._attempt_row(a, reference, agent_labels) for a in sorted(same_company, key=lambda a: a["artifact_id"])
                  if not (a.get("variant_id") == attempt.get("variant_id") and a.get("experiment_id") == attempt.get("experiment_id"))]
        reduced = None
        for reduction in self.s.artifacts["reduction"].values():
            original = reduction.get("original") if isinstance(reduction.get("original"), dict) else {}
            ok = self.s.snapshot_id("reduction", reduction["artifact_id"], original.get("snapshot_ref"))
            ids = [a for block in (reduction.get("repeats") or {}).values() if isinstance(block, dict)
                   for a in block.get("attempt_ids") or []]
            if attempt_id in ids or (ok and key and content_key(self.s.snapshots[ok]) == key):
                reduced = {"reduction_id": reduction["artifact_id"],
                           "ledger_rows": [original.get("ledger_rows"), (reduction.get("reduced") or {}).get("ledger_rows")],
                           "repeats": {n: {k: b.get(k) for k in ("failed", "total")} for n, b in (reduction.get("repeats") or {}).items()
                                       if isinstance(b, dict)}}
        control = self._control(candidate, attempt, agent_labels)
        labels = self.labels()
        company = labels.get(candidate["artifact_id"]) if candidate else (
            "Компания прогона: расчёт консоли нашёл нарушение" if reference.get("findings")
            else "Компания прогона: расчёт консоли расхождений не нашёл")
        return {"attempt": detail, "agent": agent_labels.get(f"{attempt.get('experiment_id')}/{attempt.get('variant_id')}") or "Агент",
                "company": company,
                "candidate_id": candidate["artifact_id"] if candidate else None,
                "family_root": (candidate.get("parent_id") or candidate["artifact_id"]) if candidate else None,
                "reference": reference, "outcome": outcome, "situation": situation,
                "evidence": evidence, "claimed": claimed, "repeats": repeats, "others": others,
                "reduced": reduced, "control": control}

    def _control(self, candidate: dict | None, attempt: dict, agent_labels: dict) -> dict | None:
        """The corrected or clean counterpart of this company and how the same agent answered there."""
        if candidate is None:
            return None
        root = candidate.get("parent_id") or candidate["artifact_id"]
        family = [c for c in self.s.artifacts["candidate"].values()
                  if c["artifact_id"] != candidate["artifact_id"] and (c.get("parent_id") == root or c["artifact_id"] == root)]
        fixed = next((c for c in family if c.get("transformation") == "fixed"), None)
        if fixed is None and not (candidate.get("design") or {}).get("clean"):
            fixed = next((c for c in family if (c.get("design") or {}).get("clean")), None)
        if fixed is None:
            return {"available": False, "reason": "Для этой компании не опубликован исправленный или чистый вариант."}
        key = self.key_of("candidate", fixed)
        sid = self._sid("candidate", fixed)
        reference = self.reference(sid, key, fixed)
        same = [a for a in self.attempts_by_key().get(key, [])
                if a.get("variant_id") == attempt.get("variant_id") and a.get("experiment_id") == attempt.get("experiment_id")]
        return {"available": True, "candidate_id": fixed["artifact_id"], "label": self.labels().get(fixed["artifact_id"]),
                "transformation": fixed.get("transformation"), "expected": fixed.get("expected"), "reference": reference,
                "attempts": [self._attempt_row(a, reference, agent_labels) for a in same], "family_root": root}

    # ------------------------------------------------------------------ results map
    def _facets(self, candidate: dict | None, snapshot: Snapshot | None) -> dict:
        design = (candidate or {}).get("design") or {}
        unknown = "unknown"
        types = design.get("issue_types")
        currencies = sorted((snapshot.manifest.get("currency_exponents") or {}) if snapshot else []) or design.get("currencies")
        if not design:
            difficulty = unknown
        elif design.get("clean"):
            difficulty = "clean"
        elif design.get("interaction"):
            difficulty = "interaction"
        else:
            difficulty = "single" if len(types or []) == 1 else "multiple"
        return {"violation": (types or ["clean"]) if design else [unknown],
                "interaction": [design.get("interaction") or "none"] if design else [unknown],
                "clean": ["clean" if design.get("clean") else "defect"] if design else [unknown],
                "currency": ["+".join(currencies)] if currencies else [unknown],
                "boundary": [design.get("settlement_timing") or "none"] if design else [unknown],
                "difficulty": [difficulty],
                "transformation": [(candidate or {}).get("transformation") or "original"] if candidate else [unknown]}

    def results_map(self, experiment: str | None = None, before: str | None = None, after: str | None = None) -> dict:
        agent_labels = self.variant_labels()
        candidates = self.candidates_by_key()
        rows = []
        for attempt in sorted(self.s.artifacts["attempt"].values(), key=lambda a: a["artifact_id"]):
            if experiment and attempt.get("experiment_id") != experiment:
                continue
            key = self.key_of("attempt", attempt)
            sid = self._sid("attempt", attempt)
            candidate = candidates.get(key) if key else None
            reference = self.reference(sid, key, candidate)
            row = self._attempt_row(attempt, reference, agent_labels)
            row["company_key"] = key
            row["company"] = self.labels().get(candidate["artifact_id"]) if candidate else None
            row["facets"] = self._facets(candidate, self.snapshot(sid) if sid else None)
            row["column"] = f"{attempt.get('experiment_id')}/{attempt.get('variant_id')}"
            rows.append(row)
        columns = sorted({r["column"] for r in rows})
        names = {}
        for i, key in enumerate(sorted({r["company_key"] for r in rows if r["company_key"] and not r["company"]})):
            names[key] = f"Компания без опубликованного описания {i + 1}"
        for r in rows:
            r["company"] = r["company"] or names.get(r["company_key"]) or "Снимок компании не опубликован"
        dimensions = {}
        for dimension in DIMENSIONS:
            values = sorted({v for r in rows for v in r["facets"][dimension]})
            table = []
            for value in values:
                cells = {}
                for column in columns:
                    inside = [r for r in rows if r["column"] == column and value in r["facets"][dimension]]
                    counts = {o: 0 for o in OUTCOMES}
                    for r in inside:
                        counts[r["outcome"]["primary"] if r["outcome"]["primary"] in counts else "not_evaluated"] += 1
                    cells[column] = {"attempts": len(inside),
                                     "families": len({r["family_id"] for r in inside if r["family_id"]}),
                                     "companies": len({r["company_key"] for r in inside if r["company_key"]}),
                                     "outcomes": counts, "state": "not_checked" if not inside
                                     else "partial" if counts["technical"] or counts["not_evaluated"] else "measured",
                                     "attempt_ids": [r["artifact_id"] for r in inside]}
                table.append({"value": value, "cells": cells})
            dimensions[dimension] = table
        experiments = {e["artifact_id"]: e for e in self.s.artifacts["experiment"].values()}
        incomplete = []
        for eid, e in experiments.items():
            if experiment and eid != experiment:
                continue
            p = e.get("progress") or {}
            if isinstance(p.get("planned"), int) and isinstance(p.get("completed"), int) and p["completed"] < p["planned"]:
                incomplete.append({"experiment_id": eid, "title": e.get("title"), "planned": p["planned"],
                                   "completed": p["completed"], "failed": p.get("failed") or {}})
        before, after = self._default_pair(columns, before, after)
        return {"columns": [{"id": c, "label": agent_labels.get(c) or "Агент",
                             "experiment": experiments.get(c.split("/", 1)[0], {}).get("title")} for c in columns],
                "dimensions": dimensions, "rows": [{k: r[k] for k in ("artifact_id", "company", "company_key", "agent", "column",
                                                                     "outcome", "status", "family_id", "error", "_provenance")}
                                                  for r in rows],
                "incomplete": incomplete, "experiments": [{"artifact_id": k, "title": v.get("title"),
                                                            "_provenance": v["_provenance"]} for k, v in experiments.items()],
                "regression": self._regression(rows, before, after) if before and after else None}

    def _default_pair(self, columns, before, after):
        if before in columns and after in columns:
            return before, after
        for comparison in self.s.artifacts["comparison"].values():
            for pair in comparison.get("paired") or []:
                a = f"{comparison.get('experiment_id')}/{pair.get('a')}"
                b = f"{comparison.get('experiment_id')}/{pair.get('b')}"
                if a in columns and b in columns:
                    return a, b
        return (columns[0], columns[1]) if len(columns) > 1 else (None, None)

    @staticmethod
    def _regression(rows, before, after) -> dict:
        def by_company(column):
            out = {}
            for r in rows:
                if r["column"] == column and r["company_key"]:
                    out.setdefault(r["company_key"], []).append(r)
            return out
        a, b = by_company(before), by_company(after)
        items = []
        for key in sorted(set(a) | set(b)):
            left, right = a.get(key, []), b.get(key, [])
            def state(group):
                if not group:
                    return "missing"
                if any(r["outcome"]["primary"] in ("technical", "not_evaluated") for r in group):
                    return "unavailable"
                return "correct" if all(r["outcome"]["primary"] == "correct" for r in group) else "wrong"
            s1, s2 = state(left), state(right)
            if "missing" in (s1, s2) or "unavailable" in (s1, s2):
                change = "not_comparable"
            elif s1 == s2:
                change = "unchanged_correct" if s1 == "correct" else "unchanged_wrong"
            else:
                change = "regression" if s1 == "correct" else "improvement"
            sample = (left or right)[0]
            items.append({"company": sample["company"], "company_key": key, "before": s1, "after": s2, "change": change,
                          "before_attempts": [r["artifact_id"] for r in left], "after_attempts": [r["artifact_id"] for r in right],
                          "before_outcome": [r["outcome"]["primary"] for r in left],
                          "after_outcome": [r["outcome"]["primary"] for r in right]})
        counts = {k: sum(1 for i in items if i["change"] == k) for k in
                  ("regression", "improvement", "unchanged_correct", "unchanged_wrong", "not_comparable")}
        return {"before": before, "after": after, "items": items, "counts": counts}


def lab(projection: Projection) -> Laboratory:
    return Laboratory(projection)


__all__ = ["Laboratory", "lab", "content_key", "ResearchError", "TRANSFORMATIONS", "OUTCOMES", "DIMENSIONS"]
