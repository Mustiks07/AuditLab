"""Compare published and internal companies without using their IDs or seeds as evidence."""

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import combinations
from pathlib import Path
from tempfile import TemporaryDirectory


HISTORY_VERSION = "1.0"
PUBLISHED = Path(__file__).with_name("open")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _ordered(values):
    return sorted(values, key=_json)


def canonical_history(public_dir):
    """Return the primary-event family and the posted book for supported simulator snapshots.

    Counterparty, invoice, payment and journal links determine grouping only. Multiplicity
    is preserved, except that identical archive copies describe one primary invoice.
    """
    public = Path(public_dir)
    manifest = json.loads((public / "manifest.json").read_bytes())
    policy = json.loads((public / "policy.json").read_bytes())
    start = datetime.fromisoformat(manifest["period_start"])
    if start.utcoffset() is None:
        raise ValueError("Period start must include an offset")
    start_utc = start.astimezone(UTC)

    def instant(value):
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError("Economic timestamps must include an offset")
        offset = parsed.astimezone(UTC) - start_utc
        return (offset.days * 86_400 + offset.seconds) * 1_000_000 + offset.microseconds

    def docs(table):
        return [json.loads(line) for line in (public / f"{table}.jsonl").read_bytes().splitlines() if line.strip()]

    invoice_terms = defaultdict(list)
    for invoice in docs("invoices"):
        key = invoice["entity_id"], invoice["invoice_id"]
        invoice_terms[key].append((invoice["currency"], invoice["total_minor"], instant(invoice["issued_at"]),
                                  instant(invoice["due_at"]), _ordered(invoice["line_items"])))

    journal_sides = defaultdict(list)
    journal_owners = {}
    with (public / "ledger.csv").open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            key = tuple(None if row[field] == "null" else row[field]
                        for field in ("entity_id", "invoice_id", "payment_id"))
            journal = row["journal_id"]
            if journal_owners.setdefault(journal, key) != key:
                raise ValueError("A journal spans multiple source links; unsupported canonical history")
            journal_sides[journal].append((row["account"], row["currency"], instant(row["timestamp"]),
                                           int(row["debit_minor"]), int(row["credit_minor"])))

    postings = defaultdict(list)
    payment_owners = {}
    for journal, (entity, invoice_id, payment_id) in journal_owners.items():
        if payment_id is not None and payment_owners.setdefault(payment_id, (entity, invoice_id)) != (entity, invoice_id):
            raise ValueError("A payment spans multiple source links; unsupported canonical history")
        postings[entity, invoice_id, payment_id].append(_ordered(journal_sides[journal]))

    family_payments = defaultdict(list)
    book_payments = defaultdict(list)
    used_currencies = {terms[0] for copies in invoice_terms.values() for terms in copies}
    for payment in docs("payments"):
        payment_id = payment["payment_id"]
        if payment_id not in payment_owners:
            raise ValueError("Payment without a linked journal; unsupported canonical history")
        entity, invoice_id = payment_owners.pop(payment_id)
        if payment.get("allocation_instructions") or payment["invoice_refs"] != ([] if invoice_id is None else [invoice_id]):
            raise ValueError("Payment instructions and journal links differ; unsupported canonical history")
        if invoice_id is not None and (entity, invoice_id) not in invoice_terms:
            raise ValueError("Payment links to a missing invoice")
        currency = payment["currency"]
        used_currencies.add(currency)
        terms = (currency, payment["amount_minor"], instant(payment["timestamp"]))
        journals = _ordered(postings.pop((entity, invoice_id, payment_id)))
        refunds = _ordered((side[1], side[2], side[4]) for journal in journals for side in journal
                           if side[0] == "CASH" and side[4] > 0)
        family_payments[entity, invoice_id].append((terms, refunds))
        book_payments[entity, invoice_id].append((terms, journals))
    if payment_owners:
        raise ValueError("Ledger links to a missing payment")

    family_invoices = defaultdict(list)
    book_invoices = defaultdict(list)
    for (entity, invoice_id), copies in invoice_terms.items():
        # Serialize for set equality so ordered copies do not hide separate invoice keys.
        primary = _ordered({_json(terms): terms for terms in copies}.values())
        family_invoices[entity].append((primary, _ordered(family_payments[entity, invoice_id])))
        book_invoices[entity].append((_ordered(copies), _ordered(book_payments[entity, invoice_id]),
                                      _ordered(postings.pop((entity, invoice_id, None), []))))
    if any(invoice_id is not None for _, invoice_id, _ in postings):
        raise ValueError("Ledger links to a missing invoice")

    family_entities = set(family_invoices) | {entity for entity, invoice_id in family_payments if invoice_id is None}
    book_entities = family_entities | {entity for entity, _, _ in postings}
    with (public / "fx.csv").open(encoding="utf-8", newline="") as stream:
        rates = _ordered(((date.fromisoformat(row["date"]) - start.date()).days,
                          row["source_currency"], row["target_currency"], Decimal(row["rate"]).as_integer_ratio())
                         for row in csv.DictReader(stream))
    return {
        "version": HISTORY_VERSION,
        "family": {
            "currency_exponents": {code: manifest["currency_exponents"][code] for code in used_currencies},
            "counterparties": _ordered((_ordered(family_invoices[entity]), _ordered(family_payments[entity, None]))
                                       for entity in family_entities),
        },
        "book": {
            "base_currency": manifest["base_currency"], "currency_exponents": manifest["currency_exponents"],
            "period_end": instant(manifest["period_end"]), "rounding": manifest["rounding"],
            "end_exclusive": policy["period"]["end_exclusive"], "rates": rates,
            "counterparties": _ordered((_ordered(book_invoices[entity]), _ordered(book_payments[entity, None]),
                                        _ordered(postings[entity, None, None])) for entity in book_entities),
        },
    }


def fingerprints(public_dir):
    history = canonical_history(public_dir)
    return {view: hashlib.sha256(_json((history["version"], view, history[view])).encode("utf-8")).hexdigest()
            for view in ("family", "book")}


def check_splits(splits):
    """Return within-split controls and cross-split matches; compare every pair."""
    by_split = {split: {label: fingerprints(public) for label, public in episodes.items()}
                for split, episodes in splits.items()}
    within = {}
    cross = {}
    for view in ("family", "book"):
        within[view] = {split: [[a, b] for a, b in combinations(episodes, 2) if episodes[a][view] == episodes[b][view]]
                        for split, episodes in by_split.items()}
        cross[view] = {
            f"{left}/{right}": [[a, b] for a, fa in by_split[left].items() for b, fb in by_split[right].items()
                               if fa[view] == fb[view]]
            for left, right in combinations(by_split, 2)
        }
    return {"version": HISTORY_VERSION, "counts": {name: len(episodes) for name, episodes in by_split.items()},
            "compared": {f"{a}/{b}": len(by_split[a]) * len(by_split[b]) for a, b in combinations(by_split, 2)},
            "within": within, "cross": cross}


def check_published_and_internal():
    from auditlab.generator import generate_episode
    from auditlab.release.rehearsal import selection_manifest

    splits = {split: {episode.name: episode / "public" for episode in sorted((PUBLISHED / split).iterdir())}
              for split in ("train", "dev")}
    with TemporaryDirectory() as directory:
        internal = {}
        for case in selection_manifest()["episodes"]:
            episode = Path(directory) / case["id"]
            generate_episode(case["seed"], case["config"], episode)
            internal[case["id"]] = episode / "public"
        return check_splits({**splits, "internal": internal})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = check_published_and_internal()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(any(matches for view in result["cross"].values() for matches in view.values()))


if __name__ == "__main__":
    raise SystemExit(main())
