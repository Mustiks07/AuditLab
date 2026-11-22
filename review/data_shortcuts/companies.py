"""Company-level counts (documents, postings, treasury journals) and paired error/clean differences.

An agent sees one company. A count is a shortcut when, chosen on exploration families, a threshold on
it separates companies with a violation from clean companies on the other families. Separately, for
set B, every error company is compared with its zero-issue company of the same seed and config: a
count that differs in the same direction in most pairs is a variant signal of the generator.
"""
from __future__ import annotations

import csv, io, json
from collections import Counter
from pathlib import Path

from review.data_shortcuts.heuristics import company_level, evaluate, select

FEATURES = ["ledger_rows", "ledger_rows_round_100", "document_count", "invoices", "payments", "journals", "treasury_journals",
            "treasury_share", "fx_rows", "max_entity_docs", "mean_journal_size", "entities"]


def company(episode: Path) -> dict:
    pub = episode / "public"
    m = json.loads((pub / "manifest.json").read_text(encoding="utf-8"))
    ledger = list(csv.DictReader(io.StringIO((pub / "ledger.csv").read_text(encoding="utf-8"))))
    count = lambda n: sum(1 for l in (pub / n).read_text(encoding="utf-8").splitlines() if l.strip())
    journals = Counter(r["journal_id"] for r in ledger)
    treasury = {r["journal_id"] for r in ledger if r["account"] == "CASH_RESERVE"}
    inv = [json.loads(l) for l in (pub / "invoices.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    truth = json.loads((episode / "private" / "truth.json").read_text(encoding="utf-8"))
    return {"ledger_rows": len(ledger), "ledger_rows_round_100": int(len(ledger) % 100 == 0), "document_count": m["document_count"],
            "invoices": len(inv), "payments": count("payments.jsonl"), "journals": len(journals), "treasury_journals": len(treasury),
            "treasury_share": len(treasury) / max(1, len(journals)), "fx_rows": max(0, count("fx.csv") - 1),
            "max_entity_docs": max(Counter(d["entity_id"] for d in inv).values(), default=0), "mean_journal_size": len(ledger) / max(1, len(journals)),
            "entities": len({r["entity_id"] for r in ledger if r["entity_id"] not in ("", "null")}),
            "company_has_issue": int(bool(truth["issues"])), "affected": int(bool(truth["issues"]))}


def analyse(items: list[dict], part_of, resamples: int = 10000) -> dict:
    rows = []
    for it in items:
        if it.get("status") == "rejected":
            continue
        rows.append({**company(Path(it["dir"])), "episode": it["dir"], "family": it["family"], "part": part_of(it),
                     "stratum": it["stratum"], "variant": it["variant"]})
    ex = [r for r in rows if r["part"] == "explore"]; ho = [r for r in rows if r["part"] == "evaluate"]
    out = []
    for f in FEATURES:
        rule = select(ex, f, "affected")
        if rule is None:
            continue
        e = evaluate(ho, f, rule, "affected", resamples)
        out.append({"feature": f, "rule": list(rule), "explore": evaluate(ex, f, rule, "affected", 0), "evaluate": e})
    return {"companies": {"explore": len(ex), "evaluate": len(ho), "with_issue_evaluate": sum(r["affected"] for r in ho)}, "rules": out, "rows": rows}


def paired(rows: list[dict]) -> dict:
    """Set B: error company vs its zero-issue company (same seed and config). Count of pairs per difference sign."""
    by = {}
    for r in rows:
        by.setdefault(r["family"], {})[r["variant"]] = r
    pairs = [(v["case"], v["defect_off"]) for v in by.values() if "case" in v and "defect_off" in v]
    out = {}
    for f in FEATURES:
        signs = Counter((a[f] > b[f]) - (a[f] < b[f]) for a, b in pairs)
        strata = Counter(a["stratum"] for a, b in pairs if a[f] != b[f])
        out[f] = {"pairs": len(pairs), "error_greater": signs[1], "equal": signs[0], "error_smaller": signs[-1], "differing_by_stratum": dict(strata)}
    return out
