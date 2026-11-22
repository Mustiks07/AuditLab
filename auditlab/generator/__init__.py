"""Deterministic synthetic AuditLab snapshots."""

import csv
import json
import os
import re
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from importlib import resources
from importlib.metadata import version
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core import FX_FIELDS, LEDGER_FIELDS, Company
from . import profile13
from .financial_cases import AllocationCase, parse_financial_case
from .scenarios import add_allocation_fx, add_cutoff, add_cutoff_fx, add_duplicate, add_fx


GENERATOR_VERSION = "1.7"
FINANCIAL_FAMILY_GENERATOR_VERSION = "1.8"
HARDENED_GENERATOR_VERSION = "1.9"
GENERATOR_VERSIONS = {"1.0": "1.1", "1.1": GENERATOR_VERSION, "1.2": FINANCIAL_FAMILY_GENERATOR_VERSION,
                      "1.3": HARDENED_GENERATOR_VERSION}
SUPPORTED_GENERATOR_VERSIONS = {"1.6", *GENERATOR_VERSIONS.values()}
SCHEMA_VERSION = "1.0"
CURRENCY_EXPONENTS = {"KZT": 2, "USD": 2, "EUR": 2, "JPY": 0, "KWD": 3}
ISSUE_TYPES = ("duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff")
INTERACTIONS = ("allocation_fx_same_payment", "cutoff_fx_same_invoice")
DISTRACTORS = ("split_payment", "equal_amount_different_supplier", "refund", "archive_copy")
TIMINGS = {"before": -1, "at": 0, "after": 1}
MAX_ISSUES_PER_TYPE = 5
CONFIG_FIELDS = {
    "config_version", "company_size", "base_currency", "currencies", "timezone", "target_period",
    "issue_counts", "interaction", "clean_distractors", "settlement_timing", "snapshot_nonce",
    "allocation_mode", "financial_case", "episode_key",
}


def _bounded_int(value, low, high):
    return type(value) is int and low <= value <= high


def _period(value, zone_name, config_version="1.0"):
    if not isinstance(value, str) or not re.fullmatch(r"20[0-9]{2}-(0[1-9]|1[0-2])", value):
        raise ValueError("invalid_target_period: expected YYYY-MM between 2000 and 2099")
    year, month = map(int, value.split("-"))
    if not isinstance(zone_name, str) or not zone_name:
        raise ValueError("invalid_timezone: expected an IANA zone name")
    if config_version != "1.0":
        parts = zone_name.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("invalid_timezone: expected an IANA zone name")
        try:
            with resources.files("tzdata.zoneinfo").joinpath(*parts).open("rb") as source:
                zone = ZoneInfo.from_file(source, key=zone_name)
        except ModuleNotFoundError:
            raise ValueError("timezone_data_required: install the pinned tzdata package") from None
        except (OSError, ValueError):
            raise ValueError("invalid_timezone: IANA zone unavailable") from None
    # Legacy snapshots retain their exact original timezone representation.
    elif zone_name == "Asia/Almaty" and (year, month) >= (2024, 4):
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
    config_version = config.get("config_version", "1.0")
    if not isinstance(config_version, str) or config_version not in GENERATOR_VERSIONS:
        raise ValueError("unsupported_config_version")
    scenario_seed, id_seed = seed, None
    if config_version == profile13.PROFILE:
        profile13.episode_key(config)
        scenario_seed, id_seed = profile13.materials(seed, config)
        config = profile13.resolve(config, scenario_seed)
    elif profile13.KEY_FIELD in config:
        raise ValueError("invalid_episode_key: requires config_version 1.3")
    modern = config_version != "1.0"
    base = config.get("base_currency", "KZT")
    currencies = config.get("currencies", [base, *(["USD"] if base != "USD" else ["KZT"])])
    if (not isinstance(currencies, list) or not currencies
            or any(not isinstance(code, str) or code not in CURRENCY_EXPONENTS for code in currencies)
            or len(currencies) != len(set(currencies)) or base not in currencies):
        raise ValueError("invalid_currencies: use unique supported currencies including the base currency")
    counts = config.get("issue_counts", {})
    if (not isinstance(counts, dict) or counts.keys() - set(ISSUE_TYPES)
            or any(not _bounded_int(value, 0, MAX_ISSUES_PER_TYPE) for value in counts.values())):
        raise ValueError("invalid_issue_counts: each supported issue count must be an integer from 0 to 5")
    counts = {key: counts.get(key, 0) for key in ISSUE_TYPES}
    interaction = config.get("interaction")
    if interaction is not None and interaction not in INTERACTIONS:
        raise ValueError("unknown_interaction")
    if (interaction or counts["fx_conversion"]) and len(currencies) < 2:
        raise ValueError("foreign_currency_required")
    timing = config.get("settlement_timing")
    allocation_mode = config.get("allocation_mode", "partial")
    if allocation_mode not in ("partial", "omitted") or (
        "allocation_mode" in config and not modern
    ):
        raise ValueError("invalid_allocation_mode: profiles 1.1-1.3 support partial or omitted")
    if allocation_mode == "omitted" and (timing is not None or interaction == "allocation_fx_same_payment"):
        raise ValueError("incompatible_allocation_mode: omission requires a standalone settlement")
    if timing is not None:
        if config_version == "1.2":
            raise ValueError("invalid_settlement_timing: profile 1.2 cannot combine with legacy timing; "
                             "set financial_case.settlement_offset_minutes")
        if not isinstance(timing, str) or timing not in TIMINGS:
            raise ValueError("invalid_settlement_timing: expected before, at or after")
        if "payment_allocation" not in config.get("issue_counts", {}):
            counts["payment_allocation"] = int(timing != "before")
        elif timing != "before" and counts["payment_allocation"] < 1:
            raise ValueError("contradictory_settlement_timing")
    if modern and sum(counts.values()) > 12:
        raise ValueError("invalid_issue_counts: profiles 1.1-1.3 support at most 12 issues")
    financial_case = None
    if "financial_case" in config:
        if config_version not in ("1.2", "1.3"):
            raise ValueError("invalid_financial_case: requires config_version 1.2 or 1.3")
        if timing is not None or interaction is not None:
            raise ValueError("invalid_financial_case: cannot combine with settlement_timing or interaction")
        financial_case = parse_financial_case(config["financial_case"], allocation_mode, scenario_seed)
        if any(count > (1 if kind == financial_case.issue_type else 0) for kind, count in counts.items()):
            raise ValueError("invalid_issue_counts: financial_case supports zero or one issue of its own type")
        if allocation_mode == "omitted" and not isinstance(financial_case, AllocationCase):
            raise ValueError("incompatible_allocation_mode: omission requires an allocation case")
    distractors = config.get("clean_distractors", list(DISTRACTORS))
    supported_distractors = (*DISTRACTORS, "next_day_settlement") if modern else DISTRACTORS
    if (not isinstance(distractors, list) or any(item not in supported_distractors for item in distractors)
            or len(distractors) != len(set(distractors))):
        raise ValueError("invalid_clean_distractors")
    size = config.get("company_size", {})
    document_limit = 200 if modern else 600
    if (not isinstance(size, dict) or size.keys() - {"ledger_rows_target", "documents_target"}
            or not _bounded_int(size.get("ledger_rows_target", 0), 0, 2_000)
            or not _bounded_int(size.get("documents_target", 0), 0, document_limit)):
        raise ValueError(f"invalid_company_size: use integer targets 0–2000 ledger rows and 0–{document_limit} documents")
    nonce = config.get("snapshot_nonce", "")
    if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{0,64}", nonce):
        raise ValueError("invalid_snapshot_nonce")
    zone_name = config.get("timezone", "Asia/Almaty")
    period_start, period_end = _period(config.get("target_period", "2026-11"), zone_name, config_version)
    exponents = {code: CURRENCY_EXPONENTS[code] for code in currencies}
    company = Company(scenario_seed, period_start, period_end, period_start.tzinfo, base, exponents,
                      config_version=config_version, allocation_mode=allocation_mode, id_seed=id_seed)
    return company, counts, timing, interaction, distractors, size, nonce, financial_case, config


def _add_clean_invoice(company, amount, issued_at):
    entity = company.id("entity")
    currency = company.base_currency
    invoice = company.invoice(entity, issued_at, currency, amount)
    company.post(issued_at, currency, amount, "AR", "REVENUE", entity, invoice_id=invoice["invoice_id"])
    return invoice


def _add_paid_invoices(company, amount, issued_at, *, split=False):
    invoice = _add_clean_invoice(company, amount, issued_at)
    entity, currency = invoice["entity_id"], invoice["currency"]
    invoice_id = invoice["invoice_id"]
    received = issued_at + timedelta(days=2)
    if split:
        first = (amount * company.noise_rng.randrange(1_700, 8_701) // 10_000
                 if company.financial_variants else amount * 2 // 5)
        parts = (first, amount - first)
    else:
        parts = (amount,)
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
    if company.uses_source_book:
        at += timedelta(minutes=rng.randrange(60))
        amount = rng.randrange(10_000 * scale, 100_000 * scale)
    else:
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
    if "archive_copy" in distractors and company.config_version == "1.0":
        add_duplicate(company, enabled=False)
    if company.config_version == profile13.PROFILE and distractors:
        # Correctly settled designated payments whose last tranche lands minutes before the
        # exclusive boundary: the clean counterpart of a late or partial settlement.
        for index in range(profile13.lookalike_count(company)):
            profile13.lookalike_case(company, index).build(company, enabled=False)
    if company.uses_source_book:
        if distractors:
            add_cutoff(company, enabled=False)
            # Neighboring-period evidence also contains valid recognition outside the target.
            # Vary its count independently of the requested mispostings.
            for _ in range(rng.randint(1, MAX_ISSUES_PER_TYPE + 1)):
                add_cutoff(company, enabled=False, next_period=True)
        duplicate_capacity = 2 * MAX_ISSUES_PER_TYPE if "archive_copy" in distractors else 0
        _add_bounded_filler(company, size, rng, scale, duplicate_capacity)
    else:
        _add_filler(company, size, rng, scale)


def _add_bounded_filler(company, size, rng, scale, duplicate_capacity):
    """Keep primary documents bounded while allowing larger accounting books."""
    docs_target = size.get("documents_target", 0)
    rows_target = size.get("ledger_rows_target", 0)
    # Match the primary document mix across duplicate counts, including the
    # all-clean state. Budget for the largest allowed addition, not this label.
    extra_rows = max(duplicate_capacity, company.planned_ledger_rows - len(company.ledger))
    while len(company.invoices) + len(company.payments) < docs_target:
        remaining = docs_target - len(company.invoices) - len(company.payments)
        at = company.period_start + timedelta(days=rng.randrange(2, 20), hours=rng.randrange(8, 18),
                                             minutes=rng.randrange(60))
        amount = rng.randrange(5_000 * scale, 150_000 * scale)
        budgeted_rows = len(company.ledger) + extra_rows
        if rows_target and budgeted_rows + 3 * remaining + remaining % 2 > rows_target:
            # A properly recognized, unpaid invoice needs two rows and one document.
            _add_clean_invoice(company, amount, at)
        elif remaining == 1:
            entity = company.id("entity")
            payment, _ = company.payment(entity, at, company.base_currency, amount)
            company.post(at + timedelta(minutes=1), company.base_currency, amount, "UNAPPLIED", "CASH",
                         entity, payment_id=payment["payment_id"])
            company.events.append({"kind": "refund", "payment_id": payment["payment_id"],
                                   "amount_minor": amount, "returned_at": (at + timedelta(minutes=1)).isoformat()})
        else:
            split = remaining >= 3 and bool(rng.getrandbits(1))
            if split and rows_target:
                rest = remaining - 3
                minimum_rows = budgeted_rows + 10 + 3 * rest + rest % 2
                split = minimum_rows <= rows_target
            _add_paid_invoices(company, amount, at, split=split)
    if company.allocation_mode == "omitted":
        # The omitted settlement and its clean control share a public size
        # floor. A shorter target is padded only by real treasury journals.
        rows_target = max(rows_target, len(company.ledger) + extra_rows)
    # Treasury cash transfers are real balanced internal events, without extra external invoices.
    # They do not change designated-payment settlement indexes or financial issue evidence.
    available_cash = sum(row["debit_minor"] - row["credit_minor"] for row in company.ledger
                         if row["account"] == "CASH" and row["currency"] == company.base_currency)
    if company.planned_ledger_rows < rows_target and available_cash <= 0:
        available_cash = 10_000 * scale
        _add_paid_invoices(company, available_cash, company.period_start + timedelta(days=3))
    treasury_entity = None
    balance = 0
    at = company.period_end - timedelta(hours=3)
    while company.planned_ledger_rows < rows_target:
        if treasury_entity is None:
            treasury_entity = company.id("entity")
        amount = balance if balance else min(available_cash, rng.randrange(1, 100) * scale)
        debit, credit = ("CASH", "CASH_RESERVE") if balance else ("CASH_RESERVE", "CASH")
        row, _ = company.post(at, company.base_currency, amount, debit, credit, treasury_entity)
        company.events.append({"kind": "treasury_transfer", "journal_id": row["journal_id"],
                               "from_account": credit, "to_account": debit,
                               "amount_minor": amount, "currency": company.base_currency,
                               "timestamp": at.isoformat()})
        balance = 0 if balance else amount
        at += timedelta(seconds=2)
    if company.planned_ledger_rows > 2_000 or len(company.invoices) + len(company.payments) > 200:
        raise ValueError("company_size_exceeds_profile: use fewer source events")


def _add_filler(company, size, rng, scale):
    rows_target = size.get("ledger_rows_target", 0)
    docs_target = size.get("documents_target", 0)
    rows_needed = rows_target - len(company.ledger)
    docs_needed = docs_target - len(company.invoices) - len(company.payments)
    # A paid invoice adds 6 rows and 2 documents, or 10 rows and 3 documents with split payment.
    split_count = rows_needed - 3 * docs_needed
    single_count, remainder = divmod(docs_needed - 3 * split_count, 2)
    if split_count >= 0 and single_count >= 0 and remainder == 0:
        payments_split = [True] * split_count + [False] * single_count
        rng.shuffle(payments_split)
    else:
        payments_split = []
    while len(company.ledger) < rows_target or len(company.invoices) + len(company.payments) < docs_target:
        at = company.period_start + timedelta(days=rng.randrange(1, 21), hours=rng.randrange(8, 18))
        amount = rng.randrange(5, 150) * 1_000 * scale
        split = payments_split.pop() if payments_split else bool(rng.getrandbits(1))
        _add_paid_invoices(company, amount, at, split=split)


def _add_audit_cases(company, counts, timing, interaction, distractors, financial_case):
    remaining = counts.copy()
    if financial_case is not None:
        financial_case.build(company, enabled=bool(remaining[financial_case.issue_type]))
        remaining[financial_case.issue_type] = 0
        if isinstance(financial_case, AllocationCase) and "next_day_settlement" in distractors:
            financial_case.build(company, enabled=False)
    if timing is not None:
        company.boundary_pair_member(TIMINGS[timing])
        remaining["payment_allocation"] -= int(timing != "before")
    if interaction == "allocation_fx_same_payment":
        allocation = bool(remaining["payment_allocation"])
        fx = bool(remaining["fx_conversion"])
        add_allocation_fx(company, allocation_enabled=allocation, fx_enabled=fx)
        remaining["payment_allocation"] -= allocation
        remaining["fx_conversion"] -= fx
    if interaction == "cutoff_fx_same_invoice":
        cutoff = bool(remaining["period_cutoff"])
        fx = bool(remaining["fx_conversion"])
        remaining["period_cutoff"] -= cutoff
        remaining["fx_conversion"] -= fx
    else:
        cutoff = fx = False
    if company.uses_source_book and (
        interaction == "cutoff_fx_same_invoice" or (distractors and len(company.exponents) > 1)
    ):
        [focus] = company.sample_fault_slots("cutoff_fx", 2, 1)
        for slot in range(2):
            add_cutoff_fx(company, cutoff_enabled=cutoff and slot == focus, fx_enabled=fx and slot == focus)
        # With exactly one clean neighbor the number of future foreign entries
        # would still disclose the cutoff switch for this interaction.
        for _ in range(company.noise_rng.randrange(1, 4)):
            add_cutoff_fx(company, cutoff_enabled=False, fx_enabled=False, next_period=True)
    elif interaction == "cutoff_fx_same_invoice":
        add_cutoff_fx(company, cutoff_enabled=cutoff, fx_enabled=fx)
    builders = (add_duplicate, Company.allocation_case, add_fx, add_cutoff)
    matched_controls = (
        "archive_copy" in distractors,
        "split_payment" in distractors or "next_day_settlement" in distractors
        or (financial_case is None and company.allocation_mode == "omitted"),
        bool(distractors) and len(company.exponents) > 1,
        False,
    )
    for issue_type, build, controls in zip(ISSUE_TYPES, builders, matched_controls, strict=True):
        requested = remaining[issue_type]
        if controls and company.uses_source_book:
            capacity = MAX_ISSUES_PER_TYPE + 1
            faulty_slots = company.sample_fault_slots(issue_type, capacity, requested)
            for slot in range(capacity):
                build(company, enabled=slot in faulty_slots)
        else:
            for _ in range(requested):
                build(company)


def _policy(company):
    policy = {
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
    if company.uses_source_book:
        policy["accounts"]["version"] = "1.1"
        policy["accounts"]["CASH_RESERVE"] = (
            "Treasury reserve cash in the base currency. A CASH/CASH_RESERVE journal is an internal "
            "transfer, not a receipt, expense, invoice recognition or customer settlement. It has null "
            "invoice_id/payment_id and preserves total cash; no external primary document is required.")
        policy["rule_versions"] = {
            "version": "1.0",
            "rules": {key: value.get("version", "1.0") if isinstance(value, dict) else "1.0"
                      for key, value in policy.items()},
        }
    return policy


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


def _write_snapshot_records(directory, company, order):
    if company.config_version == "1.0":
        _write_records(directory, "ledger", company.ledger, order, LEDGER_FIELDS)
    _write_records(directory, "invoices", company.invoices, order)
    _write_records(directory, "payments", company.payments, order)
    _write_records(directory, "fx", sorted(company.fx, key=lambda row: tuple(row[key] for key in FX_FIELDS)),
                   order, FX_FIELDS)
    if company.uses_source_book:
        # A change in the ledger row count must not reshuffle otherwise identical documents.
        _write_records(directory, "ledger", company.ledger, order, LEDGER_FIELDS)


def _write_source_book(source, changed, final_manifest, policy, nonce, order, private, public):
    """Keep a public-format clean book in the trusted area, with a lossless transition."""
    directory = private / "source_book"
    directory.mkdir()
    manifest = {key: value for key, value in final_manifest.items() if key != "episode_id"}
    manifest.update(ledger_rows=len(source.ledger),
                    document_count=len(source.invoices) + len(source.payments))
    manifest["episode_id"] = _episode_id(source, manifest, policy, nonce)
    _write_json(directory / "manifest.json", manifest)
    _write_json(directory / "policy.json", policy)
    _write_snapshot_records(directory, source, order)

    accounts = {"duplicate_invoice": {"AR", "REVENUE"},
                "payment_allocation": {"AR", "UNAPPLIED"},
                "period_cutoff": {"AR", "REVENUE", "FX_VALUATION", "FX_OFFSET"},
                "fx_conversion": {"FX_VALUATION", "FX_OFFSET"}}
    changes = []
    for table, key in (("ledger", "row_id"), ("invoices", "doc_id"), ("payments", "doc_id")):
        before = {record[key]: record for record in getattr(source, table)}
        after = {record[key]: record for record in getattr(changed, table)}
        for record_id in sorted(before.keys() | after.keys()):
            old, new = before.get(record_id), after.get(record_id)
            if old == new:
                continue
            record = new if new is not None else old
            entities = {f"{kind}/{record[kind + '_id']}" for kind in ("invoice", "payment")
                        if record.get(kind + "_id") is not None}
            indexes = [index for index, mutation in enumerate(changed.mutations)
                       if entities.intersection(mutation["affected_ids"])
                       and (table == "ledger" and record["account"] in accounts[mutation["type"]]
                            or table == "payments" and mutation["type"] == "payment_allocation")]
            if not indexes:
                raise RuntimeError("source_book_change_without_mutation")
            changes.append({"table": table, "id": record_id, "before": old, "after": new,
                            "mutation_indexes": indexes})
    transition = {"schema_version": "1.0", "source_episode_id": manifest["episode_id"],
                  "target_episode_id": final_manifest["episode_id"], "changes": changes}
    transition_path = private / "source_transition.json"
    _write_json(transition_path, transition)
    def hashes(root):
        return {file.name: sha256(file.read_bytes()).hexdigest() for file in sorted(root.iterdir())}
    return {"schema_version": "1.0", "path": "source_book", "episode_id": manifest["episode_id"],
            "files_sha256": hashes(directory), "target_files_sha256": hashes(public),
            "transition_path": transition_path.name,
            "transition_sha256": sha256(transition_path.read_bytes()).hexdigest()}


def generate_episode(seed: int, config: dict[str, object], output_dir: Path) -> None:
    """Generate and validate an episode, then publish it at a fresh path."""
    output = Path(output_dir).absolute()
    if os.path.lexists(output):
        raise FileExistsError(output)
    company, counts, timing, interaction, distractors, size, nonce, financial_case, resolved = _configuration(
        seed, config)
    _add_audit_cases(company, counts, timing, interaction, distractors, financial_case)
    _add_clean_history(company, distractors, size)
    if not company.invoices and not company.payments:
        _add_paid_invoices(company, 10_000 * 10 ** company.exponents[company.base_currency],
                           company.period_start + timedelta(days=3))
    company.close_settlement_indexes()
    policy = _policy(company)
    if company.uses_source_book:
        company.validate({}, policy)
        source_book_validation = {
            "ledger_rows": len(company.ledger),
            "document_count": len(company.invoices) + len(company.payments),
            "issue_count": len(company.issues),
        }
        source_company = company
        source_order = Random()
        source_order.setstate(company.order_rng.getstate())
        company = company.apply_mutations()
        company.close_settlement_indexes()
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
            "period_end": company.period_end.isoformat(), "timezone": resolved.get("timezone", "Asia/Almaty"),
            "ledger_rows": len(company.ledger), "document_count": len(company.invoices) + len(company.payments),
            "currency_exponents": company.exponents, "rounding": "ROUND_HALF_UP",
        }
        if company.uses_source_book:
            manifest["period_end_exclusive"] = True
        episode_id = _episode_id(company, manifest, policy, nonce)
        manifest["episode_id"] = episode_id
        _write_json(public / "manifest.json", manifest)
        _write_json(public / "policy.json", policy)
        _write_snapshot_records(public, company, company.order_rng)
        _write_json(private / "truth.json", {"episode_id": episode_id, "issues": company.issues})
        provenance = {
            "episode_id": episode_id, "generator_version": GENERATOR_VERSIONS[company.config_version],
            "seed": seed, "config": config, "events": company.events, "mutations": company.mutations,
            "generation_attempts": 1,
        }
        if company.config_version == profile13.PROFILE:
            # The input config (with any key) stays the reproduction recipe; the resolved
            # defaults and the derivation are recorded for verifiers, all on the trusted side.
            provenance["resolved_config"] = resolved
            provenance["randomness"] = profile13.randomness_record(config)
        if company.uses_source_book:
            provenance["timezone_database"] = {"package": "tzdata", "version": version("tzdata")}
            provenance["source_book_validation"] = source_book_validation
            provenance["source_book"] = _write_source_book(
                source_company, company, manifest, policy, nonce, source_order, private, public,
            )
        _write_json(private / "provenance.json", provenance)
        staged.rename(output)


__all__ = ["generate_episode"]
