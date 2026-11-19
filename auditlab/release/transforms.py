"""Bijective public ID/order transformation, separate from financial calculations.

The public transformation never reads truth. The trusted caller subsequently maps the
reference using the emitted mapping, after recording the independent public-only answer.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import json
from pathlib import Path
import random

ID_FIELDS = ("row_id", "journal_id", "invoice_id", "payment_id", "entity_id", "doc_id", "canonical_key")
PREFIXES = {"row_id": "ROW", "journal_id": "JOURNAL", "invoice_id": "INV", "payment_id": "PAY",
            "entity_id": "ENTITY", "doc_id": "DOC", "canonical_key": "CANON"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                         encoding="utf-8", newline="\n")
    temporary.replace(path)


def _csv(path):
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        return reader.fieldnames, list(reader)


def transform_public(source: Path, target: Path, seed: int) -> dict:
    """New public snapshot and full bijection. Never modifies source; supports >2,000 IDs."""
    rng = random.Random(seed)
    header, ledger = _csv(source / "ledger.csv")
    fx_header, fx = _csv(source / "fx.csv")
    docs = {table: [json.loads(line) for line in (source / f"{table}.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
            for table in ("invoices", "payments")}
    rows = ledger + docs["invoices"] + docs["payments"]
    seen = {field: set() for field in ID_FIELDS}
    for row in rows:
        for field in ID_FIELDS:
            value = row.get(field)
            if value is not None and value != "null":
                seen[field].add(value)
        for value in row.get("invoice_refs", []):
            seen["invoice_id"].add(value)
        for instruction in row.get("allocation_instructions", []):
            seen["invoice_id"].add(instruction["invoice_id"])
    # Reverse lexical order to expose heuristics that pick the smallest ID as the original.
    maps = {field: {old: f"{PREFIXES[field]}{len(values)-index:08d}X{seed:x}"
                    for index, old in enumerate(sorted(values))} for field, values in seen.items()}
    for row in rows:
        for field in ID_FIELDS:
            value = row.get(field)
            if value is not None and value != "null":
                row[field] = maps[field][value]
        if "invoice_refs" in row:
            row["invoice_refs"] = [maps["invoice_id"][value] for value in row["invoice_refs"]]
        for instruction in row.get("allocation_instructions", []):
            instruction["invoice_id"] = maps["invoice_id"][instruction["invoice_id"]]
        if "settlement_index" in row:
            index = row["settlement_index"]
            index["unapplied_row_ids"] = [maps["row_id"][value] for value in index["unapplied_row_ids"]]
            rng.shuffle(index["unapplied_row_ids"])
    target.mkdir(parents=True, exist_ok=False)
    manifest = read_json(source / "manifest.json")
    old_episode = manifest["episode_id"]
    new_episode = "T" + hashlib.sha256(f"{old_episode}:{seed}".encode()).hexdigest()[:30].upper()
    manifest["episode_id"] = new_episode
    write_json(target / "manifest.json", manifest)
    (target / "policy.json").write_bytes((source / "policy.json").read_bytes())
    for name, fields, records in (("ledger", header, ledger), ("fx", fx_header, fx)):
        rng.shuffle(records)
        with (target / f"{name}.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(records)
    for table, records in docs.items():
        rng.shuffle(records)
        with (target / f"{table}.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    refs = {}
    for namespace, field in (("ledger", "row_id"), ("invoices", "doc_id"), ("payments", "doc_id"),
                             ("invoice", "invoice_id"), ("payment", "payment_id")):
        refs.update({f"{namespace}/{old}": f"{namespace}/{new}" for old, new in maps[field].items()})
    return {"version": "id-order-1.0", "seed": seed, "old_episode_id": old_episode,
            "new_episode_id": new_episode, "raw_ids": maps, "references": refs}


def transform_truth(truth: dict, mapping: dict) -> dict:
    out = copy.deepcopy(truth)
    out["episode_id"] = mapping["new_episode_id"]
    refs = mapping["references"]
    for index, issue in enumerate(out["issues"]):
        issue["issue_id"] = f"TRANSFORMED-{index:03d}"
        issue["affected_ids"] = [refs.get(value, value) for value in issue["affected_ids"]]
        issue["evidence_sets"] = [[refs.get(value, value) for value in group] for group in issue["evidence_sets"]]
    return out


def comparable(findings: list[dict], references: dict | None = None) -> list[tuple]:
    refs = references or {}
    return sorted((f["type"], tuple(sorted(refs.get(a, a) for a in f["affected_ids"])),
                   f["claimed_amount_minor"], f["currency"],
                   tuple(sorted(refs.get(e, e) for e in f["evidence_ids"]))) for f in findings)
