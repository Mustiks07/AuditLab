"""Run MR1-MR5 over the protocol strata and check GPT Pro 1's declared curriculum expectations.

    python review/agent_generalization/mr_study.py WORK_DIR [--seeds 3]

Public dev seeds from 9220001 (protocol independent-assurance-2.0); nothing here is sealed.
"""
from __future__ import annotations

import argparse, json, shutil, sys, traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import judge_adapter  # noqa: E402
from auditlab.generator import generate_episode  # noqa: E402
from review.agent_generalization import metamorphic as mr  # noqa: E402
from review.agent_generalization.solver import NotSupported  # noqa: E402

Z = {"duplicate_invoice": 0, "payment_allocation": 0, "fx_conversion": 0, "period_cutoff": 0}
SIZES = [(200, 40), (400, 65), (800, 110)]


def cfg(k, base="KZT", currencies=("KZT", "USD"), counts=None, distractors=("split_payment", "refund"), **extra):
    rows, docs = SIZES[k % len(SIZES)]
    c = {"config_version": "1.1", "company_size": {"ledger_rows_target": rows, "documents_target": docs}, "base_currency": base,
         "currencies": list(currencies), "timezone": "Asia/Almaty", "target_period": "2026-11", "interaction": None,
         "clean_distractors": list(distractors), "issue_counts": {**Z, **(counts or {})}}
    c.update(extra)
    return c


STRATA = {
    "multi_tranche_split": lambda k: cfg(k, counts={"payment_allocation": 1}, distractors=("split_payment", "next_day_settlement")),
    "partial_share": lambda k: cfg(k, counts={"payment_allocation": 1}),
    "full_omission": lambda k: cfg(k, counts={"payment_allocation": 1}, allocation_mode="omitted"),
    "refund_lookalike": lambda k: cfg(k, counts={"payment_allocation": 1}, distractors=("refund",)),
    "boundary_at": lambda k: cfg(k, counts={"payment_allocation": 1}, settlement_timing="at"),
    "boundary_after": lambda k: cfg(k, counts={"payment_allocation": 1}, settlement_timing="after"),
    "period_cutoff": lambda k: cfg(k, counts={"period_cutoff": 1}),
    "jpy_full_omission": lambda k: cfg(k, base="JPY", currencies=("JPY", "USD"), counts={"payment_allocation": 1}, allocation_mode="omitted"),
    "kwd_fx": lambda k: cfg(k, base="KWD", currencies=("KWD", "USD"), counts={"fx_conversion": 1}),
    "usd_base_fx": lambda k: cfg(k, base="USD", currencies=("USD", "EUR"), counts={"fx_conversion": 1}),
    "interaction_allocation_fx": lambda k: cfg(k, counts={"payment_allocation": 1, "fx_conversion": 1}, interaction="allocation_fx_same_payment"),
    "interaction_cutoff_fx": lambda k: cfg(k, counts={"period_cutoff": 1, "fx_conversion": 1}, interaction="cutoff_fx_same_invoice"),
    "two_types_duplicate_allocation": lambda k: cfg(k, counts={"duplicate_invoice": 1, "payment_allocation": 1}),
    "two_types_fx_allocation": lambda k: cfg(k, counts={"fx_conversion": 1, "payment_allocation": 1}),
    "lookalike_duplicate": lambda k: cfg(k, counts={"duplicate_invoice": 1}, distractors=("archive_copy", "equal_amount_different_supplier")),
    "clean_lookalikes": lambda k: cfg(k, distractors=("split_payment", "equal_amount_different_supplier", "refund", "archive_copy", "next_day_settlement")),
}


def check(adapter, name, ep, expected, truth_issues, stale=None, meaning_changed=False):
    """One relation instance: solver vs prediction, scorer on predicted and stale answers."""
    try:
        got = mr.solver_answer(adapter, ep)
    except NotSupported as why:
        return {"relation": name, "status": "not_checked", "reason": str(why)}
    want = sorted(map(mr.sig, expected)); have = sorted(map(mr.sig, got["findings"]))
    sc_pred = mr.official(adapter, ep, expected)
    row = {"relation": name, "solver_matches_prediction": have == want,
           "scorer_accepts_prediction": sc_pred.get("fp") == 0 and sc_pred.get("fn") == 0,
           "predicted": len(expected), "solver": len(got["findings"]), "truth_issues": len(truth_issues)}
    if stale is not None:
        sc_stale = mr.official(adapter, ep, stale)
        row["stale_score"] = [sc_stale.get("tp"), sc_stale.get("fp"), sc_stale.get("fn")]
        row["scorer_rejects_stale"] = (sc_stale.get("fp") or 0) + (sc_stale.get("fn") or 0) > 0 if meaning_changed else None
    ok = row["solver_matches_prediction"] and row["scorer_accepts_prediction"] and row.get("scorer_rejects_stale") is not False
    row["status"] = "passed" if ok else "failed"
    if not ok:
        row["only_solver"] = [list(map(str, s)) for s in sorted(set(have) - set(want))][:3]
        row["only_predicted"] = [list(map(str, s)) for s in sorted(set(want) - set(have))][:3]
    return row


def relations(adapter, ep: Path, seed: int, work: Path) -> list[dict]:
    out = []
    truth = json.loads((ep / "private" / "truth.json").read_text(encoding="utf-8"))
    prov = json.loads((ep / "private" / "provenance.json").read_text(encoding="utf-8"))
    book = mr.load(ep / "public")
    try:
        a0 = mr.solver_answer(adapter, ep)["findings"]
    except NotSupported as why:
        return [{"relation": "baseline", "status": "not_checked", "reason": str(why)}]
    T = lambda issues: {"episode_id": truth["episode_id"], "issues": issues}
    same = lambda f, i: f["type"] == i["type"] and sorted(f["affected_ids"]) == sorted(i["affected_ids"])
    # MR1 own rename + reorder
    b1, t1, ref = mr.rename(book, truth, seed)
    e1 = mr.write_episode(b1, t1, work / "mr1")
    pred = [{**f, "affected_ids": sorted(map(ref, f["affected_ids"])), "evidence_ids": sorted(map(ref, f["evidence_ids"]))} for f in a0]
    out.append(check(adapter, "MR1_rename_reorder", e1, pred, t1["issues"], stale=a0, meaning_changed=bool(a0)))
    # MR1b GPT Pro 1 renamed_snapshot (curriculum id-order transform)
    try:
        from auditlab.research.curriculum.families import renamed_snapshot
        target = work / "mr1b"
        if target.exists(): shutil.rmtree(target)
        mapping = renamed_snapshot(ep, target, seed + 7)
        refs = mapping["references"]
        def ref2(k):
            kind, _, rest = k.partition("/")
            field = {"payment": "payment_id", "invoice": "invoice_id"}.get(kind)
            return f"{kind}/{mapping['raw_ids'][field][rest]}" if field else refs.get(k, k)
        pred2 = [{**f, "affected_ids": sorted(map(ref2, f["affected_ids"])), "evidence_ids": sorted(map(ref2, f["evidence_ids"]))} for f in a0]
        t2 = json.loads((target / "private" / "truth.json").read_text(encoding="utf-8"))
        out.append(check(adapter, "MR1b_gpt1_renamed_snapshot", target, pred2, t2["issues"], stale=a0, meaning_changed=bool(a0)))
    except Exception as error:
        out.append({"relation": "MR1b_gpt1_renamed_snapshot", "status": "not_checked", "reason": f"{type(error).__name__}: {error}"})
    # MR2 lawful look-alikes
    for name, fn in (("MR2_archive_copy", mr.archive_copy), ("MR2_same_amount_other_customer", mr.other_supplier_same_amount)):
        b2 = fn(book, {"findings": a0}, seed)
        if b2 is None:
            out.append({"relation": name, "status": "not_checked", "reason": "no plain in-period base-currency invoice to imitate"}); continue
        out.append(check(adapter, name, mr.write_episode(b2, truth, work / name), a0, truth["issues"]))
    # MR3 revert each declared mutation; MR5 when two or more
    for k, m in enumerate(prov["mutations"]):
        b3 = mr.revert(book, ep, k)
        name = "MR3_revert_" + m["type"]
        if b3 is None:
            out.append({"relation": name, "status": "not_checked", "reason": "transition changes cannot be partitioned by mutation"}); continue
        keep = [f for f in a0 if not same(f, m)]
        issues = [i for i in truth["issues"] if not same(i, m)]
        e3 = mr.write_episode(b3, T(issues), work / f"mr3-{k}")
        out.append(check(adapter, name, e3, keep, issues, stale=a0, meaning_changed=True))
        if len(prov["mutations"]) > 1:
            r = check(adapter, "MR5_fix_one_keep_other", e3, keep, issues)
            out.append(r)
    # MR3 own fix and MR4 own amount change: allocation
    for f in a0:
        if f["type"] == "payment_allocation":
            pid = next(a.split("/", 1)[1] for a in f["affected_ids"] if a.startswith("payment/"))
            b = mr.fix_allocation(book, pid, f["claimed_amount_minor"], seed)
            issues = [i for i in truth["issues"] if not same(i, f)]
            out.append(check(adapter, "MR3_own_allocation_fix", mr.write_episode(b, T(issues), work / "mr3own"),
                             [g for g in a0 if g is not f], issues, stale=a0, meaning_changed=True))
            delta = max(1, f["claimed_amount_minor"] // 7)
            b = mr.change_allocation(book, pid, delta)
            if b is None:
                out.append({"relation": "MR4_allocation_amount", "status": "not_checked", "reason": "no application or receipt shape to change"}); continue
            issues = [({**i, "expected_amount_minor": i["expected_amount_minor"] + delta} if same(i, f) else i) for i in truth["issues"]]
            pred = [({**g, "claimed_amount_minor": g["claimed_amount_minor"] + delta} if g is f else g) for g in a0]
            out.append(check(adapter, "MR4_allocation_amount", mr.write_episode(b, T(issues), work / "mr4a"), pred, issues, stale=a0, meaning_changed=True))
        if f["type"] == "fx_conversion":
            kind, _, aid = f["affected_ids"][0].partition("/")
            m = next((m for m in prov["mutations"] if m["type"] == "fx_conversion"), None)
            if not m or "amount_minor" not in m.get("after", {}):
                out.append({"relation": "MR4_fx_amount", "status": "not_checked", "reason": "provenance does not declare recorded/correct amounts"}); continue
            delta = 100
            new_claim = abs(m["after"]["amount_minor"] + delta - m["before"]["amount_minor"])
            b = mr.change_fx(book, kind, aid, delta)
            if b is None:
                out.append({"relation": "MR4_fx_amount", "status": "not_checked", "reason": "no single FX valuation journal"}); continue
            issues = [({**i, "expected_amount_minor": new_claim} if same(i, f) else i) for i in truth["issues"]]
            pred = [({**g, "claimed_amount_minor": new_claim} if g is f else g) for g in a0]
            out.append(check(adapter, "MR4_fx_amount", mr.write_episode(b, T(issues), work / "mr4f"), pred, issues, stale=a0, meaning_changed=True))
    return out


def gpt1_boundary(adapter, work: Path) -> list[dict]:
    """GPT Pro 1's declared expectations for boundary_variants (curriculum #49), checked with the solver."""
    from auditlab.research.curriculum.families import boundary_variants
    rows = []
    for seed in (9229001, 9229002):
        config = {"config_version": "1.1", "base_currency": "KZT", "currencies": ["KZT"], "settlement_timing": "after",
                  "issue_counts": {"payment_allocation": 1}, "clean_distractors": ["next_day_settlement"], "interaction": None}
        parent = work / f"gpt1-parent-{seed}"
        for p in (parent, work / f"gpt1-variants-{seed}"):
            if p.exists(): shutil.rmtree(p)
        generate_episode(seed, config, parent)
        variants = boundary_variants(parent, seed, config, work / f"gpt1-variants-{seed}")
        base = mr.solver_answer(adapter, parent)["findings"]
        for v in variants:
            try:
                got = mr.solver_answer(adapter, v["snapshot"])["findings"]
            except NotSupported as why:
                rows.append({"seed": seed, "kind": v["kind"], "status": "not_checked", "reason": str(why)}); continue
            mine = sorted((f["type"], tuple(sorted(f["affected_ids"])), f["claimed_amount_minor"], f["currency"]) for f in got)
            theirs = sorted((e["type"], tuple(sorted(e["affected_ids"])), e["claimed_amount_minor"], e["currency"]) for e in v["expected"])
            rows.append({"seed": seed, "kind": v["kind"], "declared": v["reason"], "status": "passed" if mine == theirs else "failed",
                         "solver": [list(map(str, x)) for x in mine], "declared_expected": [list(map(str, x)) for x in theirs],
                         "parent_answer_amount": [f["claimed_amount_minor"] for f in base]})
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("work", type=Path); ap.add_argument("--seeds", type=int, default=3)
    a = ap.parse_args(argv)
    a.work.mkdir(parents=True, exist_ok=True)
    report = {"protocol_id": "independent-assurance-2.0", "strata": {}, "gpt1_boundary_variants": None}
    seed = 9220001
    for stratum, make in STRATA.items():
        report["strata"][stratum] = []
        for k in range(a.seeds):
            ep = a.work / "episodes" / f"{stratum}-{k}"
            if ep.exists(): shutil.rmtree(ep)
            try:
                generate_episode(seed, make(k), ep)
            except ValueError as error:
                report["strata"][stratum].append({"seed": seed, "status": "config_rejected", "reason": str(error)}); seed += 1; continue
            try:
                rel = relations(judge_adapter, ep, seed, a.work / "transformed" / f"{stratum}-{k}")
            except Exception as error:  # a crash is a result, not a skip
                rel = [{"relation": "crash", "status": "failed", "reason": f"{type(error).__name__}: {error}", "trace": traceback.format_exc()[-800:]}]
            report["strata"][stratum].append({"seed": seed, "relations": rel})
            seed += 1
    report["gpt1_boundary_variants"] = gpt1_boundary(judge_adapter, a.work / "gpt1")
    tally = {}
    for runs in report["strata"].values():
        for run in runs:
            for r in run.get("relations", []):
                t = tally.setdefault(r["relation"], {"passed": 0, "failed": 0, "not_checked": 0}); t[r["status"]] += 1
    report["tally"] = tally
    (a.work / "metamorphic-report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps(tally, indent=1)); print(json.dumps([{k: r[k] for k in ("seed", "kind", "status")} for r in report["gpt1_boundary_variants"]]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
