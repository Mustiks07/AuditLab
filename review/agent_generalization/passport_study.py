"""Full passport (P1-P11) over public dev episodes, with both kinds of clean control for P9.

    python review/agent_generalization/passport_study.py EPISODES_DIR WORK_DIR

For each generator episode: validate_candidate with the adapter (independent solver included), then
P9 against (a) the generator's defect-off episode of the same seed and config, (b) the episode's own
source book. Public dev seeds only.
"""
from __future__ import annotations

import json, shutil, sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import judge_adapter  # noqa: E402
from auditlab.generator import generate_episode  # noqa: E402
from review.agent_generalization.pairs import matched_clean_control  # noqa: E402
from review.agent_generalization.validator import validate_candidate, clean_variant  # noqa: E402

Z = {"duplicate_invoice": 0, "payment_allocation": 0, "fx_conversion": 0, "period_cutoff": 0}


def main(argv=None) -> int:
    eps_dir, work = (Path(a).resolve() for a in (argv or sys.argv[1:]))
    work.mkdir(parents=True, exist_ok=True)
    rows = []
    for ep in sorted(p for p in eps_dir.iterdir() if (p / "private" / "provenance.json").is_file()):
        v = validate_candidate(ep, adapter=judge_adapter, work=work / "val" / ep.name)
        prov = json.loads((ep / "private" / "provenance.json").read_text(encoding="utf-8"))
        row = {"episode": ep.name, "stratum": ep.name.rsplit("-", 1)[0], "status": v["status"],
               "checks": {k: c["status"] for k, c in v["checks"].items()},
               "solver_actions": v["checks"]["independent_solver"].get("actions"), "solver_elapsed_s": v["checks"]["independent_solver"].get("elapsed_s"),
               "truth_issues": v["truth_issues"]}
        if v["truth_issues"]:
            off = dict(prov["config"], issue_counts=dict(Z)); off.pop("settlement_timing", None)
            if off.get("interaction"): off["interaction"] = None
            d_off = work / "off" / ep.name
            if d_off.exists(): shutil.rmtree(d_off)
            try:
                generate_episode(prov["seed"], off, d_off)
                row["P9_defect_off"] = matched_clean_control(ep, d_off)
            except ValueError as error:
                row["P9_defect_off"] = {"status": "not_checked", "reason": str(error)}
            d_sb = work / "sb" / ep.name
            if d_sb.exists(): shutil.rmtree(d_sb)
            clean_variant(ep, d_sb)
            row["P9_source_book"] = matched_clean_control(ep, d_sb)
        rows.append(row)
    tally = {"overall": Counter(r["status"] for r in rows),
             "checks": {k: Counter(r["checks"][k] for r in rows) for k in rows[0]["checks"]},
             "solver_max_actions": max(r["solver_actions"] or 0 for r in rows),
             "solver_max_elapsed_s": max(r["solver_elapsed_s"] or 0 for r in rows),
             "P9": {}}
    for kind in ("P9_defect_off", "P9_source_book"):
        by = {}
        for r in rows:
            if kind in r:
                by.setdefault(r["stratum"], Counter())[r[kind]["status"]] += 1
        tally["P9"][kind] = {s: dict(c) for s, c in sorted(by.items())}
    signals = Counter()
    for r in rows:
        for kind in ("P9_defect_off", "P9_source_book"):
            for f in (r.get(kind) or {}).get("differing_features", {}):
                signals[(kind, r["stratum"], f)] += 1
            if (r.get(kind) or {}).get("checks", {}).get("same_commercial_history_as_source_book") is False:
                signals[(kind, r["stratum"], "different_commercial_history")] += 1
    tally["P9_signals"] = [list(k) + [n] for k, n in sorted(signals.items())]
    (work / "passport-report.json").write_text(json.dumps({"rows": rows, "tally": tally}, indent=1, default=str), encoding="utf-8")
    print(json.dumps(tally, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
