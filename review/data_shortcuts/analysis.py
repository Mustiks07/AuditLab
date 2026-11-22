"""Shortcut analysis of one set: select rules on exploration families, evaluate on the other families."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from review.data_shortcuts.features import ARTIFICIAL, CONTEXT, FINANCIAL, records
from review.data_shortcuts.heuristics import CLASS, classify, company_level, evaluate, select


def load(items: list[dict], part_of) -> list[dict]:
    rows = []
    for it in items:
        if it.get("status") == "rejected":
            continue
        ep = Path(it["dir"])
        for r in records(ep):
            r.update(episode=str(ep), family=it["family"], stratum=it["stratum"], variant=it["variant"], part=part_of(it))
            rows.append(r)
    return rows


def analyse(rows: list[dict], resamples: int = 10000) -> dict:
    out = {"records": Counter((r["part"], r["table"]) for r in rows), "rules": []}
    out["records"] = {f"{p}:{t}": n for (p, t), n in sorted(out["records"].items())}
    for table in ("ledger", "invoices", "payments"):
        for target in ("affected", "evidence"):
            ex = [r for r in rows if r["table"] == table and r["part"] == "explore"]
            ho = [r for r in rows if r["table"] == table and r["part"] == "evaluate"]
            if not ex or not ho:
                continue
            fin_rule = ("==", 1)
            fin = evaluate(ho, "calc_" + ("affected" if target == "affected" else "evidence"), fin_rule, target, resamples)
            for feature in ARTIFICIAL + CONTEXT + FINANCIAL:
                rule = select(ex, feature, target)
                if rule is None:
                    continue
                e_ex = evaluate(ex, feature, rule, target, 0)
                e_ho = evaluate(ho, feature, rule, target, resamples if CLASS[feature] != "financial" else resamples)
                status = classify(e_ho, e_ex, CLASS[feature], fin)
                out["rules"].append({"table": table, "target": target, "feature": feature, "class": CLASS[feature], "rule": list(rule),
                                     "explore": e_ex, "evaluate": e_ho, "company_level_evaluate": company_level(ho, feature, rule),
                                     "status": status})
    return out


def strata_of_hits(rows: list[dict], table: str, feature: str, rule) -> dict:
    from review.data_shortcuts.heuristics import flag
    hit = Counter(); pos = Counter()
    for r in rows:
        if r["table"] != table or r["part"] != "evaluate":
            continue
        if flag(r, feature, rule):
            hit[(r["stratum"], r["variant"], r["affected"])] += 1
    return {f"{s}|{v}|affected={a}": n for (s, v, a), n in sorted(hit.items())}
