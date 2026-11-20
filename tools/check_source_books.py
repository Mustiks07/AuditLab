#!/usr/bin/env python3
"""Independently check exported clean books, complete transitions and final answers.

python tools/check_source_books.py SNAPSHOT [SNAPSHOT ...] --output report.json
The verifier receives standalone copies of six public-format files, never provenance
or truth. Hashes bind this report to its inputs; they are not authorship signatures.
"""
from __future__ import annotations

import argparse
import csv
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from auditlab.verifier import verify_report
from auditlab.verifier.crosscheck import compare

FILES = {"manifest.json", "policy.json", "ledger.csv", "invoices.jsonl", "payments.jsonl", "fx.csv"}


def _load(path):
    return json.loads(path.read_bytes())


def _hash(path):
    return sha256(path.read_bytes()).hexdigest()


def _records(directory, table):
    if table != "ledger":
        rows = [json.loads(line) for line in (directory / f"{table}.jsonl").read_text().splitlines()]
        key = "doc_id"
    else:
        with (directory / "ledger.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        for row in rows:
            for field, value in row.items():
                row[field] = int(value) if field in {"debit_minor", "credit_minor"} else None if value == "null" else value
        key = "row_id"
    result = {row[key]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError("duplicate_record_id")
    return result


def check_source_book(snapshot):
    snapshot = Path(snapshot)
    private, public = snapshot / "private", snapshot / "public"
    provenance = _load(private / "provenance.json")
    metadata = provenance.get("source_book")
    if not metadata:
        return {"snapshot": str(snapshot), "status": "not_checked", "reason": "no_exported_source_book"}
    if (metadata.get("schema_version") != "1.0" or metadata.get("path") != "source_book"
            or metadata.get("transition_path") != "source_transition.json"):
        raise ValueError("unsupported_source_book_metadata")
    source = private / "source_book"
    for label, directory, key in (("source", source, "files_sha256"), ("target", public, "target_files_sha256")):
        if set(metadata[key]) != FILES or {p.name for p in directory.iterdir()} != FILES:
            raise ValueError(f"{label}_file_set_mismatch")
        for filename, digest in metadata[key].items():
            if _hash(directory / filename) != digest:
                raise ValueError(f"{label}_hash_mismatch:{filename}")
    transition_path = private / "source_transition.json"
    if _hash(transition_path) != metadata["transition_sha256"]:
        raise ValueError("transition_hash_mismatch")
    transition = _load(transition_path)
    original_manifest, target_manifest = _load(source / "manifest.json"), _load(public / "manifest.json")
    if (transition.get("schema_version") != "1.0"
            or transition.get("source_episode_id") != original_manifest["episode_id"]
            or metadata["episode_id"] != original_manifest["episode_id"]
            or transition.get("target_episode_id") != target_manifest["episode_id"]
            or provenance["episode_id"] != target_manifest["episode_id"]):
        raise ValueError("source_book_identity_mismatch")
    # Only accounting rows and settlement indexes are mutable in generator 1.5.
    for filename in ("policy.json", "fx.csv", "invoices.jsonl"):
        if filename.endswith(".json"):
            equal = _load(source / filename) == _load(public / filename)
        elif filename.endswith(".jsonl"):
            equal = _records(source, "invoices") == _records(public, "invoices")
        else:
            with (source / filename).open() as a, (public / filename).open() as b:
                equal = sorted(a.read().splitlines()) == sorted(b.read().splitlines())
        if not equal:
            raise ValueError(f"unexpected_source_document_change:{filename}")
    invariant = set(original_manifest) | set(target_manifest)
    invariant -= {"episode_id", "ledger_rows", "document_count"}
    if any(original_manifest.get(k) != target_manifest.get(k) for k in invariant):
        raise ValueError("unexpected_manifest_change")
    records = {table: _records(source, table) for table in ("ledger", "invoices", "payments")}
    seen, linked_mutations = set(), set()
    for change in transition["changes"]:
        table, key = change["table"], change["id"]
        indexes = change["mutation_indexes"]
        if (table not in records or (table, key) in seen or not indexes
                or any(type(index) is not int or not 0 <= index < len(provenance["mutations"]) for index in indexes)):
            raise ValueError("invalid_transition_change")
        seen.add((table, key))
        linked_mutations.update(indexes)
        for index in indexes:
            mutation = provenance["mutations"][index]
            entities = {f"{kind}/{record[kind + '_id']}" for record in (change["before"], change["after"])
                        if record is not None for kind in ("invoice", "payment") if record.get(kind + "_id")}
            if not entities.intersection(mutation["affected_ids"]):
                raise ValueError("transition_mutation_entity_mismatch")
        if records[table].get(key) != change["before"] or change["before"] == change["after"]:
            raise ValueError("transition_before_mismatch")
        if change["after"] is None:
            records[table].pop(key)
        else:
            field = "row_id" if table == "ledger" else "doc_id"
            if change["after"].get(field) != key:
                raise ValueError("transition_record_id_mismatch")
            records[table][key] = change["after"]
    if linked_mutations != set(range(len(provenance["mutations"]))):
        raise ValueError("transition_missing_mutation")
    if any(records[table] != _records(public, table) for table in records):
        raise ValueError("transition_incomplete")
    for directory, manifest in ((source, original_manifest), (public, target_manifest)):
        if (manifest["ledger_rows"] != len(_records(directory, "ledger"))
                or manifest["document_count"] != len(_records(directory, "invoices")) + len(_records(directory, "payments"))):
            raise ValueError("manifest_size_mismatch")
    # The independent SQL verifier receives copies containing only public-format files.
    with TemporaryDirectory(prefix="auditlab-source-check-") as temporary:
        clean = Path(temporary) / "clean"
        target = Path(temporary) / "target"
        shutil.copytree(source, clean)
        shutil.copytree(public, target)
        clean_report = verify_report(clean)
        target_report = verify_report(target)
        # Open truth only after both independently calculated answers exist.
        truth = _load(private / "truth.json")
        discrepancies = compare(target_report, truth, target)
    if clean_report["findings"] or clean_report["notes"]:
        raise ValueError("source_book_not_independently_clean")
    if target_report["notes"] or discrepancies:
        raise ValueError("target_truth_disagrees_with_independent_verifier")
    return {"snapshot": str(snapshot), "status": "passed", "generator_version": provenance["generator_version"],
            "source_episode_id": original_manifest["episode_id"], "target_episode_id": target_manifest["episode_id"],
            "source_findings": 0, "target_findings": len(target_report["findings"]),
            "changed_records": len(seen), "source_files_sha256": metadata["files_sha256"],
            "target_files_sha256": metadata["target_files_sha256"], "transition_sha256": metadata["transition_sha256"],
            "provenance_sha256": _hash(private / "provenance.json"), "truth_sha256": _hash(private / "truth.json"),
            "verifier": {"engine": clean_report["engine"], "engine_version": clean_report["engine_version"],
                         "rules_version": clean_report["rules_version"]}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshots", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    rows = []
    for snapshot in args.snapshots:
        try:
            rows.append(check_source_book(snapshot))
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
            rows.append({"snapshot": str(snapshot), "status": "failed", "reason": str(error)})
    result = {"report_version": "1.0", "planned": len(args.snapshots), "results": rows,
              "passed": sum(row["status"] == "passed" for row in rows)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(f"Clean-source checks: {result['passed']}/{result['planned']}; report: {args.output}")
    return 0 if result["passed"] == result["planned"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
