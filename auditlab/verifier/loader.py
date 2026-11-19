"""Reader of the public snapshot schema (SPEC §5). Parses and normalises, never computes money.

Only files under ``public_dir`` are opened. Timestamps are reduced to UTC epoch seconds and
the company-local calendar date; money stays as integer minor units; FX rates stay as the
original decimal strings so that the SQL layer performs the only conversion.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
from importlib.metadata import version
from importlib.resources import files
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

NULL_MARKER = "null"
RATE_PATTERN = re.compile(r"^[0-9]+(\.[0-9]+)?$")
PUBLIC_FILES = ("manifest.json", "policy.json", "ledger.csv", "invoices.jsonl",
                "payments.jsonl", "fx.csv")


class SnapshotError(ValueError):
    """The public snapshot violates the published schema."""


@dataclass
class Snapshot:
    episode_id: str
    base_currency: str
    period_start: str
    period_end: str
    deadline: str
    offset: timedelta
    timezone_name: str
    timezone_database: str
    exponents: dict[str, int]
    rounding: str
    input_sha256: dict[str, str]
    tables: dict[str, list[tuple]] = field(default_factory=dict)
    notes: list[dict[str, str]] = field(default_factory=list)


def _instant(text: str, what: str) -> datetime:
    try:
        value = datetime.fromisoformat(text)
    except (TypeError, ValueError) as error:
        raise SnapshotError(f"{what}: invalid timestamp {text!r}") from error
    if value.tzinfo is None:
        raise SnapshotError(f"{what}: timestamp without UTC offset {text!r}")
    return value


def _company_timezone(name: object) -> tuple[ZoneInfo, str]:
    """Use the bundled IANA database on every OS; never infer a zone from one offset."""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*", name):
        raise SnapshotError("manifest.timezone: expected an IANA timezone name")
    try:
        database = files("tzdata.zoneinfo")
    except ModuleNotFoundError as error:
        raise SnapshotError("timezone database unavailable: install the pinned tzdata dependency") from error
    try:
        with database.joinpath(*name.split("/")).open("rb") as stream:
            zone = ZoneInfo.from_file(stream, key=name)
    except (OSError, ValueError) as error:
        raise SnapshotError(f"manifest.timezone: unknown IANA timezone {name!r}") from error
    return zone, "tzdata:" + version("tzdata")


def _nullable(value):
    return None if value is None or value == NULL_MARKER else value


def _int(value, what: str) -> int:
    if isinstance(value, bool):
        raise SnapshotError(f"{what}: boolean is not an amount")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value):
        return int(value)
    raise SnapshotError(f"{what}: expected integer minor units, got {value!r}")


def _jsonl(path: Path) -> list[dict]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise SnapshotError(f"{path.name}:{number}: {error}") from error
    return rows


def load_snapshot(public_dir: Path | str) -> Snapshot:
    public = Path(public_dir)
    if public.name == "private" or (public / "truth.json").exists():
        raise SnapshotError("verifier accepts public/ only")
    hashes = {}
    for name in PUBLIC_FILES:
        path = public / name
        if not path.is_file():
            raise SnapshotError(f"missing public file {name}")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()

    manifest = json.loads((public / "manifest.json").read_text(encoding="utf-8"))
    policy = json.loads((public / "policy.json").read_text(encoding="utf-8"))
    start = _instant(manifest["period_start"], "manifest.period_start")
    end = _instant(manifest["period_end"], "manifest.period_end")
    deadline_text = policy.get("allocation", {}).get("deadline", manifest["period_end"])
    deadline = _instant(deadline_text, "policy.allocation.deadline")
    notes = []
    offset = start.utcoffset()
    company, timezone_database = _company_timezone(manifest.get("timezone"))
    if deadline != end:
        notes.append({"kind": "ambiguity", "subject": "policy/allocation",
                      "detail": f"allocation deadline {deadline_text} differs from period_end "
                                f"{manifest['period_end']}; deadline is used"})
    exponents = {str(k): _int(v, "currency_exponents") for k, v in
                 manifest.get("currency_exponents", {}).items()}

    def stamp(text: str, what: str) -> tuple[int, str]:
        value = _instant(text, what)
        return int(value.timestamp()), value.astimezone(company).date().isoformat()

    snapshot = Snapshot(
        episode_id=str(manifest["episode_id"]), base_currency=str(manifest["base_currency"]),
        period_start=manifest["period_start"], period_end=manifest["period_end"],
        deadline=deadline_text, offset=offset, timezone_name=company.key,
        timezone_database=timezone_database, exponents=exponents,
        rounding=str(policy.get("rounding", manifest.get("rounding", ""))),
        input_sha256=hashes, notes=notes)
    if snapshot.rounding != "ROUND_HALF_UP":
        raise SnapshotError(f"unsupported rounding policy {snapshot.rounding!r}")

    ledger = []
    with (public / "ledger.csv").open(encoding="utf-8", newline="") as stream:
        for order, row in enumerate(csv.DictReader(stream)):
            what = f"ledger/{row.get('row_id')}"
            epoch, local = stamp(row["timestamp"], what)
            ledger.append((row["row_id"], row["timestamp"], epoch, local, row["account"],
                           _int(row["debit_minor"], what), _int(row["credit_minor"], what),
                           row["currency"], _nullable(row.get("entity_id")),
                           _nullable(row.get("invoice_id")), _nullable(row.get("payment_id")),
                           _nullable(row.get("journal_id")) or f"~{row['row_id']}", order))

    invoices, lines = [], []
    for doc in _jsonl(public / "invoices.jsonl"):
        what = f"invoices/{doc['doc_id']}"
        epoch, local = stamp(doc["issued_at"], what)
        key = doc.get("canonical_key") or doc["invoice_id"]
        invoices.append((doc["doc_id"], doc["invoice_id"], doc.get("entity_id"), doc["issued_at"],
                         epoch, local, doc["currency"], _int(doc["total_minor"], what), key))
        for index, item in enumerate(doc.get("line_items") or []):
            lines.append((doc["doc_id"], index, _int(item["amount_minor"], what)))

    payments, refs, allocations, index_rows = [], [], [], []
    for doc in _jsonl(public / "payments.jsonl"):
        what = f"payments/{doc['doc_id']}"
        epoch, local = stamp(doc["timestamp"], what)
        payments.append((doc["doc_id"], doc["payment_id"], doc.get("entity_id"), doc["timestamp"],
                         epoch, local, doc["currency"], _int(doc["amount_minor"], what)))
        for order, invoice_id in enumerate(doc.get("invoice_refs") or []):
            refs.append((doc["payment_id"], invoice_id, order))
        for item in doc.get("allocation_instructions") or []:
            allocations.append((doc["payment_id"], item["invoice_id"],
                                _int(item["amount_minor"], what)))
        settlement = doc.get("settlement_index")
        if settlement is not None:
            before, _ = stamp(settlement["before"], what + "/settlement_index")
            index_rows.append((doc["payment_id"], before, None))
            for row_id in settlement.get("unapplied_row_ids") or []:
                index_rows.append((doc["payment_id"], before, row_id))

    fx = []
    with (public / "fx.csv").open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            rate = (row.get("rate") or "").strip()
            if not RATE_PATTERN.fullmatch(rate):
                raise SnapshotError(f"fx/{row.get('date')}: rate must be a plain decimal, got {rate!r}")
            fx.append((row["date"], row["source_currency"], row["target_currency"], rate))

    start_epoch, _ = stamp(manifest["period_start"], "period_start")
    end_epoch, _ = stamp(manifest["period_end"], "period_end")
    deadline_epoch, _ = stamp(deadline_text, "deadline")
    snapshot.tables = {
        "params": [(start_epoch, end_epoch, deadline_epoch, snapshot.base_currency,
                    manifest["period_end"], deadline_text)],
        "currency_exp": sorted(exponents.items()),
        "ledger": ledger, "invoices": invoices, "invoice_lines": lines, "payments": payments,
        "payment_refs": refs, "payment_alloc": allocations, "settlement_index": index_rows,
        "fx": fx,
    }
    return snapshot


TABLE_DDL = {
    "params": "period_start_epoch BIGINT, period_end_epoch BIGINT, deadline_epoch BIGINT, "
              "base_currency TEXT, period_end_text TEXT, deadline_text TEXT",
    "currency_exp": "currency TEXT, exp INTEGER",
    "ledger": "row_id TEXT, ts_text TEXT, ts BIGINT, local_date TEXT, account TEXT, "
              "debit_minor BIGINT, credit_minor BIGINT, currency TEXT, entity_id TEXT, "
              "invoice_id TEXT, payment_id TEXT, journal_id TEXT, src_order INTEGER",
    "invoices": "doc_id TEXT, invoice_id TEXT, entity_id TEXT, issued_text TEXT, issued_ts BIGINT, "
                "local_date TEXT, currency TEXT, total_minor BIGINT, canonical_key TEXT",
    "invoice_lines": "doc_id TEXT, line_no INTEGER, amount_minor BIGINT",
    "payments": "doc_id TEXT, payment_id TEXT, entity_id TEXT, ts_text TEXT, ts BIGINT, "
                "local_date TEXT, currency TEXT, amount_minor BIGINT",
    "payment_refs": "payment_id TEXT, invoice_id TEXT, ref_order INTEGER",
    "payment_alloc": "payment_id TEXT, invoice_id TEXT, amount_minor BIGINT",
    "settlement_index": "payment_id TEXT, before_ts BIGINT, row_id TEXT",
    "fx": "rate_date TEXT, source_currency TEXT, target_currency TEXT, rate TEXT",
}
