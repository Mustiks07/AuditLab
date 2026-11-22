"""Families for dev (public) and holdout (sealed): generation, matched clean variants, validation,
economic separation and a shortcut audit. Everything here runs on the trusted reviewer side."""
from __future__ import annotations

import hashlib, json, os, shutil
from collections import Counter, defaultdict
from pathlib import Path

from .canonical import chains, entity_of, whole_history
from .validator import clean_variant, validate_candidate

Z = {"duplicate_invoice": 0, "payment_allocation": 0, "fx_conversion": 0, "period_cutoff": 0}
ALL = ["split_payment", "equal_amount_different_supplier", "refund", "archive_copy", "next_day_settlement"]
CURRENCIES = [["KZT", "USD"], ["KZT", "JPY", "KWD"], ["JPY", "USD"], ["KWD", "JPY", "USD"], ["USD", "EUR"], ["EUR", "USD", "JPY"]]
ZONES = [("Asia/Almaty", "2026-11"), ("Europe/Berlin", "2026-10"), ("America/New_York", "2026-03"), ("UTC", "2026-12"),
         ("Asia/Kathmandu", "2027-01"), ("Europe/Berlin", "2026-03"), ("America/New_York", "2026-11")]
SIZES = [(200, 40), (400, 65), (800, 100), (1200, 150), (2000, 200)]
STRATA = {  # holdout counts (protocol.json); full omission since generator 1.7 (holdout v2)
    "partial_allocation": 12, "full_omission_allocation": 12, "duplicate_invoice": 12, "fx_conversion": 12, "period_cutoff": 12,
    "interaction_allocation_fx": 10, "interaction_cutoff_fx": 10, "multi_issue": 10, "settlement_at_or_after_boundary": 10}
PENDING = {  # full omission: available since generator 1.7 (#40, PR #48); protocol independent-assurance-2.0
           # protocol 1.1: the boundary pair is one fixed commercial history (the S01/S02 chain, also in published train),
           # so seeds do not create new economic families in this stratum
           "settlement_at_or_after_boundary": "fixed template chain shared with published train; waits for a randomized boundary history"}


def matched_clean(adapter, error_dir: Path, seed: int, config: dict, out: Path) -> dict:
    """Protocol 1.1 clean variant: the source book if it has the error variant's ledger size; otherwise the
    generator's defect-off episode if its commercial chains equal the source book's (treasury filler aside)."""
    def rows(p):
        return json.loads((p / "manifest.json").read_text(encoding="utf-8"))["ledger_rows"]
    sb = error_dir / "private" / "source_book"
    if rows(sb) == rows(error_dir / "public"):
        clean_variant(error_dir, out)
        return {"kind": "source_book"}
    toggled = out.parent / (out.name + "-toggled")
    if toggled.exists(): shutil.rmtree(toggled)
    off = {**config, "issue_counts": dict(Z)}
    off.pop("settlement_timing", None)
    adapter.generate_episode(seed, {"team_config": off}, str(toggled))
    # commercial chains = chains with at least one invoice or payment (treasury CASH/CASH_RESERVE filler excluded)
    commercial = lambda p: sorted(c["digest"] for e, c in chains(p).items() if c["canon"]["invoices"] or c["canon"]["payments"])
    same = commercial(toggled / "public") == commercial(sb) and rows(toggled / "public") == rows(error_dir / "public")
    if same:
        shutil.move(str(toggled), str(out))
        return {"kind": "defect_off_episode", "reason": "source book is smaller than the error variant (row-count shortcut)"}
    return {"kind": None, "reason": "no size-matched clean variant with the same commercial history"}


def stratum_config(stratum: str, k: int) -> dict | None:
    cur = CURRENCIES[k % len(CURRENCIES)]; zone, period = ZONES[(k // 2) % len(ZONES)]; rows, docs = SIZES[k % len(SIZES)]  # fixed 21.11: (k // 3) never reached 2000 rows for k <= 11
    c = {"config_version": "1.1", "company_size": {"ledger_rows_target": rows, "documents_target": docs}, "base_currency": cur[0], "currencies": cur,
         "timezone": zone, "target_period": period, "interaction": None, "clean_distractors": list(ALL), "issue_counts": dict(Z)}
    if stratum in PENDING:
        return None
    if stratum == "partial_allocation": c["issue_counts"]["payment_allocation"] = 1
    elif stratum == "full_omission_allocation":
        c["issue_counts"]["payment_allocation"] = 1; c["allocation_mode"] = "omitted"
    elif stratum in ("duplicate_invoice", "fx_conversion", "period_cutoff"): c["issue_counts"][stratum] = 1
    elif stratum == "interaction_allocation_fx":
        c.update(interaction="allocation_fx_same_payment"); c["issue_counts"].update(payment_allocation=1, fx_conversion=1)
    elif stratum == "interaction_cutoff_fx":
        c.update(interaction="cutoff_fx_same_invoice"); c["issue_counts"].update(period_cutoff=1, fx_conversion=1)
    elif stratum == "multi_issue":
        # protocol 1.1: never duplicate_invoice together with period_cutoff (no size-matched clean variant exists)
        combos = [["payment_allocation", "fx_conversion"], ["duplicate_invoice", "payment_allocation"], ["period_cutoff", "fx_conversion"],
                  ["duplicate_invoice", "fx_conversion", "payment_allocation"], ["period_cutoff", "payment_allocation", "fx_conversion"]]
        for t in combos[k % len(combos)]: c["issue_counts"][t] += 1
    elif stratum == "settlement_at_or_after_boundary":
        c["settlement_timing"] = "at" if k % 2 == 0 else "after"; c["issue_counts"]["payment_allocation"] = 1
    return c


def plan(split: str, seeds: list[int] | None = None, only: set[str] | None = None) -> list[dict]:
    rows, i = [], 0
    scale = 1 if split == "holdout" else 0.3
    for stratum, n in STRATA.items():
        if only and stratum not in only:
            continue
        for k in range(round(n * scale)):
            cfg = stratum_config(stratum, k)
            rows.append({"stratum": stratum, "config": cfg, "pending": PENDING.get(stratum) if cfg is None else None})
    for r in rows:
        if r["config"] is not None:
            r["seed"] = seeds[i] if seeds else int.from_bytes(os.urandom(6), "big") + 10 ** 9
            i += 1
    return rows


def _tree(p: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p.rglob("*")):
        if f.is_file():
            h.update(str(f.relative_to(p)).replace("\\", "/").encode()); h.update(f.read_bytes())
    return h.hexdigest()


# a clean variant copied from the source book has no provenance of its own: P4 and P10 are honestly not_checked
DERIVED_CLEAN_NOT_CHECKED = {"mutation_declared", "reproducible_generation"}


def acceptable(validation: dict, variant: str) -> bool:
    if validation["status"] == "passed":
        return True
    open_checks = {k for k, c in validation["checks"].items() if c["status"] != "passed"}
    return variant == "clean" and validation["status"] == "not_checked" and open_checks <= DERIVED_CLEAN_NOT_CHECKED


def build(adapter, split: str, work: Path, seeds: list[int] | None = None, max_replacements: int = 3, only: set[str] | None = None) -> dict:
    """Generate both variants per family, validate, replace (before any agent run) when validation fails."""
    families, rejected = [], []
    for entry in plan(split, seeds, only):
        if entry["config"] is None:
            families.append({"stratum": entry["stratum"], "status": "not_checked", "reason": entry["pending"]})
            continue
        seed, attempt = entry["seed"], 0
        while True:
            fid = f"{split}-{hashlib.sha256(str(seed).encode()).hexdigest()[:10]}"
            d = work / split / fid
            if d.exists(): shutil.rmtree(d)
            adapter.generate_episode(seed, {"team_config": entry["config"]}, str(d / "error"))
            clean_kind = matched_clean(adapter, d / "error", seed, entry["config"], d / "clean")
            if clean_kind["kind"] is None:
                vals = {"error": validate_candidate(d / "error", adapter=adapter, work=d / "error.validation"),
                        "clean": {"status": "failed", "checks": {"matched_clean": {"status": "failed"}}, "truth_issues": 0}}
            else:
                vals = {v: validate_candidate(d / v, adapter=adapter, work=d / f"{v}.validation") for v in ("error", "clean")}
            if all(acceptable(x, v) for v, x in vals.items()) or attempt >= max_replacements or seeds:
                break
            rejected.append({"family_id": fid, "seed": seed, "stratum": entry["stratum"], "clean_kind": clean_kind,
                             "failed": {v: [k for k, c in x["checks"].items() if c["status"] == "failed"] for v, x in vals.items()}})
            seed, attempt = int.from_bytes(os.urandom(6), "big") + 10 ** 9, attempt + 1
        if clean_kind["kind"] is None:
            families.append({"family_id": fid, "stratum": entry["stratum"], "seed": seed, "config": entry["config"], "status": "rejected",
                             "reason": clean_kind["reason"]})
            continue
        families.append({"family_id": fid, "stratum": entry["stratum"], "seed": seed, "config": entry["config"], "clean_kind": clean_kind,
                         "variants": {v: {"dir": str(d / v), "sha256": _tree(d / v), "validation": vals[v]["status"],
                                          "failed_checks": [k for k, c in vals[v]["checks"].items() if c["status"] == "failed"],
                                          "accepted": acceptable(vals[v], v),
                                          "truth_issues": vals[v]["truth_issues"]} for v in ("error", "clean")}})
    return {"split": split, "families": families, "replaced_before_any_agent_run": rejected}


# ------------------------------------------------------------------ separation and shortcuts
def separation(built: list[dict], others: dict[str, list[Path]]) -> dict:
    """Whole histories distinct; the defect-bearing chain (and its clean counterpart) occurs nowhere else."""
    fam = [f for f in built if "variants" in f]
    whole = {f["family_id"]: whole_history(Path(f["variants"]["error"]["dir"]) / "public") for f in fam}
    dup_whole = [k for k, v in Counter(whole.values()).items() if v > 1]
    other_whole, other_chains = {}, defaultdict(set)
    for split, publics in others.items():
        for p in publics:
            other_whole[whole_history(p)] = split
            for c in chains(p).values():
                other_chains[c["digest"]].add(split)
    fam_chains = {}
    for f in fam:
        for v in ("error", "clean"):
            for ent, c in chains(Path(f["variants"][v]["dir"]) / "public").items():
                fam_chains.setdefault(c["digest"], set()).add(f["family_id"])
    problems, template_overlap = [], 0
    for f in fam:
        err = Path(f["variants"]["error"]["dir"])
        truth = json.loads((err / "private" / "truth.json").read_text(encoding="utf-8"))
        ents = {entity_of(err / "public", a) for i in truth["issues"] for a in i["affected_ids"]} - {None}
        for v in ("error", "clean"):
            ch = chains(Path(f["variants"][v]["dir"]) / "public")
            for e in ents:
                if e not in ch: continue
                dg = ch[e]["digest"]
                if fam_chains.get(dg, set()) - {f["family_id"]} or other_chains.get(dg):
                    problems.append({"family_id": f["family_id"], "variant": v, "chain": dg[:16],
                                     "also_in": sorted((fam_chains.get(dg, set()) - {f["family_id"]}) | other_chains.get(dg, set()))})
        all_chains = {c["digest"] for c in chains(err / "public").values()}
        template_overlap += sum(1 for d in all_chains if other_chains.get(d))
    return {"families": len(fam), "whole_history_duplicates": len(dup_whole),
            "whole_history_equal_to_other_split": sorted({other_whole[w] for w in whole.values() if w in other_whole}),
            "defect_chain_reused": problems, "clean_template_chains_shared_with_other_splits": template_overlap}


def shortcut_audit(built: list[dict]) -> dict:
    import csv, io
    feats = defaultdict(lambda: {"clean": [], "error": []})
    for f in (x for x in built if "variants" in x):
        for v in ("clean", "error"):
            pub = Path(f["variants"][v]["dir"]) / "public"
            ledger = list(csv.DictReader(io.StringIO((pub / "ledger.csv").read_text(encoding="utf-8"))))
            invs = [json.loads(l) for l in (pub / "invoices.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
            m = json.loads((pub / "manifest.json").read_text(encoding="utf-8"))
            vals = {"ledger_rows": m["ledger_rows"], "document_count": m["document_count"],
                    "ledger_rows_is_a_size_target": int(m["ledger_rows"] in {s for s, _ in SIZES}),
                    "fx_valuation_rows": sum(r["account"] == "FX_VALUATION" for r in ledger),
                    "archive_groups": sum(1 for c in Counter((d["entity_id"], d["invoice_id"]) for d in invs).values() if c > 1)}
            for k, x in vals.items():
                feats[k][v].append(x)
    out = {}
    for k, d in feats.items():
        c, e = d["clean"], d["error"]
        # a single public feature is a shortcut if a threshold separates clean from error variants,
        # or, for low-cardinality features, if the value sets do not overlap at all
        separates = (max(c) < min(e)) or (max(e) < min(c)) or (len(set(c) | set(e)) <= 6 and not (set(c) & set(e)))
        out[k] = {"perfectly_separates": separates, "clean_range": [min(c), max(c)], "error_range": [min(e), max(e)],
                  "clean_distinct": len(set(c)), "error_distinct": len(set(e))}
    return out
