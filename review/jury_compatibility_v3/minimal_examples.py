"""Minimal reproductions of the two ID-transformation risks on the smallest public fixture (S02).

    python -m review.jury_compatibility_v3.minimal_examples --output docs/reviews/jury-compatibility-v3/minimal-examples.json

S02: one payment D9/P9 of 50 000 ₸ for invoice I4, 30 000 ₸ applied before the boundary and the
residual 20 000 ₸ (2 000 000 minor) left; one truth issue with evidence payments/D9, ledger/R4,
ledger/R5, policy/allocation. Each example records the transformed fragment and the scores
through ``judge_adapter`` and the built-in public auditor ``reconcile``.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from review.jury_compatibility_v3 import check_ids, external_transform as ext  # noqa: E402

S02 = ROOT / "fixtures" / "foundation" / "S02"


def example(adapter, work: Path, name: str, **options) -> dict:
    target = work / name
    meta = ext.transform(S02, target, seed=1, **options)
    truth = json.loads((target / "private/truth.json").read_text(encoding="utf-8"))
    payment = ext.read_jsonl(target / "public/payments.jsonl")[0]
    _, ledger = ext.read_csv(target / "public/ledger.csv")
    data = check_ids.load(target / "public")
    return {"transform": {k: meta[k] for k in ("profile", "style", "truth_mode", "reorder")},
            "public_payment": payment, "ledger_row_ids": sorted(r["row_id"] for r in ledger),
            "truth_evidence": truth["issues"][0]["evidence_sets"][0],
            "link_problems": check_ids.link_problems(data, truth),
            "evidence_unavailable": check_ids.evidence_served(adapter, target, truth),
            "oracle_score": check_ids.score(adapter, target, check_ids.oracle(truth)),
            "reconcile_score": check_ids.reconcile_score(target, work / "runs" / name)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    import judge_adapter as adapter
    with tempfile.TemporaryDirectory(prefix="jury-v3-min-") as temporary:
        work = Path(temporary)
        truth = json.loads((S02 / "private/truth.json").read_text(encoding="utf-8"))
        report = {
            "fixture": "fixtures/foundation/S02",
            "original": {"oracle_score": check_ids.score(adapter, S02, check_ids.oracle(truth)),
                         "reconcile_score": check_ids.reconcile_score(S02, work / "runs" / "original")},
            "A_value_based_rename_compatible": example(adapter, work, "discover", profile="discover", style="neutral",
                                                       truth_mode="composite", reorder=True),
            "B_field_list_rename_leaves_settlement_index": example(adapter, work, "literal", profile="literal",
                                                                    style="neutral", truth_mode="composite", reorder=True),
            "C_opaque_truth_references": example(adapter, work, "opaque", profile="literal", style="neutral",
                                                 truth_mode="opaque", reorder=False),
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({k: (v.get("oracle_score"), v.get("reconcile_score")) for k, v in report.items() if isinstance(v, dict)},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
