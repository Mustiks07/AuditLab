"""Independent agent-generalization checks (Claude Pro 2, #43). Not an official score.

    python review/agent_generalization/run.py dev --work DIR            # 30 public dev families, validation, separation, shortcuts, baselines
    python review/agent_generalization/run.py holdout --work SEALED_DIR  # sealed holdout (trusted side only); prints commitment
    python review/agent_generalization/run.py compare EXPERIMENT_DIR     # re-score saved attempts, family-cluster statistics
    python review/agent_generalization/run.py validate EPISODE_DIR       # validate_candidate for one snapshot
    python review/agent_generalization/run.py sealed-v2 --v1 SEALED_V1_DIR --work SEALED_V2_DIR --public-dev DIR...
        # trusted side only: keep the 78 v1 families (checked to regenerate byte for byte), fill the 12 full-omission
        # slots with fresh seeds, check separation and origin partition against all public dev material, print the commitment
"""
from __future__ import annotations

import argparse, hashlib, json, subprocess, sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _others(work: Path):
    """Public directories of the other splits for the separation check: published train/dev and the internal selection."""
    from auditlab.generator import generate_episode
    from auditlab.release.rehearsal import selection_manifest
    internal = work / "internal"
    if not internal.exists():
        for row in selection_manifest()["episodes"]:
            generate_episode(row["seed"], row["config"], internal / row["id"])
    return {"train": sorted((ROOT / "datasets/open/train").glob("*/public")), "dev_published": sorted((ROOT / "datasets/open/dev").glob("*/public")),
            "internal": sorted(internal.glob("*/public"))}


def _summary(built: dict) -> dict:
    fams = [f for f in built["families"] if "variants" in f]
    return {"families_built": len(fams), "pending": Counter(f["stratum"] for f in built["families"] if f.get("status") == "not_checked"),
            "rejected": [{k: f.get(k) for k in ("family_id", "stratum", "reason")} for f in built["families"] if f.get("status") == "rejected"],
            "clean_kind": Counter(f["clean_kind"]["kind"] for f in fams),
            "strata": Counter(f["stratum"] for f in fams),
            "validation": {v: Counter(f["variants"][v]["validation"] for f in fams) for v in ("error", "clean")},
            "failed_checks": Counter(c for f in fams for v in ("error", "clean") for c in f["variants"][v]["failed_checks"]),
            "currencies": Counter(f["config"]["base_currency"] for f in fams), "zones": Counter(f["config"]["timezone"] for f in fams),
            "sizes": Counter(f["config"]["company_size"]["ledger_rows_target"] for f in fams),
            "truth_issues_error_variants": sum(f["variants"]["error"]["truth_issues"] for f in fams),
            "replaced_before_any_agent_run": len(built["replaced_before_any_agent_run"])}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("dev", "holdout"):
        p = sub.add_parser(name); p.add_argument("--work", type=Path, required=True); p.add_argument("--skip-baselines", action="store_true")
    sub.add_parser("compare").add_argument("experiment", type=Path)
    sub.add_parser("validate").add_argument("episode", type=Path)
    p = sub.add_parser("sealed-v2"); p.add_argument("--v1", type=Path, required=True); p.add_argument("--work", type=Path, required=True)
    p.add_argument("--public-dev", type=Path, nargs="*", default=[])
    p = sub.add_parser("sealed-passport"); p.add_argument("--sealed", type=Path, required=True); p.add_argument("--work", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.cmd == "sealed-passport":
        return sealed_passport(args)
    if args.cmd == "sealed-v2":
        return sealed_v2(args)
    import judge_adapter as ja
    from review.agent_generalization import families, compare, validator
    if args.cmd == "validate":
        print(json.dumps(validator.validate_candidate(args.episode, adapter=ja), indent=1)); return 0
    if args.cmd == "compare":
        print(json.dumps(compare.compare_experiment(args.experiment), indent=1, default=str)); return 0
    work = args.work; work.mkdir(parents=True, exist_ok=True)
    seeds = list(range(9300, 9330)) if args.cmd == "dev" else None
    built = families.build(ja, args.cmd, work, seeds)
    sep = families.separation(built["families"], _others(work))
    shortcuts = families.shortcut_audit(built["families"])
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    report = {"schema_version": "1.0", "protocol_id": "agent-generalization-1.1", "source_sha": sha, "split": args.cmd,
              "summary": _summary(built), "separation": sep, "shortcut_audit": shortcuts}
    if args.cmd == "dev":
        (work / "dev-manifest.json").write_text(json.dumps(built, indent=1, default=str), encoding="utf-8")
        if not args.skip_baselines:
            from auditlab.policies import make_policy
            from auditlab.runner import make_env
            from review.agent_generalization import baselines
            exp = baselines.run(make_env, make_policy, built, work / "baselines")
            report["baselines"] = compare.compare_experiment(exp)
        (work / "dev-report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    else:  # sealed: full manifest stays on the trusted side; only the commitment and coverage are published
        sealed = work / "holdout-sealed.json"
        sealed.write_text(json.dumps(built, indent=1, default=str), encoding="utf-8")
        report["commitment"] = {"file": "holdout-sealed.json (trusted side only)", "sha256": hashlib.sha256(sealed.read_bytes()).hexdigest()}
        for f in report["separation"]["defect_chain_reused"]:
            f.pop("chain", None)  # do not publish chain fingerprints of test families
        (work / "holdout-public-summary.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("summary", "separation", "shortcut_audit")} | ({"commitment": report.get("commitment")}), indent=1, default=str)[:6000])
    return 0


def sealed_passport(args) -> int:
    """Passport 2.0 (solver included) and P9 on every sealed variant. Prints counts only (trusted side)."""
    import judge_adapter as ja
    from review.agent_generalization.validator import validate_candidate
    from review.agent_generalization.pairs import matched_clean_control
    from review.agent_generalization.families import acceptable
    sealed = json.loads(args.sealed.read_text(encoding="utf-8"))
    fams = [f for f in sealed["families"] if "variants" in f]
    status, checks, p9, acc, actions = Counter(), Counter(), Counter(), Counter(), []
    for f in fams:
        for v in ("error", "clean"):
            d = Path(f["variants"][v]["dir"])
            r = validate_candidate(d, adapter=ja, work=args.work / f["family_id"] / v)
            status[(v, r["status"])] += 1; acc[(v, acceptable(r, v))] += 1
            for k, c in r["checks"].items():
                checks[(v, k, c["status"])] += 1
            actions.append(r["checks"]["independent_solver"].get("actions") or 0)
        p9[(f["stratum"], matched_clean_control(Path(f["variants"]["error"]["dir"]), Path(f["variants"]["clean"]["dir"]))["status"])] += 1
    out = {"protocol_id": "independent-assurance-2.0", "sealed_sha256": hashlib.sha256(args.sealed.read_bytes()).hexdigest(), "families": len(fams),
           "status": {f"{v}:{s}": n for (v, s), n in sorted(status.items())}, "accepted": {f"{v}:{a}": n for (v, a), n in sorted(acc.items())},
           "checks_not_passed": {f"{v}:{k}:{s}": n for (v, k, s), n in sorted(checks.items()) if s != "passed"},
           "solver_max_actions": max(actions), "p9": {f"{st}:{s}": n for (st, s), n in sorted(p9.items())}}
    (args.work / "sealed-passport-summary.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))
    return 0


def sealed_v2(args) -> int:
    """Sealed holdout v2 (protocol independent-assurance-2.0, section 4). Trusted side only."""
    import os, shutil, tempfile
    import judge_adapter as ja
    from auditlab.generator import generate_episode
    from review.agent_generalization import families, lineage
    v1 = json.loads((args.v1 / "holdout-sealed.json").read_text(encoding="utf-8"))
    work = args.work.resolve(); work.mkdir(parents=True, exist_ok=True)
    # 1. v1 families must regenerate byte for byte (public, truth, source book) under the current generator
    def tree(p):
        return {f.relative_to(p).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(p.rglob("*")) if f.is_file()}
    regen = Counter()
    for f in v1["families"]:
        if f.get("seed") is None:
            continue
        err = Path(f["variants"]["error"]["dir"])
        tmp = Path(tempfile.mkdtemp()) / "e"
        generate_episode(f["seed"], f["config"], tmp)
        a, b = tree(err), tree(tmp)
        diff = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
        regen["identical" if not diff else ("provenance_version_only" if diff == ["private/provenance.json"] else "different")] += 1
        shutil.rmtree(tmp.parent, ignore_errors=True)
    # 2. fill the full-omission slots with fresh seeds (os.urandom), validated with the full passport
    new = families.build(ja, "holdout", work / "v2", None, only={"full_omission_allocation"})
    built = [f for f in v1["families"] if f.get("stratum") != "full_omission_allocation"] + new["families"]
    sealed = {"split": "holdout", "version": "v2", "protocol_id": "independent-assurance-2.0", "v1_commitment": hashlib.sha256((args.v1 / "holdout-sealed.json").read_bytes()).hexdigest(),
              "families": built, "replaced_before_any_agent_run": v1["replaced_before_any_agent_run"] + new["replaced_before_any_agent_run"]}
    # 3. separation from every split and all public dev material; origin partition with lineage
    others = _others(work)
    others["public_dev_material"] = sorted(p for d in args.public_dev for p in d.rglob("public") if (p / "manifest.json").is_file())
    sep = families.separation(built, others)
    shortcuts = families.shortcut_audit(built)
    items = [{"id": f"{f['family_id']}/{v}", "split": "holdout", "episode": Path(f["variants"][v]["dir"]), "parent": None}
             for f in built if "variants" in f for v in ("error", "clean")]
    items += [{"id": f"pub:{k}", "split": "public", "episode": p.parent, "parent": None} for k, p in enumerate(others["public_dev_material"] + others["train"] + others["dev_published"])]
    lin = lineage.check(items)
    # within one family error and clean share the origin by design; only cross-partition repeats are violations
    path = work / "holdout-sealed-v2.json"
    path.write_text(json.dumps(sealed, indent=1, default=str), encoding="utf-8")
    fams = [f for f in built if "variants" in f]
    summary = {"schema_version": "1.0", "protocol_id": "independent-assurance-2.0", "version": "v2",
               "commitment": {"file": "holdout-sealed-v2.json (trusted side only)", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
               "v1_commitment": sealed["v1_commitment"], "v1_regeneration_under_current_generator": dict(regen),
               "families_built": len(fams), "strata": Counter(f["stratum"] for f in fams),
               "pending": Counter(f["stratum"] for f in built if f.get("status") == "not_checked"),
               "rejected": len([f for f in built if f.get("status") == "rejected"]),
               "validation": {v: Counter(f["variants"][v]["validation"] for f in fams) for v in ("error", "clean")},
               "accepted": {v: sum(bool(f["variants"][v].get("accepted", f["variants"][v]["validation"] == "passed")) for f in fams) for v in ("error", "clean")},
               "sizes": Counter(f["config"]["company_size"]["ledger_rows_target"] for f in fams),
               "currencies": Counter(f["config"]["base_currency"] for f in fams),
               "separation": {k: (len(v) if isinstance(v, list) else v) for k, v in sep.items()},
               "shortcut_audit": {k: v["perfectly_separates"] for k, v in shortcuts.items()},
               "origin_partition": {"status": lin["status"], "items": lin["items"], "violations": len(lin["violations"])},
               "public_dev_material_snapshots": len(others["public_dev_material"]),
               "replaced_before_any_agent_run": len(sealed["replaced_before_any_agent_run"])}
    (work / "holdout-public-summary-v2.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    (work / "lineage-detail.json").write_text(json.dumps(lin, indent=1, default=str), encoding="utf-8")
    print(json.dumps(summary, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
