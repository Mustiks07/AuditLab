"""Run frozen public-feature experiments against a checked-out generator."""
import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory


def public_records(public, table):
    if table == "ledger":
        with (public / "ledger.csv").open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))
    return [json.loads(line) for line in
            (public / f"{table}.jsonl").read_text(encoding="utf-8").splitlines()]


def guesses(public):
    manifest = json.loads((public / "manifest.json").read_bytes())
    invoices = public_records(public, "invoices")
    ledger = public_records(public, "ledger")
    by_id = {item["invoice_id"]: item for item in invoices}
    copy_counts = Counter((item["entity_id"], item["invoice_id"]) for item in invoices)
    deadline = datetime.fromisoformat(manifest["period_end"])
    candidates = set()
    all_next_day = set()
    for p in public_records(public, "payments"):
        if (p["currency"] != manifest["base_currency"] or len(p["invoice_refs"]) != 1
                or len(p["settlement_index"]["unapplied_row_ids"]) != 2):
            continue
        invoice = by_id[p["invoice_refs"][0]]
        receipt = datetime.fromisoformat(p["timestamp"])
        if receipt - datetime.fromisoformat(invoice["issued_at"]) != timedelta(days=1):
            continue
        all_next_day.add("payment/" + p["payment_id"])
        scale = 10 ** manifest["currency_exponents"][p["currency"]]
        if p["amount_minor"] % (1000 * scale) or receipt.minute:
            candidates.add("payment/" + p["payment_id"])
    return {
        "archive_groups": sum(count > 1 for count in copy_counts.values()),
        "allocation_candidates": candidates,
        "all_next_day": all_next_day,
        "cutoff_candidates": {"invoice/" + line["invoice_id"] for line in ledger
            if line["account"] == "AR" and int(line["debit_minor"]) > 0
            and datetime.fromisoformat(line["timestamp"]) >= deadline},
        "valuations": sum(line["account"] == "FX_VALUATION" for line in ledger),
    }


def configs():
    standard = {"config_version": "1.1", "target_period": "2028-11", "timezone": "UTC",
                "company_size": {"ledger_rows_target": 240, "documents_target": 76}}
    explicit = {**standard, "target_period": "2028-02", "timezone": "Asia/Kathmandu",
                "base_currency": "KWD", "currencies": ["KWD", "KZT"],
                "clean_distractors": ["next_day_settlement"]}
    for seed in range(82460601, 82460617):
        for kind in ("duplicate_invoice", "payment_allocation", "period_cutoff", "fx_conversion"):
            for fault in (0, 1):
                yield kind, seed, {**standard, "issue_counts": {kind: fault}}
    for seed in range(82460701, 82460709):
        for fault in (0, 1):
            yield "allocation_explicit", seed, {**explicit, "issue_counts": {"payment_allocation": fault}}


def measure(generate_episode):
    counts = defaultdict(Counter)
    minimal = {}
    with TemporaryDirectory() as directory:
        for index, (kind, seed, config) in enumerate(configs()):
            path = Path(directory) / str(index)
            generate_episode(seed, config, path)
            predictions = guesses(path / "public")
            truth = json.loads((path / "private/truth.json").read_bytes())["issues"]
            row = counts[kind]
            row["snapshots"] += 1
            if kind in {"duplicate_invoice", "fx_conversion"}:
                feature = predictions["archive_groups" if kind == "duplicate_invoice" else "valuations"]
                row["raw_count_matches"] += (max(0, feature - 1) == len(truth))
                row[f"groups_on_{len(truth)}_faults/{feature}"] += 1
                observed = {"feature_count": feature}
            else:
                feature = predictions["cutoff_candidates" if kind == "period_cutoff" else "allocation_candidates"]
                actual = {key for t in truth for key in t["affected_ids"]
                          if key.startswith("invoice/" if kind == "period_cutoff" else "payment/")}
                row.update({"tp": len(feature & actual), "fp": len(feature - actual),
                            "fn": len(actual - feature)})
                observed = {"candidates": sorted(feature), "affected": sorted(actual)}
                if kind != "period_cutoff":
                    row["next_day_candidates"] += len(predictions["all_next_day"])
                    observed["all_next_day"] = sorted(predictions["all_next_day"])
            if seed in (82460601, 82460701):
                minimal[f"{seed}/{kind}/{len(truth)}"] = observed
    return {"counts": counts, "examples": minimal}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source.resolve()))
    from auditlab.generator import GENERATOR_VERSION, generate_episode
    result = {"generator": GENERATOR_VERSION, **measure(generate_episode)}
    args.output.write_bytes((json.dumps(result, indent=2, sort_keys=True) + "\n").encode())


if __name__ == "__main__":
    main()
