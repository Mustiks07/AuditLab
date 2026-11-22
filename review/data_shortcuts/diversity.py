"""Section 4: does each profile-1.2 stratum realise its financial situation, and is its clean control fair?

Everything is read from public files (plus the case's own private source book for the pair check).
Truth is used only to confirm that the reviewer's own answer matches it after the answer is computed.
"""
from __future__ import annotations

import csv, io, json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from review.agent_generalization.pairs import matched_clean_control
from review.genesis_acceptance.independent_calc import audit


def _book(pub: Path):
    m = json.loads((pub / "manifest.json").read_text(encoding="utf-8"))
    ledger = list(csv.DictReader(io.StringIO((pub / "ledger.csv").read_text(encoding="utf-8"))))
    docs = {t: [json.loads(l) for l in (pub / f"{t}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()] for t in ("invoices", "payments")}
    pol = json.loads((pub / "policy.json").read_text(encoding="utf-8"))
    return m, ledger, docs, pol


def situation(item: dict) -> dict:
    """Facts that must hold for the stratum, computed from public data."""
    ep = Path(item["dir"]); pub = ep / "public"
    m, ledger, docs, pol = _book(pub)
    end = datetime.fromisoformat(m["period_end"]); deadline = datetime.fromisoformat(pol["allocation"]["deadline"])
    ans = audit(pub)
    truth = json.loads((ep / "private" / "truth.json").read_text(encoding="utf-8"))
    sig = lambda f, a: (f["type"], tuple(sorted(f["affected_ids"])), f[a], f["currency"])
    facts = {"answer_matches_truth": sorted(sig(f, "claimed_amount_minor") for f in ans["findings"]) == sorted(sig(i, "expected_amount_minor") for i in truth["issues"]),
             "findings": Counter(f["type"] for f in ans["findings"]), "exponents": m["currency_exponents"]}
    cfg, s = item["config"], item["stratum"]
    fc = cfg.get("financial_case") or {}
    alloc = [f for f in ans["findings"] if f["type"] == "payment_allocation"]
    if alloc:
        pid = next(a.split("/", 1)[1] for a in alloc[0]["affected_ids"] if a.startswith("payment/"))
        pay = next(p for p in docs["payments"] if p["payment_id"] == pid)
        debits = [r for r in ledger if r["payment_id"] == pid and r["account"] == "UNAPPLIED" and int(r["debit_minor"]) > 0]
        before = [r for r in debits if datetime.fromisoformat(r["timestamp"]) < deadline]
        late = [r for r in debits if datetime.fromisoformat(r["timestamp"]) >= deadline]
        facts.update(alloc_amount=pay["amount_minor"], alloc_remaining=alloc[0]["claimed_amount_minor"], tranches_before=len(before),
                     tranches_at_or_after=len(late), late_offsets_min=sorted(int((datetime.fromisoformat(r["timestamp"]) - deadline).total_seconds() // 60) for r in late))
    ok = facts["answer_matches_truth"]
    if s in ("alloc_partial", "jpy_alloc_partial"):
        ok &= bool(alloc) and 0 < facts["alloc_remaining"] < facts["alloc_amount"] and facts["tranches_before"] == fc["tranches"]
    elif s == "alloc_late_tranche":
        # `tranches` applied tranches stay before the boundary; the remainder is applied once, offset minutes after it
        ok &= bool(alloc) and facts["tranches_at_or_after"] == 1 and facts["tranches_before"] == fc["tranches"] \
            and facts["late_offsets_min"] == [fc["settlement_offset_minutes"]]
    elif s in ("alloc_omitted", "kwd_alloc_omitted"):
        ok &= bool(alloc) and facts["alloc_remaining"] == facts["alloc_amount"] and facts["tranches_before"] == 0
    elif s.startswith("cutoff"):
        ok &= facts["findings"].get("period_cutoff", 0) == 1
    elif s == "duplicate":
        ok &= facts["findings"].get("duplicate_invoice", 0) == 1
    elif s in ("fx", "usd_base_fx"):
        ok &= facts["findings"].get("fx_conversion", 0) == 1
    elif s.startswith("interaction"):
        ok &= sum(facts["findings"].values()) == 2
    elif s == "two_types_duplicate_allocation":
        ok &= facts["findings"].get("duplicate_invoice", 0) == 1 and facts["findings"].get("payment_allocation", 0) == 1
    elif s.startswith("clean"):
        ok &= not ans["findings"]
    if s.startswith("jpy"): ok &= m["currency_exponents"]["JPY"] == 0
    if s.startswith("kwd"): ok &= m["currency_exponents"]["KWD"] == 3
    # lawful look-alikes present in the book
    pay_by_inv = Counter(x for p in docs["payments"] for x in p.get("invoice_refs", []))
    by_amount = defaultdict(set)
    for d in docs["invoices"]:
        by_amount[d["total_minor"]].add(d["entity_id"])
    refunds = [p for p in docs["payments"] if not p.get("invoice_refs")
               and sum(int(r["credit_minor"]) - int(r["debit_minor"]) for r in ledger if r["payment_id"] == p["payment_id"] and r["account"] == "UNAPPLIED") == 0]
    facts["lookalikes"] = {"split_payment": sum(1 for v in pay_by_inv.values() if v >= 2),
                           "equal_amount_different_customers": sum(1 for v in by_amount.values() if len(v) >= 2),
                           "archive_copies": sum(1 for v in Counter(d["invoice_id"] for d in docs["invoices"]).values() if v >= 2),
                           "refunds_with_zero_balance": len(refunds)}
    facts["status"] = "passed" if ok else "failed"
    facts["findings"] = dict(facts["findings"])
    return facts


def run(items: list[dict]) -> dict:
    rows, pairs = [], []
    by = defaultdict(dict)
    for it in items:
        if it.get("status") == "rejected":
            continue
        by[it["family"]][it["variant"]] = it
        if it["variant"] == "case":
            rows.append({"family": it["family"], "stratum": it["stratum"], **situation(it)})
    for fam, v in sorted(by.items()):
        if "defect_off" in v:
            p = matched_clean_control(Path(v["case"]["dir"]), Path(v["defect_off"]["dir"]))
            pairs.append({"family": fam, "stratum": v["case"]["stratum"], "status": p["status"], "checks": p["checks"], "differing": p["differing_features"]})
    tally = defaultdict(Counter)
    for r in rows:
        tally[r["stratum"]][r["status"]] += 1
    ptally = defaultdict(Counter)
    for p in pairs:
        ptally[p["stratum"]][p["status"]] += 1
    look = Counter()
    for r in rows:
        for k, n in r["lookalikes"].items():
            look[k] += bool(n)
    return {"situations": {s: dict(c) for s, c in sorted(tally.items())}, "clean_control_p9": {s: dict(c) for s, c in sorted(ptally.items())},
            "companies_with_lookalike": dict(look), "companies": len(rows), "rows": rows, "pairs": pairs}
