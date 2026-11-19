"""Evidence for simple checks over records observed through the public tools."""

from collections import defaultdict
from datetime import datetime
from fractions import Fraction
from importlib import resources
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from auditlab.contracts import MAX_EVIDENCE, Finding

Record = dict[str, Any]


def _allocation_findings(
    payments: list[Record], ledger: dict[str, Record], deadline: datetime
) -> Iterator[Finding]:
    for payment in payments:
        index = payment.get("settlement_index")
        if index is None or len(payment["invoice_refs"]) != 1:
            continue
        if datetime.fromisoformat(index["before"]) != deadline:
            continue
        row_ids = index["unapplied_row_ids"]
        if len(row_ids) > MAX_EVIDENCE - 2 or not all(row_id in ledger for row_id in row_ids):
            continue
        movements = [ledger[row_id] for row_id in row_ids]
        if any(row["account"] != "UNAPPLIED" or row["payment_id"] != payment["payment_id"]
               or row["currency"] != payment["currency"]
               or datetime.fromisoformat(row["timestamp"]) >= deadline for row in movements):
            continue
        balance = sum(row["credit_minor"] - row["debit_minor"] for row in movements)
        if balance > 0:
            yield {
                "type": "payment_allocation",
                "affected_ids": [f"payment/{payment['payment_id']}",
                                 f"invoice/{payment['invoice_refs'][0]}"],
                "claimed_amount_minor": balance,
                "currency": payment["currency"],
                "evidence_ids": [f"payments/{payment['doc_id']}",
                                 *(f"ledger/{row_id}" for row_id in row_ids), "policy/allocation"],
                "explanation": "Credits minus debits in the complete settlement index leave a positive balance.",
            }


def _invoice_findings(
    invoices: list[Record], ledger: list[Record], start: datetime, end: datetime, check_period: bool
) -> Iterator[Finding]:
    documents = defaultdict(list)
    recognitions = defaultdict(dict)
    for invoice in invoices:
        documents[invoice["entity_id"], invoice["invoice_id"]].append(invoice)
    for row in ledger:
        if row["account"] == "AR" and row["payment_id"] is None and row["debit_minor"] > 0:
            key = row["entity_id"], row["invoice_id"]
            recognitions[key].setdefault(row["journal_id"], row)

    for key, copies in documents.items():
        invoice = copies[0]
        entries = [row for row in recognitions[key].values() if row["currency"] == invoice["currency"]]
        if not entries:
            continue
        if len(copies) >= 2 and 2 <= len(entries) <= MAX_EVIDENCE - 2:
            excess = sum(row["debit_minor"] for row in entries) - invoice["total_minor"]
            if excess > 0:
                yield {
                    "type": "duplicate_invoice",
                    "affected_ids": [f"invoice/{invoice['invoice_id']}"],
                    "claimed_amount_minor": excess,
                    "currency": invoice["currency"],
                    "evidence_ids": [*(f"invoices/{doc['doc_id']}" for doc in copies[:2]),
                                     *(f"ledger/{row['row_id']}" for row in entries)],
                    "explanation": "Documents for one commercial invoice have repeated accounts receivable entries.",
                }

        event_in_period = start <= datetime.fromisoformat(invoice["issued_at"]) < end
        if check_period and all(
            (start <= datetime.fromisoformat(row["timestamp"]) < end) != event_in_period for row in entries
        ):
            yield {
                "type": "period_cutoff",
                "affected_ids": [f"invoice/{invoice['invoice_id']}"],
                "claimed_amount_minor": invoice["total_minor"],
                "currency": invoice["currency"],
                "evidence_ids": [f"invoices/{invoice['doc_id']}", f"ledger/{entries[0]['row_id']}",
                                 "policy/period"],
                "explanation": "The invoice issue date and the accounting recognition fall on opposite sides of the reporting boundary.",
            }


def _fx_findings(manifest: Record, records: dict[str, list[Record]], start: datetime) -> Iterator[Finding]:
    timezone = start.tzinfo
    if manifest["timezone"] != "Asia/Almaty" or (start.year, start.month) < (2024, 4):
        parts = manifest["timezone"].split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("invalid_timezone")
        try:
            with resources.files("tzdata.zoneinfo").joinpath(*parts).open("rb") as source:
                timezone = ZoneInfo.from_file(source, key=manifest["timezone"])
        except (ModuleNotFoundError, OSError, ValueError):
            raise ValueError("timezone_data_unavailable: install pinned tzdata and use a supported IANA zone") from None

    exponents = manifest["currency_exponents"]
    rates = {(row["date"], row["source_currency"], row["target_currency"]): row["rate"]
             for row in records.get("fx", [])}
    invoices = {document["invoice_id"]: document for document in records.get("invoices", [])}
    payments = {document["payment_id"]: document for document in records.get("payments", [])}
    for row in records.get("ledger", []):
        if row["account"] != "FX_VALUATION" or row["currency"] != manifest["base_currency"]:
            continue
        if row["payment_id"] is not None:
            source = payments.get(row["payment_id"])
            id_key, evidence_table, amount_key, timestamp_key = "payment_id", "payments", "amount_minor", "timestamp"
        else:
            source = invoices.get(row["invoice_id"])
            id_key, evidence_table, amount_key, timestamp_key = "invoice_id", "invoices", "total_minor", "issued_at"
        if source is None:
            continue
        date = datetime.fromisoformat(source[timestamp_key]).astimezone(timezone).date().isoformat()
        source_currency = source["currency"]
        target_currency = row["currency"]
        rate_key = date, source_currency, target_currency
        if rate_key not in rates:
            continue
        exact = (abs(source[amount_key]) * Fraction(rates[rate_key]) * 10 ** exponents[target_currency]
                 / 10 ** exponents[source_currency])
        whole, rest = divmod(exact.numerator, exact.denominator)
        expected = whole + (2 * rest >= exact.denominator)
        difference = abs(expected - row["debit_minor"])
        if difference:
            yield {
                "type": "fx_conversion",
                "affected_ids": [f"{id_key.removesuffix('_id')}/{source[id_key]}"],
                "claimed_amount_minor": difference,
                "currency": target_currency,
                "evidence_ids": [f"{evidence_table}/{source['doc_id']}", f"fx/{'/'.join(rate_key)}",
                                 f"ledger/{row['row_id']}", "policy/fx_date"],
                "explanation": "Converting the nominal amount at the local source date rate gives a different valuation.",
            }


def reconcile(
    manifest: Record, records: dict[str, list[Record]], complete: set[str]
) -> Iterator[Finding]:
    start = datetime.fromisoformat(manifest["period_start"])
    end = datetime.fromisoformat(manifest["period_end"])
    rules = {entry["id"] for entry in records.get("policy", [])}
    ledger = records.get("ledger", [])
    if "allocation" in rules:
        yield from _allocation_findings(records.get("payments", []), {row["row_id"]: row for row in ledger}, end)
    if "ledger" in complete:
        yield from _invoice_findings(records.get("invoices", []), ledger, start, end, "period" in rules)
    if "fx_date" in rules and manifest.get("rounding") == "ROUND_HALF_UP":
        yield from _fx_findings(manifest, records, start)
