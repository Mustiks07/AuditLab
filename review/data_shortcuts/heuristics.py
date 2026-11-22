"""One-feature rules chosen on the exploration families and evaluated once on the other families.

Rule forms: x >= t, x <= t, x == v. Selection: best F1 on exploration (ties: higher precision, then
fewer flags). Evaluation reports denominators, base rate, precision, recall, lift, flags on clean
companies and a family-cluster bootstrap interval (protocol data-shortcuts-3.0, sections 3 and 5).
"""
from __future__ import annotations

import random
from collections import defaultdict

from review.data_shortcuts.features import ARTIFICIAL, CATEGORICAL, CONTEXT, FINANCIAL

CLASS = {**{f: "artificial" for f in ARTIFICIAL}, **{f: "context" for f in CONTEXT}, **{f: "financial" for f in FINANCIAL}}


def _f1(tp, flagged, pos):
    return 2 * tp / (flagged + pos) if flagged + pos else 0.0


def select(rows: list[dict], feature: str, target: str):
    """Best rule for one feature on exploration rows; None when there are no positives."""
    pos = sum(r[target] for r in rows)
    if not pos:
        return None
    best = None
    values = [r[feature] for r in rows]
    distinct = set(values)
    if feature in CATEGORICAL or len(distinct) <= 12:
        by = defaultdict(lambda: [0, 0])
        for r in rows:
            by[r[feature]][0] += 1; by[r[feature]][1] += r[target]
        for v, (n, tp) in by.items():
            cand = (_f1(tp, n, pos), tp / n, -n, ("==", v))
            best = max(best, cand) if best else cand
    if feature not in CATEGORICAL:
        for op, reverse in ((">=", True), ("<=", False)):
            order = sorted(rows, key=lambda r: r[feature], reverse=reverse)
            tp = n = 0
            for k, r in enumerate(order):
                n += 1; tp += r[target]
                if k + 1 < len(order) and order[k + 1][feature] == r[feature]:
                    continue
                cand = (_f1(tp, n, pos), tp / n, -n, (op, r[feature]))
                best = max(best, cand) if best else cand
    return best[3] if best else None


def flag(r: dict, feature: str, rule) -> bool:
    op, v = rule
    x = r[feature]
    return x == v if op == "==" else (x >= v if op == ">=" else x <= v)


def _bootstrap(fam_counts: dict, resamples: int, seed: int):
    fams = sorted(fam_counts)
    if len(fams) < 2:
        return None, None
    rng = random.Random(seed)
    precs, recs = [], []
    for _ in range(resamples):
        tp = fl = pos = 0
        for f in (fams[rng.randrange(len(fams))] for _ in fams):
            c = fam_counts[f]; tp += c[0]; fl += c[1]; pos += c[2]
        if fl: precs.append(tp / fl)
        if pos: recs.append(tp / pos)
    q = lambda xs: [sorted(xs)[int(0.025 * len(xs))], sorted(xs)[int(0.975 * len(xs)) - 1]] if xs else None
    return q(precs), q(recs)


def evaluate(rows: list[dict], feature: str, rule, target: str, resamples: int = 10000, seed: int = 20261122) -> dict:
    fam = defaultdict(lambda: [0, 0, 0])
    tp = fl = pos = 0
    clean_flags, clean_companies, accused = 0, set(), set()
    for r in rows:
        f = flag(r, feature, rule); y = r[target]
        tp += f and y; fl += f; pos += y
        c = fam[r["family"]]; c[0] += f and y; c[1] += f; c[2] += y
        if not r["company_has_issue"]:
            clean_companies.add(r["episode"])
            if f:
                clean_flags += 1; accused.add(r["episode"])
    n = len(rows)
    base = pos / n if n else 0.0
    prec = tp / fl if fl else None
    out = {"records": n, "positives": pos, "base_rate": base, "flagged": fl, "tp": tp, "precision": prec,
           "recall": tp / pos if pos else None, "lift": (prec / base) if prec is not None and base else None,
           "flags_in_clean_companies": clean_flags, "clean_companies": len(clean_companies), "clean_companies_accused": len(accused),
           "families": len(fam)}
    if fl and resamples:
        out["precision_ci95"], out["recall_ci95"] = _bootstrap(fam, resamples, seed)
    return out


def company_level(rows: list[dict], feature: str, rule) -> dict:
    """'The company has a violation' when any of its records is flagged."""
    comp = {}
    for r in rows:
        c = comp.setdefault(r["episode"], [r["company_has_issue"], False])
        c[1] = c[1] or flag(r, feature, rule)
    tp = sum(1 for y, f in comp.values() if y and f); fl = sum(1 for _, f in comp.values() if f)
    pos = sum(1 for y, _ in comp.values() if y)
    clean = sum(1 for y, _ in comp.values() if not y)
    return {"companies": len(comp), "with_issue": pos, "flagged": fl, "precision": tp / fl if fl else None, "recall": tp / pos if pos else None,
            "clean_companies": clean, "clean_flagged": fl - tp}


def classify(ev: dict, explore: dict, cls: str, financial: dict | None) -> str:
    """Decision rules of section 5 (the fresh-seed reproduction is checked separately)."""
    if cls == "financial":
        return "financial_rule"
    p, lift, rec = ev["precision"], ev["lift"], ev["recall"]
    lo = (ev.get("precision_ci95") or [0, 0])[0]
    strong = p is not None and p >= 0.5 and (lift or 0) >= 10 and (rec or 0) >= 0.2
    if strong and ev["positives"] >= 20 and lo > 5 * ev["base_rate"]:
        if cls == "context" and financial and (financial.get("precision") or 0) >= p and ev["flags_in_clean_companies"] > 0:
            return "nothing_found"  # legitimate attention signal, not better than the financial rule
        return "confirmed"
    ex_strong = explore["precision"] is not None and explore["precision"] >= 0.5 and (explore["lift"] or 0) >= 10 and (explore["recall"] or 0) >= 0.2
    if strong or ex_strong or (p is not None and 3 <= (lift or 0) < 10 and (rec or 0) >= 0.2):
        return "hypothesis"
    return "nothing_found"
