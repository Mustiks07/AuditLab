"""An external ID/order transformer written from the published requirements only.

It deliberately imports nothing from ``auditlab``: it models what an organiser could build from
https://genesis-exe.com/fintech-checks.html (published 2026-11-20) and SPEC §11 «Безвредное
преобразование», without knowing AuditLab's internal transformer (auditlab/release/transforms.py).

Profiles (all keep amounts, nulls, timestamps, FX keys, policy keys and episode_id):

* ``reorder``  — permute the records of ledger, invoices, payments and fx; nothing is renamed.
* ``literal``  — rename exactly the fields the page lists for public files: row_id, doc_id,
  invoice_id, payment_id, entity_id, plus the §03 reference list invoice_refs. Nested AuditLab
  extensions (settlement_index, allocation_instructions) and journal_id are not known to it.
* ``discover`` — rename every string anywhere in public JSON/CSV that equals a known ID of one of
  those five ID spaces (a value-based transformer that needs no field list).

Truth references (affected_ids / evidence_ids) are rewritten in one of two ways:

* ``composite`` — a reference ``kind/value`` keeps ``kind`` and maps ``value`` (AuditLab form);
* ``opaque``    — the whole string is looked up in the ID map; ``ledger/R1`` is then never found.

ID styles: ``grammar`` (same first letter + 24 upper-case hex, like the generator), ``neutral``
(``row-000017``), ``numeric`` (``17``: a string of digits). The style only changes the new values.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import random
import shutil
from pathlib import Path

SPACES = {"row_id": "row", "doc_id": "doc", "invoice_id": "invoice", "payment_id": "payment", "entity_id": "entity"}
PUBLIC_TABLES = ("ledger.csv", "invoices.jsonl", "payments.jsonl", "fx.csv")
COPIED = ("manifest.json", "policy.json")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows), encoding="utf-8")


def read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        return list(reader.fieldnames or []), list(reader)


def write_csv(path: Path, header: list[str], rows: list[dict]) -> None:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=header, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    path.write_text(buffer.getvalue(), encoding="utf-8")


def collect_ids(public: Path) -> dict[str, set[str]]:
    """Old IDs per ID space, from the fields the page names (never from AuditLab code)."""
    ids = {space: set() for space in SPACES.values()}
    _, ledger = read_csv(public / "ledger.csv")
    for table in (ledger, read_jsonl(public / "invoices.jsonl"), read_jsonl(public / "payments.jsonl")):
        for row in table:
            for field, space in SPACES.items():
                value = row.get(field)
                if isinstance(value, str) and value not in ("", "null"):
                    ids[space].add(value)
    return ids


def new_id(space: str, old: str, seed: int, style: str, counter: int) -> str:
    digest = hashlib.sha256(f"external/{seed}/{space}/{old}".encode()).hexdigest().upper()
    if style == "grammar":
        return old[0] + digest[:24]
    if style == "neutral":
        return f"{space}-{counter:06d}"
    if style == "numeric":
        return str(10_000_000 + counter * 7 + {"row": 1, "doc": 2, "invoice": 3, "payment": 4, "entity": 5}[space])
    raise ValueError(style)


def build_maps(ids: dict[str, set[str]], seed: int, style: str) -> dict[str, dict[str, str]]:
    maps, counter = {}, 0
    for space, values in sorted(ids.items()):
        order = sorted(values)
        random.Random(f"{seed}/{space}").shuffle(order)
        maps[space] = {}
        for old in order:
            counter += 1
            maps[space][old] = new_id(space, old, seed, style, counter)
    flat: dict[str, str] = {}
    for mapping in maps.values():
        for old, new in mapping.items():
            if old in flat and flat[old] != new:
                raise ValueError(f"ID {old} occurs in two ID spaces; a value-based rename would be ambiguous")
            flat[old] = new
    if len(set(flat.values())) != len(flat):
        raise ValueError("new IDs are not one-to-one")
    return maps


def _rename_value(value, flat: dict[str, str]):
    if isinstance(value, str):
        if value in flat:
            return flat[value]
        kind, sep, rest = value.partition("/")
        if sep and rest in flat:
            return f"{kind}/{flat[rest]}"
        return value
    if isinstance(value, list):
        return [_rename_value(v, flat) for v in value]
    if isinstance(value, dict):
        return {k: _rename_value(v, flat) for k, v in value.items()}
    return value


def _rename_literal(row: dict, maps: dict[str, dict[str, str]]) -> dict:
    row = dict(row)
    for field, space in SPACES.items():
        if isinstance(row.get(field), str):
            row[field] = maps[space].get(row[field], row[field])
    if isinstance(row.get("invoice_refs"), list):
        row["invoice_refs"] = [maps["invoice"].get(v, v) for v in row["invoice_refs"]]
    return row


def map_reference(ref: str, flat: dict[str, str], mode: str) -> str:
    if mode == "opaque":
        return flat.get(ref, ref)
    kind, sep, rest = ref.partition("/")
    return f"{kind}/{flat[rest]}" if sep and rest in flat else ref


def transform(episode: Path, target: Path, *, profile: str, seed: int, style: str = "grammar",
              truth_mode: str = "composite", reorder: bool = True) -> dict:
    """Write a transformed copy ``target`` of ``episode``; the original is never modified."""
    if target.exists():
        raise FileExistsError(target)
    public, out = episode / "public", target / "public"
    out.mkdir(parents=True)
    for name in COPIED:
        shutil.copyfile(public / name, out / name)
    maps = build_maps(collect_ids(public), seed, style) if profile != "reorder" else {s: {} for s in SPACES.values()}
    flat = {old: new for mapping in maps.values() for old, new in mapping.items()}
    rng = random.Random(seed)

    def rows(rows_in: list[dict]) -> list[dict]:
        if profile == "literal":
            rows_in = [_rename_literal(r, maps) for r in rows_in]
        elif profile == "discover":
            rows_in = [_rename_value(r, flat) for r in rows_in]
        rows_in = list(rows_in)
        if reorder:
            rng.shuffle(rows_in)
        return rows_in

    for name in ("ledger.csv", "fx.csv"):
        header, data = read_csv(public / name)
        write_csv(out / name, header, rows(data))
    for name in ("invoices.jsonl", "payments.jsonl"):
        write_jsonl(out / name, rows(read_jsonl(public / name)))

    truth = json.loads((episode / "private" / "truth.json").read_text(encoding="utf-8"))
    for issue in truth.get("issues", []):
        issue["affected_ids"] = [map_reference(r, flat, truth_mode) for r in issue["affected_ids"]]
        issue["evidence_sets"] = [[map_reference(r, flat, truth_mode) for r in s] for s in issue["evidence_sets"]]
    (target / "private").mkdir()
    (target / "private" / "truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8")
    inverse = {new: old for old, new in flat.items()}
    return {"profile": profile, "style": style, "truth_mode": truth_mode, "reorder": reorder, "seed": seed,
            "renamed": len(flat), "inverse": inverse}


def inverse_reference(ref: str, inverse: dict[str, str]) -> str:
    kind, sep, rest = ref.partition("/")
    if sep and rest in inverse:
        return f"{kind}/{inverse[rest]}"
    return inverse.get(ref, ref)
