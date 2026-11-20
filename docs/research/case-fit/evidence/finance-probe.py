"""Build the preregistered audit inputs and confirm the frozen answers afterward."""

import argparse
import csv
import hashlib
import json
import sys
import time
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = Path(__file__).resolve().parent
WORK = ROOT / "artifacts" / "case-fit-finance"
sys.path.insert(0, str(ROOT))

ISSUES = ("duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff")


def planned_cases():
    common = {"config_version": "1.1", "timezone": "UTC", "clean_distractors": []}
    for label, seed, interaction, first in (
        ("A", 84270021, "allocation_fx_same_payment", "payment_allocation"),
        ("B", 84270022, "cutoff_fx_same_invoice", "period_cutoff"),
    ):
        for a, b in ((0, 0), (1, 0), (0, 1), (1, 1)):
            yield f"{label}{a}{b}", seed, dict(
                common, interaction=interaction, issue_counts={first: a, "fx_conversion": b}
            )
    yield "MAX", 84270023, {
        "config_version": "1.1",
        "timezone": "America/New_York",
        "target_period": "2026-03",
        "currencies": ["KZT", "JPY", "KWD"],
        "company_size": {"ledger_rows_target": 2000, "documents_target": 200},
        "issue_counts": dict.fromkeys(ISSUES, 3),
        "interaction": "allocation_fx_same_payment",
    }
    yield "REFUND", 84270024, dict(common, currencies=["KZT"], clean_distractors=["refund"])
    yield "JPY-TWIN", 0, jpy_config(0)


def jpy_config(count):
    return dict(config_version="1.1", base_currency="JPY", currencies=["JPY", "EUR"],
                clean_distractors=[], interaction="allocation_fx_same_payment",
                issue_counts={"fx_conversion": count})


def out_json(path, payload):
    path.write_bytes((json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8"))


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def out_csv(path, records, fields):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows({key: "null" if val is None else val for key, val in row.items()}
                         for row in records)


def out_jsonl(path, records):
    path.write_bytes("".join(json.dumps(row, sort_keys=True) + "\n" for row in records).encode("utf-8"))


def build():
    from auditlab.generator import generate_episode
    from auditlab.generator.core import Company, LEDGER_FIELDS, FX_FIELDS

    snapshots = WORK / "snapshots"
    snapshots.mkdir(parents=True)
    for label, seed, config in planned_cases():
        source = {}
        apply = Company.apply_mutations

        def copy_source(book):
            source.update({key: deepcopy(getattr(book, key))
                           for key in ("ledger", "invoices", "payments", "fx")})
            return apply(book)

        with patch.object(Company, "apply_mutations", copy_source):
            generate_episode(seed, config, snapshots / label)

        public = snapshots / label / "public"
        source_dir = snapshots / label / "captured-source"
        source_dir.mkdir()
        manifest = json.loads((public / "manifest.json").read_bytes())
        manifest.update(ledger_rows=len(source["ledger"]),
                        document_count=len(source["invoices"]) + len(source["payments"]))
        out_json(source_dir / "manifest.json", manifest)
        (source_dir / "policy.json").write_bytes((public / "policy.json").read_bytes())
        out_csv(source_dir / "ledger.csv", source["ledger"], LEDGER_FIELDS)
        out_csv(source_dir / "fx.csv", source["fx"], FX_FIELDS)
        for table in ("invoices", "payments"):
            out_jsonl(source_dir / f"{table}.jsonl", source[table])

    make_fold_fixture(snapshots / "FOLD")
    out_json(WORK / "public-input-hashes.json", {
        f"{case.name}/{directory}": {file.name: file_hash(file)
                                     for file in sorted((case / directory).iterdir())}
        for case in sorted(snapshots.iterdir())
        for directory in ("public", "captured-source") if (case / directory).is_dir()
    })


def make_fold_fixture(root):
    # Reuse the test helper only for fixture I/O and the debit/credit row schema.
    sys.path.insert(0, str(ROOT / "tests" / "verifier"))
    from cases import Case, dump

    case = Case("EF011D")
    for idx, event, posted_amount in ((1, "2026-11-01T05:30:00+00:00", 251),
                                      (2, "2026-11-01T06:30:00+00:00", 250)):
        entity = f"C{idx}"
        invoice = f"I{idx}"
        case.invoice(f"D{idx}", invoice, event, 1, "JPY", entity)
        case.journal(event, ("AR", 1, 0), ("REVENUE", 0, 1), currency="JPY",
                     entity=entity, invoice=invoice)
        case.journal(event, ("FX_VALUATION", posted_amount, 0), ("FX_OFFSET", 0, posted_amount),
                     entity=entity, invoice=invoice)
    for day, rate in (("2026-10-31", "2.49"), ("2026-11-01", "2.505"), ("2026-11-02", "2.52")):
        case.rate(day, "JPY", "KZT", rate)
    public = case.write(root)
    start, end = "2026-11-01T00:00:00-04:00", "2026-12-01T00:00:00-05:00"
    manifest = json.loads((public / "manifest.json").read_bytes())
    manifest.update(timezone="America/New_York", period_start=start, period_end=end)
    dump(public / "manifest.json", manifest)
    policy = json.loads((public / "policy.json").read_bytes())
    policy["allocation"]["deadline"] = end
    dump(public / "policy.json", policy)


def key(finding, amount_field):
    return (finding["type"], tuple(sorted(finding["affected_ids"])),
            finding[amount_field], finding["currency"])


def compare(expected_hash):
    from auditlab.environment import open_session
    from auditlab.policies import make_policy
    from auditlab.verifier import verify_report

    answer = EVIDENCE / "finance-public-answer.json"
    if file_hash(answer) != expected_hash:
        raise ValueError("Public answer must be frozen before comparison")
    answers = json.loads(answer.read_bytes())
    for label, result in answers.items():
        root = WORK / "snapshots" / label
        public = root / "public"
        input_hashes = {file.name: file_hash(file) for file in sorted(public.iterdir())}
        if result["input_sha256"] != input_hashes:
            raise ValueError(f"Unexpected public input hashes for {label}")
        expected = Counter(key(finding, "claimed_amount_minor") for finding in result["findings"])
        evidence = {key(finding, "claimed_amount_minor"): set(finding["evidence_ids"])
                    for finding in result["findings"]}
        row = {}
        private = root / "private"
        if private.is_dir():
            truth = json.loads((private / "truth.json").read_bytes())["issues"]
            row["truth_equals"] = Counter(key(f, "expected_amount_minor") for f in truth) == expected
            row["truth_evidence_accepted"] = sum(
                any(set(group) <= evidence.get(key(finding, "expected_amount_minor"), set())
                    for group in finding["evidence_sets"]) for finding in truth
            )
        for engine in ("sqlite", "duckdb"):
            report = verify_report(public, engine=engine)
            row[engine] = {"equals": Counter(key(f, "claimed_amount_minor")
                       for f in report["findings"]) == expected, "notes": report["notes"]}
        source = root / "captured-source"
        if source.is_dir():
            source_hashes = {file.name: file_hash(file) for file in sorted(source.iterdir())}
            if source_hashes != result["captured_source_sha256"]:
                raise ValueError(f"Unexpected source input hashes for {label}")
            report = verify_report(source, engine="sqlite")
            row["source"] = {"findings": len(report["findings"]), "notes": report["notes"]}
            row["ledger_delta"] = ledger_delta(source / "ledger.csv", public / "ledger.csv")
        session = open_session(public)
        policy = make_policy("reconcile", 17)
        manifest = json.loads((public / "manifest.json").read_bytes())
        observation = {"manifest": manifest, "remaining_steps": 100}
        started = time.perf_counter()
        observed = set()
        for count in range(1, 101):
            action = policy.act(observation)
            if action["action"] == "submit":
                break
            reply = session.inspect(action)
            assert reply["ok"], reply
            assert action["action"] == "list_records"
            table = action["args"]["table"]
            for record in reply["result"]["records"]:
                if table == "fx":
                    record_id = "/".join(record[field] for field in ("date", "source_currency", "target_currency"))
                else:
                    record_id = record[{"ledger": "row_id", "invoices": "doc_id", "payments": "doc_id",
                                        "policy": "id"}[table]]
                observed.add(f"{table}/{record_id}")
            observation = {**reply, "remaining_steps": 100 - count}
        submitted = action["args"]["findings"]
        row["reconcile"] = {"equals": Counter(key(f, "claimed_amount_minor") for f in submitted) == expected,
                            "steps": count, "seconds": round(time.perf_counter() - started, 3),
                            "evidence_observed": sum(set(f["evidence_ids"]) <= observed for f in submitted)}
        print(label, json.dumps(row, sort_keys=True))


def ledger_delta(source, posted):
    def read(path):
        with path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        journal = Counter()
        for row in rows:
            journal[row["journal_id"], row["currency"]] += int(row["debit_minor"]) - int(row["credit_minor"])
        assert all(net == 0 for net in journal.values())
        return {row["row_id"]: row for row in rows}

    before, after = read(source), read(posted)
    return {"source_rows": len(before), "posted_rows": len(after),
            "added": len(after.keys() - before.keys()), "removed": len(before.keys() - after.keys()),
            "timestamp_changed": sum(before[row]["timestamp"] != after[row]["timestamp"]
                                     for row in before.keys() & after.keys()),
            "amount_changed": sum(
                any(before[row][field] != after[row][field] for field in ("debit_minor", "credit_minor"))
                for row in before.keys() & after.keys())}


def shortcut_build():
    """Public candidate identification based on event timing and index cardinality only."""
    cases = sorted(p for split in ("train", "dev") for p in (ROOT / "datasets" / "open" / split).iterdir())
    cases += sorted(p for p in (WORK / "snapshots").iterdir() if p.name != "FOLD")
    result = {}
    for folder in cases:
        public = folder / "public"
        docs = [json.loads(row) for row in (public / "invoices.jsonl").read_bytes().splitlines()]
        invoices = {doc["invoice_id"]: doc for doc in docs}
        payments = [json.loads(row) for row in (public / "payments.jsonl").read_bytes().splitlines()]
        candidates = set()
        for payment in payments:
            if len(payment["invoice_refs"]) != 1 or len(payment["settlement_index"]["unapplied_row_ids"]) != 2:
                continue
            (invoice_id,) = payment["invoice_refs"]
            issued = datetime.fromisoformat(invoices[invoice_id]["issued_at"]).replace(tzinfo=None)
            received = datetime.fromisoformat(payment["timestamp"]).replace(tzinfo=None)
            if received - issued == timedelta(days=1):
                candidates.add("payment/" + payment["payment_id"])
        result[str(folder.relative_to(ROOT)).replace("\\", "/")] = sorted(candidates)
    out_json(EVIDENCE / "finance-shortcut-public.json", result)


def shortcut_compare(expected_hash):
    answer = EVIDENCE / "finance-shortcut-public.json"
    if file_hash(answer) != expected_hash:
        raise ValueError("Candidate file must be frozen before comparison")
    totals = Counter()
    for name, candidates in json.loads(answer.read_bytes()).items():
        private = ROOT / name / "private" / "truth.json"
        truth = json.loads(private.read_bytes())["issues"]
        expected = {ref for finding in truth if finding["type"] == "payment_allocation"
                    for ref in finding["affected_ids"] if ref.startswith("payment/")}
        split = name.split("/")[2] if name.startswith("datasets/") else "new"
        found = set(candidates)
        totals[split, "episodes"] += 1
        totals[split, "tp"] += len(found & expected)
        totals[split, "fp"] += len(found - expected)
        totals[split, "fn"] += len(expected - found)
    print(json.dumps({f"{split}/{mode}": value for (split, mode), value in sorted(totals.items())}))


def reproduce_jpy_error():
    from auditlab.generator import generate_episode

    failed = {}
    with TemporaryDirectory() as d:
        for seed in range(40):
            try:
                generate_episode(seed, jpy_config(1), Path(d) / str(seed))
            except RuntimeError as error:
                failed[seed] = str(error)
    print(json.dumps(failed, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("build", "compare", "jpy-error", "shortcut-build", "shortcut-compare"))
    parser.add_argument("--answer-sha256")
    arguments = parser.parse_args()
    if arguments.stage == "build":
        build()
    elif arguments.stage == "compare":
        compare(arguments.answer_sha256)
    elif arguments.stage == "shortcut-build":
        shortcut_build()
    elif arguments.stage == "shortcut-compare":
        shortcut_compare(arguments.answer_sha256)
    else:
        reproduce_jpy_error()
