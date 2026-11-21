"""Reviewer's own canonical form of an economic history (public files only; no generator code).

IDs and row order are removed; times become offsets from period_start in seconds; links are kept
as positions inside a counterparty chain. Two snapshots that differ only by renamed IDs or
permuted rows have the same canonical history; a changed amount, date or link changes it.
"""
from __future__ import annotations

import csv, hashlib, io, json
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def load(public: Path):
    m = json.loads((public / "manifest.json").read_text(encoding="utf-8"))
    ledger = list(csv.DictReader(io.StringIO((public / "ledger.csv").read_text(encoding="utf-8"))))
    docs = lambda n: [json.loads(l) for l in (public / n).read_text(encoding="utf-8").splitlines() if l.strip()]
    return m, ledger, docs("invoices.jsonl"), docs("payments.jsonl")


def chains(public: Path) -> dict:
    """Counterparty chains: entity -> canonical (invoices, payments, postings). Entity-less rows form one 'treasury' chain."""
    m, ledger, invoices, payments = load(Path(public))
    t0 = datetime.fromisoformat(m["period_start"])
    rel = lambda ts: int((datetime.fromisoformat(ts) - t0).total_seconds())
    nul = lambda v: None if v in (None, "", "null") else v
    by_entity = defaultdict(lambda: {"inv": [], "pay": [], "rows": []})
    for d in invoices:
        by_entity[d["entity_id"]]["inv"].append(d)
    for p in payments:
        ent = p.get("entity_id") or next((r["entity_id"] for r in ledger if r["payment_id"] == p["payment_id"]), None)
        by_entity[nul(ent)]["pay"].append(p)
    for r in ledger:
        by_entity[nul(r["entity_id"])]["rows"].append(r)
    out = {}
    for ent, g in by_entity.items():
        # canonical order of invoices: by economic content, not by ID
        inv_key = lambda d: (rel(d["issued_at"]), d["currency"], d["total_minor"], rel(d["due_at"]), [li["amount_minor"] for li in d["line_items"]])
        invs = sorted(g["inv"], key=inv_key)
        inv_pos = {}
        for i, d in enumerate(invs):
            inv_pos.setdefault(d["invoice_id"], i)  # archive copies share an invoice_id: same position
        pays = sorted(g["pay"], key=lambda p: (rel(p["timestamp"]), p["currency"], p["amount_minor"]))
        pay_pos = {p["payment_id"]: i for i, p in enumerate(pays)}
        journals = defaultdict(list)
        for r in g["rows"]:
            journals[r["journal_id"]].append((r["account"], int(r["debit_minor"]), int(r["credit_minor"]), r["currency"], rel(r["timestamp"]),
                                               inv_pos.get(nul(r["invoice_id"]), "?" if nul(r["invoice_id"]) else None),
                                               pay_pos.get(nul(r["payment_id"]), "?" if nul(r["payment_id"]) else None)))
        postings = sorted(sorted(v) for v in journals.values())
        canon = {"invoices": [list(inv_key(d)) for d in invs],
                 "payments": [[rel(p["timestamp"]), p["currency"], p["amount_minor"], sorted(inv_pos.get(x, "?") for x in p.get("invoice_refs", []))] for p in pays],
                 "postings": postings}
        out[ent] = {"digest": _digest(canon), "canon": canon}
    return out


def whole_history(public: Path) -> str:
    m, *_ = load(Path(public))
    parts = sorted(c["digest"] for c in chains(public).values())
    return _digest({"base": m["base_currency"], "exponents": m.get("currency_exponents"), "chains": parts})


def entity_of(public: Path, affected_id: str):
    """Counterparty entity that owns an affected invoice/payment (for the defect-bearing chain)."""
    m, ledger, invoices, payments = load(Path(public))
    kind, _, key = affected_id.partition("/")
    if kind == "invoice":
        return next((d["entity_id"] for d in invoices if d["invoice_id"] == key), None)
    if kind == "payment":
        p = next((p for p in payments if p["payment_id"] == key), None)
        if p and p.get("entity_id"):
            return p["entity_id"]
        return next((r["entity_id"] for r in ledger if r["payment_id"] == key), None)
    return None
