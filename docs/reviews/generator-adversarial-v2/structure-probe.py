"""Measure two fixed public-document shortcuts against separate labels."""

import argparse
import json
import shutil
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from auditlab.generator import generate_episode


DEVELOPMENT = range(945605100, 945605108)
HOLDOUT = range(945606200, 945606224)
ARCHIVE = "archive_copy"
DISTRACTORS = ["split_payment", "equal_amount_different_supplier", "refund", "next_day_settlement"]
VARIANTS = (
    ("clean/no-archive", False, {}, None),
    ("clean/with-archive", True, {}, None),
    ("duplicate-1/no-archive", False, {"duplicate_invoice": 1}, None),
    ("duplicate-1/with-archive", True, {"duplicate_invoice": 1}, None),
    ("duplicate-2/with-archive", True, {"duplicate_invoice": 2}, None),
    ("allocation-1", True, {"payment_allocation": 1}, None),
    ("fx-1", True, {"fx_conversion": 1}, None),
    ("clean/interaction", True, {}, "allocation_fx_same_payment"),
)
CONTEXTS = (
    ("KZT", "USD", "Asia/Almaty", "2026-11", 240, 76),
    ("USD", "EUR", "America/New_York", "2027-03", 400, 120),
    ("JPY", "KWD", "UTC", "2028-02", 560, 150),
)


def config(index, archive, issues, interaction):
    base, foreign, zone, month, rows, docs = CONTEXTS[index % len(CONTEXTS)]
    return {
        "config_version": "1.1",
        "base_currency": base,
        "currencies": [base, foreign],
        "timezone": zone,
        "target_period": month,
        "company_size": {"ledger_rows_target": rows, "documents_target": docs},
        "clean_distractors": [*DISTRACTORS, *([ARCHIVE] if archive else [])],
        "issue_counts": issues,
        **({"interaction": interaction} if interaction else {}),
    }


def json_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def public_signals(public):
    manifest = json.loads((public / "manifest.json").read_bytes())
    invoices = json_lines(public / "invoices.jsonl")
    payments = json_lines(public / "payments.jsonl")
    currency = manifest["base_currency"]
    thousand = 1000 * 10 ** manifest["currency_exponents"][currency]
    issuance = {row["invoice_id"]: datetime.fromisoformat(row["issued_at"]) for row in invoices}
    multiplicity = Counter(row["invoice_id"] for row in invoices)
    groups = sorted(key for key, count in multiplicity.items() if count >= 2)

    allocation_ids = set()
    or_ids = set()
    next_day_payments = {}
    for row in payments:
        if row["currency"] != currency or len(row["invoice_refs"]) != 1:
            continue
        receipt_at = datetime.fromisoformat(row["timestamp"])
        invoice_id, = row["invoice_refs"]
        if receipt_at - issuance[invoice_id] != timedelta(days=1):
            continue
        key = "payment/" + row["payment_id"]
        amount = row["amount_minor"]
        if amount % thousand:
            allocation_ids.add(key)
        if amount % thousand or receipt_at.minute:
            or_ids.add(key)
        next_day_payments[key] = {
            "minor": amount,
            "currency": currency,
            "received_at": row["timestamp"],
            "issued_at": issuance[invoice_id].isoformat(),
        }
    return groups, allocation_ids, or_ids, next_day_payments


def private_labels(private):
    issues = json.loads((private / "truth.json").read_bytes())["issues"]
    duplicates = sum(item["type"] == "duplicate_invoice" for item in issues)
    allocation_ids = {
        value
        for item in issues if item["type"] == "payment_allocation"
        for value in item["affected_ids"] if value.startswith("payment/")
    }
    return duplicates, allocation_ids


def tally(counts, predicted, actual):
    counts.update(tp=len(predicted & actual), fp=len(predicted - actual), fn=len(actual - predicted))


def run_block(output, seeds):
    support = Counter()
    archive_count_guesses = Counter()
    duplicate_presence = Counter()
    allocation_amount = Counter()
    allocation_amount_or_minute = Counter()
    same_seed_pairs = Counter()
    examples = {}
    work = output / "active"
    for index, seed in enumerate(seeds):
        observations = {}
        for variant, archive, issues, interaction in VARIANTS:
            generate_episode(seed, config(index, archive, issues, interaction), work)
            groups, guesses, or_guesses, next_day_payments = public_signals(work / "public")
            expected_duplicates, expected_allocations = private_labels(work / "private")
            support.update(companies=1, archive_companies=int(archive), duplicate_companies=int(bool(expected_duplicates)),
                           duplicate_faults=expected_duplicates, allocation_faults=len(expected_allocations))
            tally(duplicate_presence, {seed} if len(groups) >= 2 else set(), {seed} if expected_duplicates else set())
            if archive:
                archive_count_guesses.update(total=1, exact=int(max(len(groups) - 1, 0) == expected_duplicates))
            tally(allocation_amount, guesses, expected_allocations)
            tally(allocation_amount_or_minute, or_guesses, expected_allocations)
            observations[variant] = (len(groups), sorted(
                (payment["minor"], payment["received_at"], payment["issued_at"])
                for payment in next_day_payments.values()
            ))
            if index == 0 and variant in {"clean/with-archive", "duplicate-1/with-archive", "allocation-1"}:
                examples[variant] = {
                    "seed": seed, "public_duplicate_groups": groups, "public_base_next_day_payments": next_day_payments,
                    "private_duplicate_count": expected_duplicates, "private_allocation_payment_ids": sorted(expected_allocations),
                }
            shutil.rmtree(work)
        clean_groups, clean_payments = observations["clean/with-archive"]
        same_seed_pairs.update(
            seeds=1,
            equal_duplicate_group_count=int(clean_groups == observations["duplicate-1/with-archive"][0]),
            equal_next_day_metadata=int(clean_payments == observations["allocation-1"][1]),
        )
    return {
        "seed_first": seeds.start,
        "seed_last": seeds.stop - 1,
        "support": support,
        "A__duplicate_presence_two_or_more_groups": duplicate_presence,
        "A__duplicate_count_groups_minus_one_fixed_archive": archive_count_guesses,
        "B__allocation_amount": allocation_amount,
        "B_or_minute__allocation_amount_or_minute": allocation_amount_or_minute,
        "same_seed_clean_vs_one_fault": same_seed_pairs,
        "counterexample_seed_first": examples,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--counterexample-only", action="store_true", help="Run the first held-out seed only")
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    blocks = {"counterexample": range(HOLDOUT.start, HOLDOUT.start + 1)} if args.counterexample_only else {
        "development": DEVELOPMENT, "holdout": HOLDOUT,
    }
    report = {name: run_block(args.output, seeds) for name, seeds in blocks.items()}
    (args.output / "results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for name, result in report.items():
        print(name, {key: value for key, value in result.items() if key != "counterexample_seed_first"})


if __name__ == "__main__":
    main()
