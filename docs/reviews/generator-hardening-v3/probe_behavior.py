"""Measure what judge_adapter.generate_episode actually produces for a set of configurations.

Run from the repository root (pinned tzdata needed for profiles 1.1+, e.g. in the runtime image):
    python docs/reviews/generator-hardening-v3/probe_behavior.py --seeds 1-8 --out probe.json
Writes one JSON row per (configuration, seed) and an aggregate per configuration. Truth and
provenance are read here only as the trusted side; nothing is passed to a policy.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import judge_adapter  # noqa: E402

TYPICAL = {
    "clean": {"issue_counts": {}},
    "duplicate": {"issue_counts": {"duplicate_invoice": 1}},
    "allocation": {"issue_counts": {"payment_allocation": 1}},
    "fx": {"currencies": ["KZT", "USD"], "issue_counts": {"fx_conversion": 1}},
    "cutoff": {"issue_counts": {"period_cutoff": 1}},
    "allocation_fx": {"currencies": ["KZT", "USD"], "interaction": "allocation_fx_same_payment",
                      "issue_counts": {"payment_allocation": 1, "fx_conversion": 1}},
    "cutoff_fx": {"currencies": ["KZT", "USD"], "interaction": "cutoff_fx_same_invoice",
                  "issue_counts": {"period_cutoff": 1, "fx_conversion": 1}},
}


def configurations(profiles):
    yield "empty", {}
    for profile in profiles:
        for name, config in TYPICAL.items():
            yield f"{name}@{profile or 'default'}", ({**config, "config_version": profile} if profile else dict(config))


def describe(episode: Path) -> dict:
    public, private = episode / "public", episode / "private"
    manifest = json.loads((public / "manifest.json").read_text(encoding="utf-8"))
    with (public / "ledger.csv").open(encoding="utf-8") as stream:
        ledger = list(csv.DictReader(stream))
    invoices = [json.loads(line) for line in (public / "invoices.jsonl").read_text(encoding="utf-8").splitlines()]
    payments = [json.loads(line) for line in (public / "payments.jsonl").read_text(encoding="utf-8").splitlines()]
    truth = json.loads((private / "truth.json").read_text(encoding="utf-8"))
    provenance = json.loads((private / "provenance.json").read_text(encoding="utf-8"))
    amounts = [int(row["debit_minor"]) or int(row["credit_minor"]) for row in ledger]
    tranches = Counter(len(p["settlement_index"]["unapplied_row_ids"]) for p in payments if p["invoice_refs"])
    return {
        "generator_version": provenance["generator_version"],
        "ledger_rows": len(ledger), "manifest_rows_match": manifest["ledger_rows"] == len(ledger),
        "invoices": len(invoices), "payments": len(payments),
        "ledger_pages_at_50": -(-len(ledger) // 50), "document_pages_at_50": -(-len(invoices) // 50) - (-len(payments) // 50),
        "currencies_used": sorted({row["currency"] for row in ledger}),
        "period": [manifest["period_start"], manifest["period_end"]], "timezone": manifest["timezone"],
        "accounts": sorted({row["account"] for row in ledger}),
        "amount_min": min(amounts), "amount_max": max(amounts), "distinct_amounts": len(set(amounts)),
        "event_kinds": dict(sorted(Counter(event["kind"] for event in provenance["events"]).items())),
        "designated_payment_unapplied_movements": dict(sorted(tranches.items())),
        "truth_types": dict(sorted(Counter(issue["type"] for issue in truth["issues"]).items())),
        "truth_amounts": sorted(issue["expected_amount_minor"] for issue in truth["issues"]),
    }


def seeds(text):
    low, _, high = text.partition("-")
    return range(int(low), int(high or low) + 1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", default="1-8")
    parser.add_argument("--profiles", default=",1.1,1.2", help="comma list; empty item = no config_version")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    rows, summary = [], {}
    with tempfile.TemporaryDirectory() as tmp:
        for name, config in configurations(args.profiles.split(",")):
            described = []
            for seed in seeds(args.seeds):
                target = Path(tmp) / f"{len(rows)}"
                try:
                    judge_adapter.generate_episode(seed, config, str(target))
                except ValueError as error:
                    rows.append({"config_name": name, "config": config, "seed": seed, "rejected": str(error)})
                    continue
                row = {"config_name": name, "config": config, "seed": seed, **describe(target)}
                rows.append(row)
                described.append(row)
            if described:
                summary[name] = {
                    "generated": len(described),
                    "generator_versions": sorted({r["generator_version"] for r in described}),
                    "ledger_rows": [min(r["ledger_rows"] for r in described), max(r["ledger_rows"] for r in described)],
                    "documents": [min(r["invoices"] + r["payments"] for r in described),
                                  max(r["invoices"] + r["payments"] for r in described)],
                    "ledger_pages_max": max(r["ledger_pages_at_50"] for r in described),
                    "currencies_used": sorted({c for r in described for c in r["currencies_used"]}),
                    "periods": sorted({tuple(r["period"]) for r in described}),
                    "truth_types": dict(sum((Counter(r["truth_types"]) for r in described), Counter())),
                    "median_distinct_amounts": statistics.median(r["distinct_amounts"] for r in described),
                    "event_kinds": sorted({k for r in described for k in r["event_kinds"]}),
                    "unapplied_movement_counts": sorted({int(k) for r in described
                                                         for k in r["designated_payment_unapplied_movements"]}),
                }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump({"seeds": args.seeds, "summary": summary, "rows": rows}, stream, ensure_ascii=False, indent=1,
                  sort_keys=True, default=list)
        stream.write("\n")
    for name, item in summary.items():
        print(name, json.dumps(item, ensure_ascii=False, default=list))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
