"""Independent agent-generalization checks (Claude Pro 2, #43). Not an official score.

    python review/agent_generalization/run.py dev --work DIR            # 30 public dev families, validation, separation, shortcuts, baselines
    python review/agent_generalization/run.py holdout --work SEALED_DIR  # sealed holdout (trusted side only); prints commitment
    python review/agent_generalization/run.py compare EXPERIMENT_DIR     # re-score saved attempts, family-cluster statistics
    python review/agent_generalization/run.py validate EPISODE_DIR       # validate_candidate for one snapshot
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
    args = ap.parse_args(argv)
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


if __name__ == "__main__":
    raise SystemExit(main())
