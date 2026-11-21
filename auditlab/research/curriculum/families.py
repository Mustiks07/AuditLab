"""Build paired controls and check their expected financial changes."""
from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from auditlab.generator import generate_episode
from auditlab.release.rehearsal import file_hashes
from auditlab.release.transforms import ID_FIELDS, read_json, transform_public, transform_truth, write_json
from auditlab.verifier.crosscheck import _read_units, _write_units


def _rename_record(record, maps):
    if record is None:
        return None
    record = dict(record)
    for field in ID_FIELDS:
        if record.get(field) not in (None, "null", ""):
            record[field] = maps[field].get(record[field], f"SOURCE{record[field]}")
    if "invoice_refs" in record:
        record["invoice_refs"] = [maps["invoice_id"][key] for key in record["invoice_refs"]]
    if "settlement_index" in record:
        index = dict(record["settlement_index"])
        index["unapplied_row_ids"] = [maps["row_id"].get(key, f"SOURCE{key}")
                                     for key in index["unapplied_row_ids"]]
        record["settlement_index"] = index
    if "allocation_instructions" in record:
        record["allocation_instructions"] = [
            {**item, "invoice_id": maps["invoice_id"][item["invoice_id"]]}
            for item in record["allocation_instructions"]
        ]
    return record


def _remapped_units(book, maps):
    units = _read_units(book)
    units["ledger"] = [_rename_record(record, maps) for record in units["ledger"]]
    for table in ("invoices", "payments"):
        units[table] = [json.dumps(_rename_record(json.loads(line), maps), sort_keys=True, separators=(",", ":"))
                        for line in units[table]]
    return units


def _episode_alias(previous, seed):
    return "E" + sha256(f"curriculum/id-order/1:{previous}:{seed}".encode()).hexdigest()[:32].upper()


def _rename_public(original, target, seed):
    target.parent.mkdir(parents=True, exist_ok=False)
    with TemporaryDirectory(dir=target.parent) as temporary:
        intermediate = Path(temporary) / "public"
        mapping = transform_public(original, intermediate, seed)
        # Use the generator's ID grammar in the paired experiments. The release
        # transform's sequential aliases would make variant membership visible.
        display = {field: {alias: old[0] + sha256(
            f"curriculum/id-order/1:{field}:{seed}:{old}".encode()).hexdigest()[:24].upper()
                          for old, alias in aliases.items()} for field, aliases in mapping["raw_ids"].items()}
        _write_units(intermediate, target, _remapped_units(intermediate, display))
    mapping["new_episode_id"] = _episode_alias(mapping["old_episode_id"], seed)
    mapping["version"] = "curriculum-id-order-1.0"
    manifest = read_json(target / "manifest.json")
    write_json(target / "manifest.json", {**manifest, "episode_id": mapping["new_episode_id"]})
    for namespace, reference in mapping["references"].items():
        kind, _, alias = reference.partition("/")
        field = {"ledger": "row_id", "payments": "doc_id", "invoices": "doc_id"}.get(kind, kind + "_id")
        mapping["references"][namespace] = f"{kind}/{display[field][alias]}"
    for field, aliases in mapping["raw_ids"].items():
        mapping["raw_ids"][field] = {old: display[field][alias] for old, alias in aliases.items()}
    return mapping


def renamed_snapshot(parent: Path, target: Path, seed: int) -> dict:
    """Reuse the release transform, including the linked clean book and transition."""
    private = parent / "private"
    output = target / "private"
    mapping = _rename_public(parent / "public", target / "public", seed)
    output.mkdir()
    truth = transform_truth(read_json(private / "truth.json"), mapping)
    write_json(output / "truth.json", truth)

    clean = private / "source_book"
    maps = mapping["raw_ids"]
    units = _remapped_units(clean, maps)
    field_by_table = {"invoices": "doc_id", "payments": "doc_id", "ledger": "row_id"}
    transformed_clean = output / "source_book"
    _write_units(clean, transformed_clean, units)
    source_manifest = read_json(transformed_clean / "manifest.json")
    source_manifest["episode_id"] = _episode_alias(source_manifest["episode_id"], seed)
    write_json(transformed_clean / "manifest.json", source_manifest)

    transition = read_json(private / "source_transition.json")
    transition["source_episode_id"] = source_manifest["episode_id"]
    transition["target_episode_id"] = truth["episode_id"]
    final_units = _read_units(target / "public")
    # The release transform also permutes the lists inside payment indexes.
    final_payments = {doc["doc_id"]: doc for doc in map(json.loads, final_units["payments"])}
    original_payments = {doc["doc_id"]: doc for doc in map(json.loads, units["payments"])}
    changes = []
    for change in transition["changes"]:
        table = change["table"]
        before, after = (_rename_record(change[key], maps) for key in ("before", "after"))
        key = (after or before)[field_by_table[table]]
        if table == "payments":
            before, after = original_payments.get(key), final_payments.get(key)
        changes.append({**change, "id": key, "before": before, "after": after})
    # Keep permutations inside unchanged payment indexes identical on both sides.
    changed_payments = {c["id"] for c in changes if c["table"] == "payments"}
    shared = (original_payments.keys() & final_payments.keys()) - changed_payments
    if shared:
        units["payments"] = [json.dumps(final_payments[key] if key in shared else doc, sort_keys=True)
                             for key, doc in original_payments.items()]
        # Write only the projected data, preserving the new source identity.
        (transformed_clean / "payments.jsonl").write_text("\n".join(units["payments"]) + "\n",
                                                         encoding="utf-8", newline="\n")

    transition["changes"] = changes
    transition_path = output / "source_transition.json"
    write_json(transition_path, transition)
    provenance = read_json(private / "provenance.json")
    provenance["episode_id"] = truth["episode_id"]
    provenance["parent_events"] = provenance.pop("events")
    provenance["transform"] = {"version": mapping["version"], "seed": seed,
                               "parent_episode_id": mapping["old_episode_id"]}
    reference_map = mapping["references"]
    for mutation in provenance["mutations"]:
        mutation["affected_ids"] = [reference_map[key] for key in mutation["affected_ids"]]
    provenance["source_book"].update(episode_id=source_manifest["episode_id"],
                                     files_sha256=file_hashes(transformed_clean),
                                     target_files_sha256=file_hashes(target / "public"),
                                     transition_sha256=sha256(transition_path.read_bytes()).hexdigest())
    write_json(output / "provenance.json", provenance)
    return mapping


def boundary_variants(parent: Path, seed: int, config: dict, output: Path) -> list[dict]:
    """Use the supported boundary scenario with a fixed 50,000 / 30,000 split."""
    if (config.get("config_version") != "1.1" or config.get("settlement_timing") != "after"
            or config.get("base_currency") != "KZT" or config.get("currencies") != ["KZT"]
            or config.get("issue_counts") != {"payment_allocation": 1}
            or config.get("clean_distractors") != ["next_day_settlement"]
            or config.get("interaction") is not None):
        raise ValueError("The paired controls require the standalone KZT boundary scenario")
    original = read_json(parent / "private/truth.json")["issues"]
    if len(original) != 1 or original[0]["type"] != "payment_allocation":
        raise ValueError("Boundary baseline must contain one allocation defect")
    affected = original[0]["affected_ids"]
    variants = []
    for kind, reason, changes, expected in (
        ("id_order", "IDs and order change while the remaining balance stays 20,000 KZT", {}, (2_000_000, "KZT")),
        ("lawful_refund", "A genuine independent deposit refund leaves the designated balance unchanged",
         {"clean_distractors": ["next_day_settlement", "refund"]}, (2_000_000, "KZT")),
        ("fixed", "The second tranche is posted before the exclusive deadline and the finding disappears",
         {"settlement_timing": "before", "issue_counts": {"payment_allocation": 0}}, None),
        ("currency", "The designated contract is denominated in JPY with exponent zero, so the 20,000-unit balance changes currency and minor amount",
         {"base_currency": "JPY", "currencies": ["JPY"]}, (20_000, "JPY")),
    ):
        snapshot = output / kind
        expected_ids = affected
        variant_config = {**config, **changes}
        if kind == "id_order":
            mapping = renamed_snapshot(parent, snapshot, seed + 1)
            expected_ids = [mapping["references"][key] for key in affected]
        else:
            generate_episode(seed, variant_config, snapshot)
        expected_items = [] if expected is None else [{
            "type": "payment_allocation", "affected_ids": sorted(expected_ids),
            "claimed_amount_minor": expected[0], "currency": expected[1],
        }]
        variants.append({"kind": kind, "reason": reason, "expected": expected_items,
                         "snapshot": snapshot, "config": variant_config})
    return variants


def answers_match(expected, findings):
    def signature(item):
        return (item["type"], tuple(sorted(item["affected_ids"])), item["claimed_amount_minor"], item["currency"])
    return sorted(map(signature, expected)) == sorted(map(signature, findings))
