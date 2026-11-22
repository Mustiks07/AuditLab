"""The three sets of protocol data-shortcuts-3.0, built by the generator of a given checkout.

Generation runs in a separate process whose import path is the target checkout only, through that
checkout's `judge_adapter.generate_episode`. The reviewer's analysis code never imports the target's
generator, so the same analysis can be repeated on any branch (e.g. Claude Pro 1's).
"""
from __future__ import annotations

import json, random, subprocess, sys
from pathlib import Path

Z = {"duplicate_invoice": 0, "payment_allocation": 0, "fx_conversion": 0, "period_cutoff": 0}
SIZES = [(200, 40), (400, 65), (800, 110)]
ALL5 = ["split_payment", "equal_amount_different_supplier", "refund", "archive_copy", "next_day_settlement"]
B_START, C_START = 9330001, 9340001

_GEN = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import judge_adapter
plan = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
done = []
for item in plan:
    out = Path(item["dir"])
    if (out / "public" / "manifest.json").is_file():
        done.append({"dir": item["dir"], "status": "exists"}); continue
    try:
        judge_adapter.generate_episode(item["seed"], item["config"], str(out))
        done.append({"dir": item["dir"], "status": "generated"})
    except Exception as error:
        done.append({"dir": item["dir"], "status": "rejected", "reason": f"{type(error).__name__}: {error}"})
Path(sys.argv[3]).write_text(json.dumps(done), encoding="utf-8")
'''


def generate(checkout: Path, plan: list[dict], work: Path) -> list[dict]:
    work.mkdir(parents=True, exist_ok=True)
    (work / "plan.json").write_text(json.dumps(plan, indent=1), encoding="utf-8")
    script = work / "generate.py"
    script.write_text(_GEN, encoding="utf-8")
    env = {**__import__("os").environ, "PYTHONPATH": str(checkout)}
    r = subprocess.run([sys.executable, str(script), str(checkout), str(work / "plan.json"), str(work / "generated.json")],
                       cwd=checkout, capture_output=True, text=True, env=env, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError("generation failed: " + r.stderr[-800:])
    status = {d["dir"]: d for d in json.loads((work / "generated.json").read_text(encoding="utf-8"))}
    return [{**p, **status[p["dir"]]} for p in plan]


# ------------------------------------------------------------------ B: profile 1.2
def _b_config(stratum: str, k: int, rng: random.Random) -> tuple[dict, dict | None]:
    """(error config, matched zero-issue config). Parameters are drawn from the family's own RNG."""
    rows, docs = SIZES[k % len(SIZES)]
    base = {"config_version": "1.2", "company_size": {"ledger_rows_target": rows, "documents_target": docs},
            "base_currency": "KZT", "currencies": ["KZT", "USD"], "timezone": "Asia/Almaty", "target_period": "2026-11",
            "clean_distractors": list(ALL5) if k % 2 else ["split_payment", "equal_amount_different_supplier", "refund", "archive_copy"]}
    amount = rng.randrange(100_000, 9_000_000)
    alloc = {"kind": "allocation", "amount_minor": amount, "applied_bps": rng.randrange(1_000, 9_001), "tranches": rng.randint(1, 3)}
    cut = {"kind": "invoice_cutoff", "amount_minor": amount, "issued_before_minutes": rng.randint(1, 1_440),
           "recognition_offset_minutes": rng.randint(1, 1_440)}
    c = dict(base)
    if stratum == "alloc_partial":
        c.update(financial_case=alloc, issue_counts={"payment_allocation": 1})
    elif stratum == "alloc_late_tranche":
        c.update(financial_case={**alloc, "tranches": rng.randint(2, 3), "settlement_offset_minutes": rng.choice([0, rng.randint(1, 1_440)])},
                 issue_counts={"payment_allocation": 1})
    elif stratum == "alloc_omitted":
        c.update(financial_case=alloc, allocation_mode="omitted", issue_counts={"payment_allocation": 1})
    elif stratum == "jpy_alloc_partial":
        c.update(base_currency="JPY", currencies=["JPY", "USD"], financial_case={**alloc, "amount_minor": rng.randrange(10_000, 900_000)},
                 issue_counts={"payment_allocation": 1})
    elif stratum == "kwd_alloc_omitted":
        c.update(base_currency="KWD", currencies=["KWD", "USD"], financial_case=alloc, allocation_mode="omitted", issue_counts={"payment_allocation": 1})
    elif stratum == "cutoff_late_recognition":
        c.update(financial_case=cut, issue_counts={"period_cutoff": 1})
    elif stratum == "cutoff_issued_next_period":
        c.update(financial_case={**cut, "issued_next_period": True}, issue_counts={"period_cutoff": 1})
    elif stratum == "duplicate":
        c.update(issue_counts={"duplicate_invoice": 1})
    elif stratum == "fx":
        c.update(issue_counts={"fx_conversion": 1})
    elif stratum == "usd_base_fx":
        c.update(base_currency="USD", currencies=["USD", "EUR"], issue_counts={"fx_conversion": 1})
    elif stratum == "interaction_allocation_fx":
        c.update(interaction="allocation_fx_same_payment", issue_counts={"payment_allocation": 1, "fx_conversion": 1})
    elif stratum == "interaction_cutoff_fx":
        c.update(interaction="cutoff_fx_same_invoice", issue_counts={"period_cutoff": 1, "fx_conversion": 1})
    elif stratum == "two_types_duplicate_allocation":
        c.update(issue_counts={"duplicate_invoice": 1, "payment_allocation": 1})
    elif stratum == "clean_allocation_case":
        c.update(financial_case=alloc, issue_counts={"payment_allocation": 0}); return c, None
    elif stratum == "clean_cutoff_case":
        c.update(financial_case=cut, issue_counts={"period_cutoff": 0}); return c, None
    elif stratum == "clean_company":
        c.update(issue_counts=dict(Z)); return c, None
    off = dict(c)  # the generator accepts an interaction with zero issues: keep it, so the history stays the same
    off["issue_counts"] = {t: 0 for t in c["issue_counts"]}
    if "financial_case" in c:
        off["issue_counts"] = {next(iter(c["issue_counts"])): 0}
    return c, off


B_STRATA = ["alloc_partial", "alloc_late_tranche", "alloc_omitted", "jpy_alloc_partial", "kwd_alloc_omitted", "cutoff_late_recognition",
            "cutoff_issued_next_period", "duplicate", "fx", "usd_base_fx", "interaction_allocation_fx", "interaction_cutoff_fx",
            "two_types_duplicate_allocation", "clean_allocation_case", "clean_cutoff_case", "clean_company"]


def plan_b(work: Path, per_stratum: int = 8, start: int = B_START, prefix: str = "B") -> list[dict]:
    plan, seed, n = [], start, 0
    for stratum in B_STRATA:
        for k in range(per_stratum):
            rng = random.Random(f"data-shortcuts-3.0/B/{seed}")
            err, off = _b_config(stratum, k, rng)
            fam = f"{prefix}{n:03d}"
            plan.append({"set": "B", "family": fam, "stratum": stratum, "seed": seed, "config": err, "variant": "case", "dir": str(work / fam / "case")})
            if off is not None:
                plan.append({"set": "B", "family": fam, "stratum": stratum, "seed": seed, "config": off, "variant": "defect_off", "dir": str(work / fam / "defect_off")})
            seed += 1; n += 1
    return plan


# ------------------------------------------------------------------ C: the adapter with typical configurations
C_TYPES = ["duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff"]


def plan_c(work: Path, per_cell: int = 6) -> list[dict]:
    plan, seed, n = [], C_START, 0
    for version in (None, "1.1"):
        cells = [(t, {"issue_counts": {t: 1}}) for t in C_TYPES] + [("clean", {})]
        for label, cfg in cells:
            for _ in range(per_cell):
                c = dict(cfg)
                if version:
                    c["config_version"] = version
                fam = f"C{n:03d}"
                plan.append({"set": "C", "family": fam, "stratum": f"{version or 'adapter-default'}:{label}", "seed": seed, "config": c,
                             "variant": "case", "dir": str(work / fam / "case")})
                seed += 1; n += 1
    return plan


# ------------------------------------------------------------------ A: published train/dev of the checkout
def plan_a(checkout: Path) -> list[dict]:
    out = []
    for split in ("train", "dev"):
        for d in sorted((checkout / "datasets" / "open" / split).iterdir()):
            if (d / "public" / "manifest.json").is_file():
                out.append({"set": "A", "family": None, "split_name": split, "stratum": split, "dir": str(d), "variant": "case", "status": "published"})
    return out
