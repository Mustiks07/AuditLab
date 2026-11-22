"""Versioned generator options for small, controlled financial histories."""

from dataclasses import dataclass
from datetime import timedelta

from .core import random_stream
from .scenarios import add_configured_cutoff

AMOUNT_RANGE = (100, 10_000_000_000)
ALLOCATION_FIELDS = {"kind", "amount_minor", "applied_bps", "tranches", "settlement_offset_minutes"}
CUTOFF_FIELDS = {"kind", "amount_minor", "issued_before_minutes", "recognition_offset_minutes", "issued_next_period"}


def _integer(options, key, limits, default=None):
    value = options.get(key, default)
    low, high = limits
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"invalid_financial_case: {key} must be an integer from {low} to {high}")
    return value


def _split(amount, tranches):
    each, remainder = divmod(amount, tranches)
    return [each + int(index < remainder) for index in range(tranches)]


@dataclass(frozen=True)
class AllocationCase:
    amount_minor: int
    applied_bps: int
    tranches: int
    settlement_offset_minutes: int | None
    issue_type = "payment_allocation"

    @property
    def applied_minor(self):
        return self.amount_minor * self.applied_bps // 10_000

    def build(self, company, *, enabled):
        """Create a matched source, then schedule the requested alteration."""
        _, day, hour, minute = company.case_parameters("financial_allocation", 100)
        receipt_at = company.period_start + timedelta(days=day, hours=hour, minutes=minute)
        entity = company.id("entity")
        currency = company.base_currency
        invoice = company.invoice(entity, receipt_at - timedelta(days=1), currency, self.amount_minor)
        invoice_id = invoice["invoice_id"]
        company.post(receipt_at - timedelta(days=1), currency, self.amount_minor,
                     "AR", "REVENUE", entity, invoice_id=invoice_id)
        payment, _ = company.payment(entity, receipt_at, currency, self.amount_minor, invoice_id)
        payment_id = payment["payment_id"]
        affected = [f"invoice/{invoice_id}", f"payment/{payment_id}"]

        boundary = self.settlement_offset_minutes is not None
        remainder = self.amount_minor - self.applied_minor
        regular_amount = self.applied_minor if boundary else self.amount_minor
        schedule = [
            (receipt_at + timedelta(minutes=(index + 1) * (60 + minute)), amount)
            for index, amount in enumerate(_split(regular_amount, self.tranches))
        ]
        if boundary:
            just_before = company.period_end - timedelta(minutes=max(1, self.settlement_offset_minutes))
            schedule.append((just_before, remainder))
        postings = [
            company.post(at, currency, amount, "UNAPPLIED", "AR", entity,
                         invoice_id=invoice_id, payment_id=payment_id)
            for at, amount in schedule
        ]
        company.events.append({
            "kind": "designated_payment", "affected_ids": affected, "currency": currency,
            "amount_minor": self.amount_minor, "receipt_at": receipt_at.isoformat(),
            "scheduled_tranches_minor": [amount for _, amount in schedule],
            "settled_at": schedule[-1][0].isoformat(),
        })
        if not enabled:
            return

        if company.allocation_mode == "omitted":
            for posting in postings:
                company.defer_posting_omission(posting)
            actual = []
            effect = "The designated payment was never applied."
        elif boundary:
            crossed = company.period_end + timedelta(minutes=self.settlement_offset_minutes)
            company.defer_posting_changes(postings[-1], timestamp=crossed)
            actual = [*schedule[:-1], (crossed, remainder)]
            effect = "The final tranche posted outside the exclusive reporting period."
        else:
            parts = _split(self.applied_minor, self.tranches)
            actual = [(at, part) for (at, _), part in zip(schedule, parts, strict=True)]
            for (at, part), posting in zip(actual, postings, strict=True):
                if part != posting[0]["debit_minor"]:
                    company.defer_posting_changes(posting, timestamp=at, amount=part)
            effect = "The settlement posted less than the designated payment."
        applied = sum(amount for at, amount in actual if at < company.period_end)
        company.mutations.append({
            "type": self.issue_type, "affected_ids": affected,
            "before": {"settled_at": schedule[-1][0].isoformat(), "applied_minor": self.amount_minor},
            "after": {"settled_at": actual[-1][0].isoformat() if actual else None,
                      "applied_minor": applied, "remaining_minor": self.amount_minor - applied},
            "effect": effect,
        })


@dataclass(frozen=True)
class CutoffCase:
    amount_minor: int
    issued_before_minutes: int
    recognition_offset_minutes: int
    issued_next_period: bool
    issue_type = "period_cutoff"

    def build(self, company, *, enabled):
        add_configured_cutoff(
            company, enabled=enabled, amount_minor=self.amount_minor, issued_before_minutes=self.issued_before_minutes,
            recognition_offset_minutes=self.recognition_offset_minutes, issued_next_period=self.issued_next_period,
        )


def parse_financial_case(options, allocation_mode, seed):
    if not isinstance(options, dict):
        raise ValueError("invalid_financial_case: expected an object")
    kind = options.get("kind")
    if kind == "allocation" and not options.keys() - ALLOCATION_FIELDS:
        defaults = random_stream(seed, "financial-case-defaults/allocation")
        offset = None
        if "settlement_offset_minutes" in options:
            if allocation_mode == "omitted":
                raise ValueError("incompatible_allocation_mode: omission cannot also delay a settlement")
            offset = _integer(options, "settlement_offset_minutes", (0, 1_440))
        case = AllocationCase(
            _integer(options, "amount_minor", AMOUNT_RANGE),
            _integer(options, "applied_bps", (100, 9_900), defaults.randrange(1_700, 8_701)),
            _integer(options, "tranches", (1, 3), defaults.randint(1, 3)), offset,
        )
        if allocation_mode != "omitted" and case.applied_minor < case.tranches:
            raise ValueError("invalid_financial_case: each applied tranche must contain at least one minor unit")
        return case
    if kind == "invoice_cutoff" and not options.keys() - CUTOFF_FIELDS:
        defaults = random_stream(seed, "financial-case-defaults/invoice-cutoff")
        next_period = options.get("issued_next_period", False)
        if type(next_period) is not bool:
            raise ValueError("invalid_financial_case: issued_next_period must be a boolean")
        return CutoffCase(
            _integer(options, "amount_minor", AMOUNT_RANGE),
            _integer(options, "issued_before_minutes", (1, 1_440), defaults.randint(1, 1_440)),
            _integer(options, "recognition_offset_minutes", (0, 1_440), defaults.randrange(0, 1_441)),
            next_period,
        )
    raise ValueError("invalid_financial_case: unsupported case kind or options")
