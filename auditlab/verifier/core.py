"""Assemble verifier findings from the SQL views. No money is computed here."""
from __future__ import annotations

from pathlib import Path
import re

from auditlab.contracts import MAX_EVIDENCE, MAX_EXPLANATION, Finding

from .engine import Database, default_engine, sql_files
from .loader import load_snapshot

CLASSES = ("payment_allocation", "fx_conversion", "period_cutoff", "duplicate_invoice")
REPORT_VERSION = "1.0"
RULES_VERSION = "verifier-rules-1.0"


def _natural(text: str):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def finding_key(finding: dict, amount_field: str = "claimed_amount_minor") -> tuple:
    return (finding["type"], tuple(sorted(finding["affected_ids"])),
            int(finding[amount_field]), finding["currency"])


def verify_report(public_dir: Path | str, engine: str | None = None) -> dict:
    """Full machine-readable report: findings with derivation, notes, engine and input hashes."""
    snapshot = load_snapshot(public_dir)
    engine = engine or default_engine()
    database = Database(engine)
    try:
        database.load(snapshot.tables)
        database.run_rules()
        details = []
        for kind in CLASSES:
            rows = database.query(f"SELECT finding_key, type, currency, amount_minor, rule, arithmetic "
                                  f"FROM f_{kind} ORDER BY finding_key")
            affected = {}
            for key, value in database.query(f"SELECT finding_key, affected_id FROM a_{kind}"):
                affected.setdefault(key, set()).add(value)
            evidence = {}
            for key, value, prio, ts in database.query(
                    f"SELECT finding_key, evidence_id, prio, ts FROM e_{kind}"):
                evidence.setdefault(key, {})[value] = min((prio, ts), evidence.get(key, {}).get(value, (prio, ts)))
            for key, kind_name, currency, amount, rule, arithmetic in rows:
                ranked = sorted(evidence.get(key, {}).items(), key=lambda item: (item[1], _natural(item[0])))
                ids = [value for value, _ in ranked]
                details.append({
                    "finding_key": key, "type": kind_name, "currency": currency,
                    "amount_minor": int(amount), "affected_ids": sorted(affected.get(key, set()), key=_natural),
                    "evidence_ids": ids[:MAX_EVIDENCE], "evidence_truncated": ids[MAX_EVIDENCE:],
                    "rule": rule, "arithmetic": arithmetic})
        notes = list(snapshot.notes)
        notes += [{"kind": k, "subject": s, "detail": d}
                  for k, s, d in database.query("SELECT kind, subject, detail FROM notes")]
        for item in details:
            if item["evidence_truncated"]:
                notes.append({"kind": "ambiguity", "subject": item["finding_key"],
                              "detail": f"evidence capped at {MAX_EVIDENCE}; dropped "
                                        + ", ".join(item["evidence_truncated"])})
        notes.sort(key=lambda note: (note["kind"], _natural(note["subject"]), note["detail"]))
    finally:
        database.close()
    findings: list[Finding] = []
    for item in details:
        explanation = f"{item['rule']}: {item['arithmetic']}"
        findings.append({"type": item["type"], "affected_ids": item["affected_ids"],
                         "claimed_amount_minor": item["amount_minor"], "currency": item["currency"],
                         "evidence_ids": item["evidence_ids"],
                         "explanation": explanation[:MAX_EXPLANATION]})
    findings.sort(key=lambda f: (f["type"], [_natural(a) for a in f["affected_ids"]],
                                 f["claimed_amount_minor"], f["currency"]))
    return {
        "report_version": REPORT_VERSION, "rules_version": RULES_VERSION,
        "episode_id": snapshot.episode_id, "engine": engine, "engine_version": database.version,
        "sql_files": [path.name for path in sql_files()],
        "input_sha256": snapshot.input_sha256, "period_end": snapshot.period_end,
        "allocation_deadline": snapshot.deadline, "findings": findings, "derivations": details,
        "notes": notes,
    }


def verify_snapshot(public_dir: Path | str) -> list[Finding]:
    """INTERFACES.md entry point: findings reproduced from public/ only."""
    return verify_report(public_dir)["findings"]
