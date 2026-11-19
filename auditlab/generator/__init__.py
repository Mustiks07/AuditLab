"""Deterministic synthetic AuditLab snapshots."""

import csv
import json
import os
import re
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core import FX_FIELDS, LEDGER_FIELDS, Company
from .scenarios import add_allocation_fx, add_cutoff, add_cutoff_fx, add_duplicate, add_fx


GENERATOR_VERSION = "1.0"
SCHEMA_VERSION = "1.0"
CURRENCY_EXPONENTS = {"KZT": 2, "USD": 2, "EUR": 2, "JPY": 0, "KWD": 3}
ISSUE_TYPES = ("duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff")
INTERACTIONS = ("allocation_fx_same_payment", "cutoff_fx_same_invoice")
DISTRACTORS = ("split_payment", "equal_amount_different_supplier", "refund", "archive_copy")
TIMINGS = {"before": -1, "at": 0, "after": 1}
CONFIG_FIELDS = {
    "config_version", "company_size", "base_currency", "currencies", "timezone", "target_period",
    "issue_counts", "interaction", "clean_distractors", "settlement_timing", "snapshot_nonce",
}


def _bounded_int(value, low, high):
    return type(value) is int and low <= value <= high


def _period(value, zone_name):
    if not isinstance(value, str) or not re.fullmatch(r"20[0-9]{2}-(0[1-9]|1[0-2])", value):
        raise ValueError("invalid_target_period: expected YYYY-MM between 2000 and 2099")
    year, month = map(int, value.split("-"))
    if not isinstance(zone_name, str) or not zone_name:
        raise ValueError("invalid_timezone: expected an IANA zone name")
    # Fixed rules cover the default experiment on systems without an IANA database.
    if zone_name == "Asia/Almaty" and (year, month) >= (2024, 4):
        zone = timezone(timedelta(hours=5), name=zone_name)
    elif zone_name == "UTC":
        zone = timezone.utc
    else:
        try:
            zone = ZoneInfo(zone_name)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("invalid_timezone: IANA zone unavailable") from None
    return datetime(year, month, 1, tzinfo=zone), datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=zone)


def _configuration(seed, config):
    if type(seed) is not int:
        raise ValueError("invalid_seed: expected an integer")
    if not isinstance(config, dict) or config.keys() - CONFIG_FIELDS:
        raise ValueError("invalid_config: unsupported configuration fields")
    if config.get("config_version", "1.0") != "1.0":
        raise ValueError("unsupported_config_version")
    base = config.get("base_currency", "KZT")
    currencies = config.get("currencies", [base, *(["USD"] if base != "USD" else ["KZT"])])
    if (not isinstance(currencies, list) or not currencies
            or any(not isinstance(code, str) or code not in CURRENCY_EXPONENTS for code in currencies)
            or len(currencies) != len(set(currencies)) or base not in currencies):
        raise ValueError("invalid_currencies: use unique supported currencies including the base currency")
    counts = config.get("issue_counts", {})
    if (not isinstance(counts, dict) or counts.keys() - set(ISSUE_TYPES)
            or any(not _bounded_int(value, 0, 5) for value in counts.values())):
        raise ValueError("invalid_issue_counts: each supported issue count must be an integer from 0 to 5")
    counts = {key: counts.get(key, 0) for key in ISSUE_TYPES}
    interaction = config.get("interaction")
    if interaction is not None and interaction not in INTERACTIONS:
        raise ValueError("unknown_interaction")
    if (interaction or counts["fx_conversion"]) and len(currencies) < 2:
        raise ValueError("foreign_currency_required")
    timing = config.get("settlement_timing")
    if timing is not None:
        if not isinstance(timing, str) or timing not in TIMINGS:
            raise ValueError("invalid_settlement_timing: expected before, at or after")
        if "payment_allocation" not in config.get("issue_counts", {}):
            counts["payment_allocation"] = int(timing != "before")
        elif timing != "before" and counts["payment_allocation"] < 1:
            raise ValueError("contradictory_settlement_timing")
    distractors = config.get("clean_distractors", list(DISTRACTORS))
    if (not isinstance(distractors, list) or any(item not in DISTRACTORS for item in distractors)
            or len(distractors) != len(set(distractors))):
        raise ValueError("invalid_clean_distractors")
    size = config.get("company_size", {})
    if (not isinstance(size, dict) or size.keys() - {"ledger_rows_target", "documents_target"}
            or not _bounded_int(size.get("ledger_rows_target", 0), 0, 2_000)
            or not _bounded_int(size.get("documents_target", 0), 0, 600)):
        raise ValueError("invalid_company_size: use integer targets 0–2000 ledger rows and 0–600 documents")
    nonce = config.get("snapshot_nonce", "")
    if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{0,64}", nonce):
        raise ValueError("invalid_snapshot_nonce")
    zone_name = config.get("timezone", "Asia/Almaty")
    period_start, period_end = _period(config.get("target_period", "2026-11"), zone_name)
    exponents = {code: CURRENCY_EXPONENTS[code] for code in currencies}
    company = Company(seed, period_start, period_end, period_start.tzinfo, base, exponents)
    return company, counts, timing, interaction, distractors, size, nonce


def _add_paid_invoices(company, amount, issued_at, *, split=False):
    entity = company.id("entity")
    currency = company.base_currency
    invoice = company.invoice(entity, issued_at, currency, amount)
    invoice_id = invoice["invoice_id"]
    company.post(issued_at, currency, amount, "AR", "REVENUE", entity, invoice_id=invoice_id)
    received = issued_at + timedelta(days=2)
    parts = (amount * 2 // 5, amount - amount * 2 // 5) if split else (amount,)
    for offset, part in enumerate(parts):
        timestamp = received + timedelta(days=offset)
        payment, _ = company.payment(entity, timestamp, currency, part, invoice_id)
        company.post(timestamp + timedelta(minutes=1), currency, part, "UNAPPLIED", "AR", entity,
                     invoice_id=invoice_id, payment_id=payment["payment_id"])
    company.events.append({"kind": "paid_invoice", "invoice_id": invoice_id, "amount_minor": amount})


def _add_clean_history(company, distractors, size):
    rng = company.noise_rng
    at = company.period_start + timedelta(days=rng.randrange(1, 10), hours=rng.randrange(8, 18))
    scale = 10 ** company.exponents[company.base_currency]
    amount = rng.randrange(10, 100) * 1_000 * scale
    if "split_payment" in distractors:
        _add_paid_invoices(company, amount, at, split=True)
    if "equal_amount_different_supplier" in distractors:
        _add_paid_invoices(company, amount, at + timedelta(days=1))
        _add_paid_invoices(company, amount, at + timedelta(days=2))
    if "refund" in distractors:
        entity = company.id("entity")
        deposit = rng.randrange(1, 10) * 1_000 * scale
        payment, _ = company.payment(entity, at, company.base_currency, deposit)
        returned = at + timedelta(days=3)
        company.post(returned, company.base_currency, deposit, "UNAPPLIED", "CASH", entity,
                     payment_id=payment["payment_id"])
        company.events.append({"kind": "refund", "payment_id": payment["payment_id"],
                               "amount_minor": deposit, "returned_at": returned.isoformat()})
    if "archive_copy" in distractors:
        add_duplicate(company, enabled=False)
    while (len(company.ledger) < size.get("ledger_rows_target", 0)
           or len(company.invoices) + len(company.payments) < size.get("documents_target", 0)):
        at = company.period_start + timedelta(days=rng.randrange(1, 21), hours=rng.randrange(8, 18))
        _add_paid_invoices(company, rng.randrange(5, 150) * 1_000 * scale, at, split=bool(rng.getrandbits(1)))


def _add_audit_cases(company, counts, timing, interaction):
    remaining = counts.copy()
    if timing is not None:
        company.boundary_pair_member(TIMINGS[timing])
        remaining["payment_allocation"] -= int(timing != "before")
    if interaction == "allocation_fx_same_payment":
        allocation = bool(remaining["payment_allocation"])
        fx = bool(remaining["fx_conversion"])
        add_allocation_fx(company, allocation_enabled=allocation, fx_enabled=fx)
        remaining["payment_allocation"] -= allocation
        remaining["fx_conversion"] -= fx
    elif interaction == "cutoff_fx_same_invoice":
        cutoff = bool(remaining["period_cutoff"])
        fx = bool(remaining["fx_conversion"])
        add_cutoff_fx(company, cutoff_enabled=cutoff, fx_enabled=fx)
        remaining["period_cutoff"] -= cutoff
        remaining["fx_conversion"] -= fx
    builders = (add_duplicate, lambda b: b.allocation_issue(), add_fx, add_cutoff)
    for issue_type, build in zip(ISSUE_TYPES, builders, strict=True):
        for _ in range(remaining[issue_type]):
            build(company)


def _policy(company):
    return {
        "allocation": {
            "version": "1.0", "deadline": company.period_end.isoformat(),
            "rule": "Apply the entire designated payment before period_end (exclusive). UNAPPLIED is "
                    "credit-normal. Its balance is credits minus debits. The settlement index lists every "
                    "UNAPPLIED movement for the payment strictly before period_end. Unassigned deposits "
                    "can be refunded via debit UNAPPLIED, credit CASH.",
        },
        "period": {
            "version": "1.0", "end_exclusive": True,
            "recognition": "Invoice issue date for invoices; transaction date for cash receipts and settlements.",
        },
        "fx_date": "Local date of the source event. FX_VALUATION is a separate base-currency memorandum "
                   "posting. When it has payment_id, look up the payment timestamp and amount; otherwise "
                   "look up the invoice issue timestamp and total. Quotes are target units per source unit.",
        "rounding": "ROUND_HALF_UP",
        "accounts": {
            "version": "1.0", "AR": "Invoice receivables in the invoice currency",
            "REVENUE": "Source-currency revenue", "CASH": "Cash received or refunded",
            "UNAPPLIED": "Payment amounts held for settlement or refund",
            "FX_VALUATION": "Base-currency reporting valuation of a foreign source event",
            "FX_OFFSET": "Offset to the reporting valuation memorandum account",
        },
    }


def _write_json(path, record):
    path.write_bytes((json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))


def _episode_id(company, manifest, policy, nonce):
    canonical = {
        "manifest": manifest, "policy": policy, "nonce": nonce,
        "ledger": sorted(company.ledger, key=lambda row: row["row_id"]),
        "invoices": sorted(company.invoices, key=lambda doc: doc["doc_id"]),
        "payments": sorted(company.payments, key=lambda doc: doc["doc_id"]),
        "fx": sorted(company.fx, key=lambda row: tuple(row[key] for key in FX_FIELDS)),
    }
    serialized = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "E" + sha256(serialized.encode("utf-8")).hexdigest()[:32].upper()


def _write_records(directory, name, records, rng, fields=None):
    records = list(records)
    rng.shuffle(records)
    if fields is None:
        with (directory / f"{name}.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    else:
        with (directory / f"{name}.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            for record in records:
                writer.writerow({key: "null" if value is None else value for key, value in record.items()})


def generate_episode(seed: int, config: dict[str, object], output_dir: Path) -> None:
    """Generate and validate an episode, then publish it at a fresh path."""
    output = Path(output_dir).absolute()
    if os.path.lexists(output):
        raise FileExistsError(output)
    company, counts, timing, interaction, distractors, size, nonce = _configuration(seed, config)
    _add_audit_cases(company, counts, timing, interaction)
    _add_clean_history(company, distractors, size)
    if not company.invoices and not company.payments:
        _add_paid_invoices(company, 10_000 * 10 ** company.exponents[company.base_currency],
                           company.period_start + timedelta(days=3))
    company.close_settlement_indexes()
    policy = _policy(company)
    company.validate(counts, policy)
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".auditlab-", dir=output.parent) as staging:
        staged = Path(staging) / "snapshot"
        public, private = staged / "public", staged / "private"
        public.mkdir(parents=True)
        private.mkdir()
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "base_currency": company.base_currency, "period_start": company.period_start.isoformat(),
            "period_end": company.period_end.isoformat(), "timezone": config.get("timezone", "Asia/Almaty"),
            "ledger_rows": len(company.ledger), "document_count": len(company.invoices) + len(company.payments),
            "currency_exponents": company.exponents, "rounding": "ROUND_HALF_UP",
        }
        episode_id = _episode_id(company, manifest, policy, nonce)
        manifest["episode_id"] = episode_id
        _write_json(public / "manifest.json", manifest)
        _write_json(public / "policy.json", policy)
        _write_records(public, "ledger", company.ledger, company.order_rng, LEDGER_FIELDS)
        _write_records(public, "invoices", company.invoices, company.order_rng)
        _write_records(public, "payments", company.payments, company.order_rng)
        _write_records(public, "fx", sorted(company.fx, key=lambda row: tuple(row[key] for key in FX_FIELDS)),
                       company.order_rng, FX_FIELDS)
        _write_json(private / "truth.json", {"episode_id": episode_id, "issues": company.issues})
        _write_json(private / "provenance.json", {
            "episode_id": episode_id, "generator_version": GENERATOR_VERSION,
            "seed": seed, "config": config, "events": company.events, "mutations": company.mutations,
            "generation_attempts": 1,
        })
        staged.rename(output)


__all__ = ["generate_episode"]
