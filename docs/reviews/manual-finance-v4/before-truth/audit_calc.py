"""Independent public-only audit calculator for review v4 (Claude Pro 2).

Written for this review from docs/SPEC.md §§4-6 and the episode's public policy.json text only.
It does not import or copy auditlab.generator or auditlab.verifier. Exact arithmetic (Fraction),
company-local dates via zoneinfo + the pinned tzdata package.

usage: python audit_calc.py PUBLIC_DIR  -> JSON {findings, notes, arithmetic}
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import datetime
from fractions import Fraction
from math import floor
from pathlib import Path
from zoneinfo import ZoneInfo


def load(public: Path):
    m = json.loads((public / "manifest.json").read_text(encoding="utf-8"))
    pol = json.loads((public / "policy.json").read_text(encoding="utf-8"))
    ledger = list(csv.DictReader((public / "ledger.csv").open(encoding="utf-8", newline="")))
    for r in ledger:
        for k in ("entity_id", "invoice_id", "payment_id", "journal_id"):
            r[k] = None if r[k] in ("null", "") else r[k]
        r["debit_minor"], r["credit_minor"] = int(r["debit_minor"]), int(r["credit_minor"])
        r["t"] = datetime.fromisoformat(r["timestamp"])
    docs = lambda n: [json.loads(l) for l in (public / n).read_text(encoding="utf-8").splitlines() if l.strip()]
    fx = {}
    for r in csv.DictReader((public / "fx.csv").open(encoding="utf-8", newline="")):
        key = (r["date"], r["source_currency"], r["target_currency"])
        fx.setdefault(key, []).append(r["rate"])
    return m, pol, ledger, docs("invoices.jsonl"), docs("payments.jsonl"), fx


def half_up(x: Fraction) -> int:
    if x < 0:
        raise ValueError("negative valuation")
    return floor(x + Fraction(1, 2))


def audit(public: Path) -> dict:
    m, pol, ledger, invoices, payments, fx = load(public)
    zone = ZoneInfo(m["timezone"])
    start, end = datetime.fromisoformat(m["period_start"]), datetime.fromisoformat(m["period_end"])
    deadline = datetime.fromisoformat(pol["allocation"]["deadline"])
    base, exps = m["base_currency"], m["currency_exponents"]
    period = lambda t: -1 if t < start else (0 if t < end else 1)
    local_date = lambda t: t.astimezone(zone).date().isoformat()
    findings, notes, arith = [], [], []
    journals = defaultdict(list)
    for r in ledger:
        journals[r["journal_id"]].append(r)
    # integrity: each journal balances per currency
    for j, rows in journals.items():
        for cur in {r["currency"] for r in rows}:
            d = sum(r["debit_minor"] for r in rows if r["currency"] == cur)
            c = sum(r["credit_minor"] for r in rows if r["currency"] == cur)
            if d != c:
                notes.append(f"unbalanced journal {j} {cur}: {d} != {c}")
    inv_by_id = defaultdict(list)
    for d in invoices:
        inv_by_id[d["invoice_id"]].append(d)
    pay_by_id = {p["payment_id"]: p for p in payments}

    # 1. payment_allocation: designated payment fully applied before the deadline (exclusive)
    for p in payments:
        un = [r for r in ledger if r["account"] == "UNAPPLIED" and r["payment_id"] == p["payment_id"]]
        before = [r for r in un if r["t"] < deadline]
        idx = p.get("settlement_index") or {}
        if sorted(idx.get("unapplied_row_ids", [])) != sorted(r["row_id"] for r in un if r["t"] < datetime.fromisoformat(idx.get("before", pol["allocation"]["deadline"]))):
            notes.append(f"settlement_index mismatch for {p['payment_id']}")
        bal = sum(r["credit_minor"] - r["debit_minor"] for r in before if r["currency"] == p["currency"])
        if p.get("allocation_instructions"):
            notes.append(f"allocation_instructions present for {p['payment_id']} (not modelled)")
        if bal > 0 and p.get("invoice_refs"):
            if len(p["invoice_refs"]) != 1:
                notes.append(f"{p['payment_id']}: positive balance with several refs")
            findings.append({"type": "payment_allocation", "affected_ids": sorted([f"payment/{p['payment_id']}"] + [f"invoice/{i}" for i in p["invoice_refs"]]),
                             "claimed_amount_minor": bal, "currency": p["currency"],
                             "evidence_ids": [f"payments/{p['doc_id']}"] + [f"ledger/{r['row_id']}" for r in sorted(before, key=lambda r: (r["t"], r["row_id"]))] + ["policy/allocation"]})
            arith.append(f"alloc {p['payment_id']}: credits-debits before {deadline.isoformat()} = {bal} {p['currency']}")
        elif bal < 0:
            notes.append(f"{p['payment_id']}: negative UNAPPLIED balance {bal}")

    # 2. fx_conversion: FX_VALUATION memorandum vs policy valuation of the source event
    for j, rows in journals.items():
        val = [r for r in rows if r["account"] == "FX_VALUATION"]
        if not val:
            continue
        recorded = sum(r["debit_minor"] - r["credit_minor"] for r in val)
        r0 = val[0]
        if r0["payment_id"]:
            p = pay_by_id[r0["payment_id"]]
            kind, key, doc, src_minor, src_cur, t = "payment", p["payment_id"], f"payments/{p['doc_id']}", p["amount_minor"], p["currency"], datetime.fromisoformat(p["timestamp"])
        else:
            ds = inv_by_id[r0["invoice_id"]]
            if len({(d["total_minor"], d["currency"], d["issued_at"]) for d in ds}) != 1:
                notes.append(f"FX source invoice {r0['invoice_id']} ambiguous"); continue
            d = ds[0]
            kind, key, doc, src_minor, src_cur, t = "invoice", d["invoice_id"], f"invoices/{d['doc_id']}", d["total_minor"], d["currency"], datetime.fromisoformat(d["issued_at"])
        date = local_date(t)
        rates = fx.get((date, src_cur, base), [])
        if len(rates) != 1:
            notes.append(f"FX {kind} {key}: {len(rates)} rates for {date} {src_cur}/{base}"); continue
        exact = Fraction(src_minor, 10 ** exps[src_cur]) * Fraction(rates[0]) * 10 ** exps[base]
        expected = half_up(exact)
        arith.append(f"fx {kind} {key}: {src_minor}/10^{exps[src_cur]} x {rates[0]} x 10^{exps[base]} = {exact} -> {expected}; recorded {abs(recorded)}"
                     + (" [EXACT HALF]" if exact.denominator == 2 else ""))
        if abs(recorded) != expected:
            findings.append({"type": "fx_conversion", "affected_ids": [f"{kind}/{key}"], "claimed_amount_minor": abs(abs(recorded) - expected),
                             "currency": base, "evidence_ids": [doc, f"fx/{date}/{src_cur}/{base}", "policy/fx_date"] + [f"ledger/{r['row_id']}" for r in val]})

    # recognition journals of invoices: invoice currency, AR debit, no payment
    recog = defaultdict(list)
    for j, rows in journals.items():
        ar = [r for r in rows if r["account"] == "AR" and r["debit_minor"] > 0 and r["payment_id"] is None and r["invoice_id"]]
        if ar and any(r["account"] == "REVENUE" for r in rows):
            recog[ar[0]["invoice_id"]].append((min(r["t"] for r in rows), j, ar))

    # 3. duplicate_invoice: several documents of one (entity, canonical key) recognised more than once
    groups = defaultdict(list)
    for d in invoices:
        groups[(d["entity_id"], d.get("canonical_key") or d["invoice_id"])].append(d)
    dup_invoices = set()
    for (entity, ckey), ds in groups.items():
        if len(ds) < 2:
            continue
        ids = {d["invoice_id"] for d in ds}
        js = sorted(x for i in ids for x in recog.get(i, []))
        totals = {d["total_minor"] for d in ds}
        if len(js) >= 2 and len(totals) == 1:
            total = totals.pop()
            recognised = sum(sum(r["debit_minor"] for r in ar) for _, _, ar in js)
            orig = js[0][2][0]["invoice_id"]
            dup_invoices |= ids
            findings.append({"type": "duplicate_invoice", "affected_ids": [f"invoice/{orig}"], "claimed_amount_minor": recognised - total,
                             "currency": ds[0]["currency"],
                             "evidence_ids": [f"invoices/{d['doc_id']}" for d in ds] + [f"ledger/{ar[0]['row_id']}" for _, _, ar in js]})
            arith.append(f"dup {ckey}: {len(ds)} docs, {len(js)} recognitions {recognised} - {total}")

    # 4. period_cutoff: invoice issue date vs first recognition; payment date vs cash posting
    for inv_id, ds in inv_by_id.items():
        if inv_id not in recog:
            continue
        d = ds[0]
        first_t, j, ar = sorted(recog[inv_id])[0]
        ev, po = period(datetime.fromisoformat(d["issued_at"])), period(first_t)
        if ev != po and 0 in (ev, po):
            findings.append({"type": "period_cutoff", "affected_ids": [f"invoice/{inv_id}"], "claimed_amount_minor": d["total_minor"],
                             "currency": d["currency"], "evidence_ids": [f"invoices/{x['doc_id']}" for x in ds] + [f"ledger/{ar[0]['row_id']}", "policy/period"]})
            arith.append(f"cutoff invoice {inv_id}: issued period {ev}, recognised period {po}, nominal {d['total_minor']} {d['currency']}")
    for p in payments:
        cash = [r for r in ledger if r["account"] == "CASH" and r["payment_id"] == p["payment_id"] and r["debit_minor"] > 0]
        if cash:
            ev, po = period(datetime.fromisoformat(p["timestamp"])), period(min(r["t"] for r in cash))
            if ev != po and 0 in (ev, po):
                findings.append({"type": "period_cutoff", "affected_ids": [f"payment/{p['payment_id']}"], "claimed_amount_minor": p["amount_minor"],
                                 "currency": p["currency"], "evidence_ids": [f"payments/{p['doc_id']}"] + [f"ledger/{r['row_id']}" for r in cash] + ["policy/period"]})
    findings.sort(key=lambda f: (f["type"], f["affected_ids"]))
    return {"episode_id": m["episode_id"], "findings": findings, "notes": notes, "arithmetic": arith}


if __name__ == "__main__":
    print(json.dumps(audit(Path(sys.argv[1])), ensure_ascii=False, indent=1))
