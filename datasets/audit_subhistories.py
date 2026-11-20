"""Diagnose complete commercial subhistories shared across public data splits.

This extends the whole-company comparison; it does not equate shared accounting rules
with leakage. Only exact, ID-independent primary-document chains and linked books match.
"""
import argparse
from collections import defaultdict
import hashlib
from itertools import combinations
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .audit_families import PUBLISHED, canonical_history, check_splits

VERSION = "commercial-subhistories-1.0"
VIEWS = ("counterparty", "invoice", "posted_counterparty")


def _digest(value):
    wire = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()


def units(public):
    """Yield complete invoice/payment/refund chains, excluding treasury-only entities.

    Primary families ignore posting defects. Posted counterparties also retain every
    linked journal and side, base currency and rounding. FX quote tables and company
    timezone names are not included in this partial view; the whole-book view retains
    the full quote table. Instants retain the existing period-relative normalization.
    """
    history = canonical_history(public)
    exponents = history["family"]["currency_exponents"]
    for index, (invoices, unattached) in enumerate(history["family"]["counterparties"]):
        for view, values in (("counterparty", [(invoices, unattached)]), ("invoice", invoices)):
            for child, value in enumerate(values):
                invs, payments = value if view == "counterparty" else ([value], [])
                if not (invs or payments):
                    continue
                currencies = {terms[0] for primary, _ in invs for terms in primary}
                currencies |= {terms[0] for _, pays in invs for terms, _ in pays}
                currencies |= {terms[0] for terms, _ in payments}
                payload = {"unit": value, "exponents": {c: exponents[c] for c in sorted(currencies)}}
                yield view, index, child if view == "invoice" else None, payload

    book = history["book"]
    for index, value in enumerate(book["counterparties"]):
        invoices, payments, journals = value
        if not (invoices or payments):
            continue
        currencies = {book["base_currency"]}
        currencies |= {terms[0] for copies, _, _ in invoices for terms in copies}
        currencies |= {terms[0] for _, pays, _ in invoices for terms, _ in pays}
        currencies |= {terms[0] for terms, _ in payments}
        journal_groups = [journals, *(js for _, _, js in invoices),
                          *(js for _, pays, _ in invoices for _, js in pays),
                          *(js for _, js in payments)]
        currencies |= {side[1] for group in journal_groups for journal in group for side in journal}
        payload = {"unit": value, "base_currency": book["base_currency"], "rounding": book["rounding"],
                   "exponents": {c: book["currency_exponents"][c] for c in sorted(currencies)}}
        yield "posted_counterparty", index, None, payload


def check_subhistories(splits):
    index = {view: defaultdict(list) for view in VIEWS}
    payloads = {}
    for split, episodes in splits.items():
        for label, public in episodes.items():
            for view, parent, child, payload in units(public):
                digest = _digest(payload)
                payloads[digest] = payload
                index[view][digest].append({"split": split, "episode": label,
                                           "counterparty_index": parent, "invoice_index": child})
    result = {"version": VERSION, "scope": "Exact complete commercial chains; not arbitrary subgraphs or label leakage",
              "counts": {split: len(episodes) for split, episodes in splits.items()}, "views": {}}
    for view, groups in index.items():
        matches = [{"sha256": digest, "members": members, "canonical": payloads[digest]}
                   for digest, members in sorted(groups.items()) if len({m["split"] for m in members}) > 1]
        pairs = {f"{left}/{right}": sorted({(a["episode"], b["episode"])
                 for match in matches for a in match["members"] for b in match["members"]
                 if a["split"] == left and b["split"] == right}) for left, right in combinations(splits, 2)}
        result["views"][view] = {
            "units_per_split": {split: sum(m["split"] == split for ms in groups.values() for m in ms)
                                for split in splits},
            "shared_families": len(matches), "episode_pairs": pairs, "matches": matches,
        }
    return result


def check_published_and_internal():
    from auditlab.generator import GENERATOR_VERSION, generate_episode
    from auditlab.release.rehearsal import selection_manifest
    selection = selection_manifest()
    splits = {split: {e.name: e / "public" for e in sorted((PUBLISHED / split).iterdir())}
              for split in ("train", "dev")}
    with TemporaryDirectory() as directory:
        splits["internal"] = {}
        for case in selection["episodes"]:
            episode = Path(directory) / case["id"]
            generate_episode(case["seed"], case["config"], episode)
            splits["internal"][case["id"]] = episode / "public"
        return {"generator_version": GENERATOR_VERSION, "selection_version": selection["version"],
                "selection_sha256": _digest(selection), "whole": check_splits(splits),
                "partial": check_subhistories(splits)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-disjoint", action="store_true",
                        help="return 1 for any measured cross-split complete-chain match")
    args = parser.parse_args()
    result = check_published_and_internal()
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return int(args.require_disjoint and any(v["shared_families"] for v in result["partial"]["views"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
