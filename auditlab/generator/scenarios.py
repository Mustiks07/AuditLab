"""Source documents, balanced postings and recorded mutations for audit cases."""

from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Context, Decimal, localcontext


def _recognize(b, invoice, posted_at, *, after=None):
    return b.post(
        posted_at, invoice["currency"], invoice["total_minor"], "AR", "REVENUE",
        invoice["entity_id"], invoice_id=invoice["invoice_id"], after=after,
    )


def add_duplicate(b, *, enabled=True):
    """Archive two copies of one commercial invoice and optionally post both."""
    entity_id = b.id("entity")
    units, day, hour, minute = b.case_parameters("duplicate", 12_000)
    issued_at = b.period_start + timedelta(days=day, hours=hour, minutes=minute)
    currency = b.base_currency
    total_minor = units * 10 ** b.exponents[currency]
    original = b.invoice(entity_id, issued_at, currency, total_minor)
    archive_copy = b.invoice(
        entity_id, issued_at, currency, total_minor, invoice_id=original["invoice_id"],
    )
    first, closing_row = _recognize(b, original, issued_at)
    affected = [f"invoice/{original['invoice_id']}"]
    records = [f"invoices/{doc['doc_id']}" for doc in (original, archive_copy)]
    b.events.append({"kind": "invoice_archive", "affected_ids": affected, "documents": records})
    if enabled:
        extra, _ = _recognize(
            b, archive_copy, issued_at, after=closing_row if b.config_version == "1.1" else None,
        )
        evidence = [*records, f"ledger/{first['row_id']}", f"ledger/{extra['row_id']}"]
        b.issue("duplicate_invoice", affected, extra["debit_minor"], currency, evidence, deferred=True)
        b.mutations.append({
            "type": "duplicate_invoice", "affected_ids": affected,
            "before": {"journals": [first["journal_id"]], "recognized_minor": first["debit_minor"]},
            "after": {
                "journals": [first["journal_id"], extra["journal_id"]],
                "recognized_minor": first["debit_minor"] + extra["debit_minor"],
            },
            "effect": "The archive copy created a second recognition of one receivable.",
            "evidence_ids": evidence,
        })


def _boundary_invoice(b, currency, total_minor, *, cutoff_enabled):
    _, _, hour, minute = b.case_parameters("boundary", 100)
    distance = 1 if b.config_version == "1.0" else hour * 60 + minute + 1
    issued_at = b.period_end - timedelta(minutes=distance)
    posted_at = b.period_end + timedelta(minutes=distance) if cutoff_enabled else issued_at
    invoice = b.invoice(b.id("entity"), issued_at, currency, total_minor)
    deferred = b.config_version == "1.1" and cutoff_enabled
    posting = _recognize(b, invoice, issued_at if deferred else posted_at)
    debit, _ = posting
    if deferred:
        b.defer_posting_changes(posting, timestamp=posted_at)
    affected = [f"invoice/{invoice['invoice_id']}"]
    source = f"invoices/{invoice['doc_id']}"
    b.events.append({
        "kind": "invoice_issued", "affected_ids": affected, "source": source,
        "issued_at": issued_at.isoformat(), "total_minor": total_minor, "currency": currency,
    })
    if not b.period_start <= posted_at < b.period_end:
        evidence = [source, f"ledger/{debit['row_id']}", "policy/period"]
        b.issue("period_cutoff", affected, invoice["total_minor"], currency, evidence, deferred=True)
        b.mutations.append({
            "type": "period_cutoff", "affected_ids": affected,
            "before": {"timestamp": issued_at.isoformat()},
            "after": {"timestamp": posted_at.isoformat(), "journal_id": debit["journal_id"]},
            "effect": "The invoice was recognized in the following reporting period.",
            "evidence_ids": evidence,
        })
    return invoice, posted_at


def add_cutoff(b, *, enabled=True):
    """Recognize a base-currency invoice on either side of period_end."""
    units, *_ = b.case_parameters("cutoff", 24_000)
    amount = units * 10 ** b.exponents[b.base_currency]
    _boundary_invoice(b, b.base_currency, amount, cutoff_enabled=enabled)


def _foreign_currency(b, *, prefer_usd=False):
    if prefer_usd and "USD" in b.exponents and b.base_currency != "USD":
        return "USD"
    if b.money_currency == b.base_currency:
        raise ValueError("foreign_currency_required: configure a currency other than the base")
    return b.money_currency


def _rates(b, currency, expected_date):
    if b.base_currency == "KZT":
        quotes = {
            "USD": ("505", "500", "510"),
            "JPY": ("2.555", "2.505", "2.605"),
            "KWD": ("1605.5", "1600.5", "1610.5"),
        }
        previous_rate, expected_rate, alternate_rate = quotes.get(currency, ("72.695", "72.195", "73.195"))
    else:
        previous_rate, expected_rate, alternate_rate = "1.15", "1.05", "1.25"
    previous_date = expected_date - timedelta(days=1)
    alternate_date = expected_date + timedelta(days=1)
    if b.config_version == "1.1":
        # A single rate for a currency/day, even when independently varied events overlap dates.
        step = Decimal({"USD": ".25", "JPY": ".005", "KWD": ".5"}.get(currency, ".01"))
        base = Decimal(expected_rate)
        def daily(day):
            return format(base + ((day - date(2000, 1, 1)).days % 31) * step, "f")
        previous_rate, expected_rate, alternate_rate = map(daily, (previous_date, expected_date, alternate_date))
    for day, rate in ((previous_date, previous_rate), (expected_date, expected_rate),
                      (alternate_date, alternate_rate)):
        record = {
            "date": day.isoformat(), "source_currency": currency,
            "target_currency": b.base_currency, "rate": rate,
        }
        if record not in b.fx:
            b.fx.append(record)
    return expected_rate, alternate_date, alternate_rate


def _convert(b, amount_minor, currency, rate):
    exponent_change = b.exponents[b.base_currency] - b.exponents[currency]
    with localcontext(Context(prec=40, rounding=ROUND_HALF_UP)):
        target_minor = (Decimal(amount_minor) * Decimal(rate)).scaleb(exponent_change)
        return int(target_minor.to_integral_value())


def _value(b, invoice, posted_at, *, payment=None, fx_enabled):
    if payment is None:
        currency, amount = invoice["currency"], invoice["total_minor"]
        source_at = invoice["issued_at"]
        source = f"invoices/{invoice['doc_id']}"
        affected = [f"invoice/{invoice['invoice_id']}"]
        payment_id = None
    else:
        currency, amount = payment["currency"], payment["amount_minor"]
        source_at = payment["timestamp"]
        source = f"payments/{payment['doc_id']}"
        payment_id = payment["payment_id"]
        affected = [f"payment/{payment_id}"]

    source_at = datetime.fromisoformat(source_at)
    expected_date = source_at.astimezone(b.zone).date()
    correct_rate, alternate_date, alternate_rate = _rates(b, currency, expected_date)
    actual_date, actual_rate = (alternate_date, alternate_rate) if fx_enabled else (expected_date, correct_rate)
    expected = _convert(b, amount, currency, correct_rate)
    actual = _convert(b, amount, currency, actual_rate)
    deferred = b.config_version == "1.1"
    posting = b.post(
        source_at if deferred else posted_at, b.base_currency, expected if deferred else actual,
        "FX_VALUATION", "FX_OFFSET", invoice["entity_id"],
        invoice_id=invoice["invoice_id"], payment_id=payment_id,
    )
    debit, _ = posting
    if deferred and (posted_at != source_at or actual != expected):
        b.defer_posting_changes(posting, timestamp=posted_at, amount=actual)
    b.events.append({
        "kind": "reporting_valuation", "affected_ids": affected, "source": source,
        "source_currency": currency, "source_minor": amount, "local_date": expected_date.isoformat(),
        "base_currency": b.base_currency,
    })
    difference = abs((actual if deferred else debit["debit_minor"]) - expected)
    if difference:
        evidence = [source, f"fx/{expected_date}/{currency}/{b.base_currency}",
                    f"ledger/{debit['row_id']}", "policy/fx_date"]
        b.issue("fx_conversion", affected, difference, b.base_currency, evidence, deferred=True)
        b.mutations.append({
            "type": "fx_conversion", "affected_ids": affected,
            "before": {"quote_date": expected_date.isoformat(), "rate": correct_rate, "amount_minor": expected},
            "after": {"quote_date": actual_date.isoformat(), "rate": actual_rate, "amount_minor": actual},
            "effect": "The reporting valuation used the following day's exchange rate.",
            "evidence_ids": evidence,
        })


def add_fx(b, *, enabled=True):
    """Value a fully settled foreign payment using its company-local date."""
    currency = _foreign_currency(b)
    units, day, hour, minute = b.case_parameters("fx", 100, day=13, hour=0, minute=1)
    amount = units * 10 ** b.exponents[currency]
    if currency == "JPY":
        amount += 1
    elif currency == "KWD":
        amount += 10
    received_at = (b.period_start + timedelta(days=day, hours=hour, minutes=minute)).astimezone(UTC)
    invoice, payment, *_ = b.allocation(
        b.id("entity"), currency, amount, amount * 3 // 5, received_at,
        received_at + timedelta(minutes=1), received_at + timedelta(minutes=2),
    )
    _value(b, invoice, received_at, payment=payment, fx_enabled=enabled)


def add_allocation_fx(b, *, allocation_enabled, fx_enabled):
    """Use one payment for independent allocation and FX controls."""
    currency = _foreign_currency(b, prefer_usd=True)
    exponent = b.exponents[currency]
    units, day, hour, minute = b.case_parameters("allocation_fx", 50, day=13, hour=16)
    received_at = b.period_start + timedelta(days=day, hours=hour, minutes=minute)
    last_at = b.period_end + timedelta(minutes=1 if allocation_enabled else -1)
    invoice, payment, *_ = b.allocation(
        b.id("entity"), currency, units * 10 ** exponent, units * 10 ** exponent * 3 // 5,
        received_at, received_at + timedelta(minutes=1), last_at,
    )
    _value(b, invoice, received_at, payment=payment, fx_enabled=fx_enabled)


def add_cutoff_fx(b, *, cutoff_enabled, fx_enabled):
    """Use one invoice for independent period and FX controls."""
    currency = _foreign_currency(b, prefer_usd=True)
    units, *_ = b.case_parameters("cutoff_fx", 100)
    invoice, posted_at = _boundary_invoice(
        b, currency, units * 10 ** b.exponents[currency], cutoff_enabled=cutoff_enabled,
    )
    _value(b, invoice, posted_at, fx_enabled=fx_enabled)
