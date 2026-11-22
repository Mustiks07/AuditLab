"""Recompute discrepancies from the final documents and accounting movements."""

from collections import Counter, defaultdict
from datetime import datetime
from fractions import Fraction


def _instant(record, key="timestamp"):
    return datetime.fromisoformat(record[key])


def _signature(kind, affected, amount, currency):
    return kind, tuple(sorted(affected)), amount, currency


def _invoice_findings(company, documents, rows):
    findings = []
    for key, copies in documents.items():
        invoice = copies[0]
        currency, amount = invoice["currency"], invoice["total_minor"]
        expected_fields = {name: value for name, value in invoice.items() if name != "doc_id"}
        if any({name: value for name, value in doc.items() if name != "doc_id"} != expected_fields for doc in copies):
            raise RuntimeError("inconsistent_invoice_copies")
        postings = [row for row in rows[key] if row["account"] == "AR" and row["debit_minor"]]
        if not postings or len(postings) > len(copies) or any(
            row["entity_id"] != invoice["entity_id"] or row["currency"] != currency
            or row["debit_minor"] != amount or row["payment_id"] is not None for row in postings
        ):
            raise RuntimeError("inconsistent_invoice_recognition")
        applied = sum(row["credit_minor"] for row in rows[key] if row["account"] == "AR")
        if applied > amount:
            raise RuntimeError("overapplied_invoice")
        affected = [f"invoice/{key}"]
        if len(postings) > 1:
            findings.append(_signature("duplicate_invoice", affected, (len(postings) - 1) * amount, currency))
        issued_in_period = company.period_start <= _instant(invoice, "issued_at") < company.period_end
        for row in postings:
            posted_in_period = company.period_start <= _instant(row) < company.period_end
            if posted_in_period != issued_in_period:
                findings.append(_signature("period_cutoff", affected, amount, currency))
    return findings


def _payment_findings(company, invoices, payments, rows):
    findings = []
    for key, payment in payments.items():
        currency, amount = payment["currency"], payment["amount_minor"]
        native = [row for row in rows[key] if row["account"] in {"CASH", "UNAPPLIED", "AR"}]
        destinations = payment["invoice_refs"]
        invoice = invoices[destinations[0]][0] if destinations else None
        destination = invoice["invoice_id"] if invoice else None
        if len(destinations) > 1 or any(row["currency"] != currency or row["invoice_id"] != destination for row in native):
            raise RuntimeError("inconsistent_payment_posting")
        if invoice and (invoice["currency"] != currency or any(row["entity_id"] != invoice["entity_id"] for row in native)):
            raise RuntimeError("inconsistent_payment_destination")
        cash_received = [row for row in native if row["account"] == "CASH" and row["debit_minor"]]
        movements = [row for row in native if row["account"] == "UNAPPLIED"]
        if (len(cash_received) != 1 or cash_received[0]["debit_minor"] != amount
                or _instant(cash_received[0]) != _instant(payment)
                or sum(row["credit_minor"] for row in movements) != amount
                or sum(row["debit_minor"] for row in movements) > amount):
            raise RuntimeError("inconsistent_payment_amount")
        eligible = [row for row in movements if _instant(row) < company.period_end]
        index = payment["settlement_index"]
        if (_instant(index, "before") != company.period_end
                or Counter(index["unapplied_row_ids"]) != Counter(row["row_id"] for row in eligible)):
            raise RuntimeError("incomplete_settlement_index")
        applied = sum(row["credit_minor"] for row in native
                      if row["account"] == "AR" and _instant(row) < company.period_end)
        remaining = sum(row["credit_minor"] - row["debit_minor"] for row in eligible)
        if invoice and _instant(payment) < company.period_end:
            if applied + remaining != amount:
                raise RuntimeError("unreconciled_designated_payment")
            if remaining:
                findings.append(_signature("payment_allocation", [f"invoice/{destination}", f"payment/{key}"],
                                           remaining, currency))
    return findings


def _fx_findings(company, invoices, payments):
    findings = []
    rates = {(row["date"], row["source_currency"], row["target_currency"]): Fraction(row["rate"])
             for row in company.fx}
    for row in company.ledger:
        if row["account"] != "FX_VALUATION":
            continue
        base = company.base_currency
        copies = invoices.get(row["invoice_id"])
        if not copies:
            raise RuntimeError("invalid_valuation_source")
        invoice = copies[0]
        if row["payment_id"]:
            key = row["payment_id"]
            payment = payments[key]
            if invoice["invoice_id"] not in payment["invoice_refs"]:
                raise RuntimeError("invalid_valuation_source")
            source_time, amount, currency = _instant(payment), payment["amount_minor"], payment["currency"]
            affected = [f"payment/{key}"]
        else:
            source_time, amount, currency = _instant(invoice, "issued_at"), invoice["total_minor"], invoice["currency"]
            affected = [f"invoice/{invoice['invoice_id']}"]
        day = source_time.astimezone(company.zone).date().isoformat()
        rate = rates.get((day, currency, base))
        if (row["entity_id"] != invoice["entity_id"] or row["currency"] != base or row["debit_minor"] <= 0
                or currency == base or rate is None or rate <= 0):
            raise RuntimeError("invalid_valuation_source")
        converted = Fraction(amount * 10 ** company.exponents[base], 10 ** company.exponents[currency]) * rate
        # Positive monetary amounts round exactly at a half minor unit.
        expected = int(converted + Fraction(1, 2))
        difference = abs(row["debit_minor"] - expected)
        if difference:
            findings.append(_signature("fx_conversion", affected, difference, base))
    return findings


def validate_accounting(company):
    if company.uses_source_book:
        _validate_treasury(company)
    invoices = defaultdict(list)
    by_invoice = defaultdict(list)
    by_payment = defaultdict(list)
    for document in company.invoices:
        invoices[document["invoice_id"]].append(document)
    payments = {document["payment_id"]: document for document in company.payments}
    if any(key not in invoices for payment in payments.values() for key in payment["invoice_refs"]):
        raise RuntimeError("unknown_payment_destination")
    for row in company.ledger:
        by_invoice[row["invoice_id"]].append(row)
        by_payment[row["payment_id"]].append(row)
    calculated = Counter(_invoice_findings(company, invoices, by_invoice))
    calculated.update(_payment_findings(company, invoices, payments, by_payment))
    calculated.update(_fx_findings(company, invoices, payments))
    truth = Counter(_signature(issue["type"], issue["affected_ids"], issue["expected_amount_minor"], issue["currency"])
                    for issue in company.issues)
    if calculated != truth:
        raise RuntimeError("accounting_disagrees_with_truth")


def _validate_treasury(company):
    """Internal transfers have no external source document, so validate their event explicitly."""
    journals = defaultdict(list)
    for row in company.ledger:
        journals[row["journal_id"]].append(row)
    events = {e["journal_id"]: e for e in company.events if e["kind"] == "treasury_transfer"}
    reserve_journals = {row["journal_id"] for row in company.ledger if row["account"] == "CASH_RESERVE"}
    if reserve_journals != events.keys():
        raise RuntimeError("unrecorded_treasury_transfer")
    for journal, event in events.items():
        rows = journals[journal]
        if (len(rows) != 2 or {row["account"] for row in rows} != {"CASH", "CASH_RESERVE"}
                or any(row["currency"] != company.base_currency or row["invoice_id"] is not None
                       or row["payment_id"] is not None or row["timestamp"] != event["timestamp"] for row in rows)
                or sum(row["debit_minor"] for row in rows) != event["amount_minor"]
                or sum(row["credit_minor"] for row in rows) != event["amount_minor"]
                or any(row["account"] != event["to_account"] for row in rows if row["debit_minor"])
                or any(row["account"] != event["from_account"] for row in rows if row["credit_minor"])):
            raise RuntimeError("inconsistent_treasury_transfer")
    balances = defaultdict(int)
    for row in sorted(company.ledger, key=lambda r: (_instant(r), r["journal_id"])):
        if row["account"] in {"CASH", "CASH_RESERVE"} and row["currency"] == company.base_currency:
            balances[row["account"]] += row["debit_minor"] - row["credit_minor"]
            if balances[row["account"]] < 0:
                raise RuntimeError("treasury_cash_overdraft")
