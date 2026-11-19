"""Public inspection tools for an immutable AuditLab snapshot.

Pages contain ``records`` and ``next_cursor``, ordered by the table's lookup key.
Use row_id for ledger, doc_id for invoices and payments, date/SOURCE/TARGET for
FX, and a top-level rule name for policy. Policy pages expose id/value pairs.
The calculator returns a decimal string.
"""

import csv
import json
import re
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from auditlab.contracts import Action, PAGE_LIMIT, PublicSession, ToolReply

from ._calculator import calculate


PUBLIC_TABLES = ("ledger", "invoices", "payments", "fx", "policy")
_ARGUMENTS = {
    "list_records": {"table", "cursor", "limit"},
    "get_record": {"table", "id"},
    "get_document": {"doc_id"},
    "calculator": {"expression"},
}
_ERRORS = {
    "invalid_action": "Expected an action with an argument object.",
    "unknown_action": "Unknown inspection action.",
    "invalid_args": "Invalid arguments for this action.",
    "unknown_table": "Unknown public table.",
    "not_found": "Public record not found.",
    "invalid_cursor": "Invalid cursor for this table and episode.",
    "invalid_expression": "Invalid decimal expression.",
    "division_by_zero": "Division by zero.",
    "arithmetic_limit": "Decimal arithmetic exceeded the calculator limits.",
}
_CURSOR = re.compile(r"([1-9a-f][0-9a-f]{0,15})\.[0-9a-f]{32}")


def _error(code: str) -> ToolReply:
    return {"ok": False, "error": {"code": code, "message": _ERRORS[code]}}


def _reply(value: object) -> ToolReply:
    return {"ok": True, "result": deepcopy(value)}


def _snapshot_file(root: Path, name: str) -> Path:
    path = root / name
    if path.resolve(strict=True) != path:
        raise ValueError("A public snapshot file must be local to the snapshot.")
    return path


def _read_csv(root: Path, name: str) -> list[dict]:
    with _snapshot_file(root, name).open(encoding="utf-8", newline="") as stream:
        return [
            {key: None if value == "null" else value for key, value in row.items()}
            for row in csv.DictReader(stream)
        ]


def _read_json(root: Path, name: str) -> dict:
    with _snapshot_file(root, name).open(encoding="utf-8") as stream:
        return json.load(stream)


def _read_jsonl(root: Path, name: str) -> list[dict]:
    with _snapshot_file(root, name).open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _ledger_row(row: dict) -> dict:
    for key in ("debit_minor", "credit_minor"):
        row[key] = int(row[key])
    return row


def _index(entries) -> dict[str, dict]:
    result = {}
    for key, record in entries:
        if not isinstance(key, str) or not key or key in result:
            raise ValueError("A public index contains a missing or duplicate key.")
        result[key] = record
    return result


class _Session:
    def __init__(self, public_dir: Path):
        root = public_dir.resolve(strict=True)
        self._episode_id = _read_json(root, "manifest.json")["episode_id"]
        ledger = _read_csv(root, "ledger.csv")
        invoices = _read_jsonl(root, "invoices.jsonl")
        payments = _read_jsonl(root, "payments.jsonl")
        fx = _read_csv(root, "fx.csv")
        policy = _read_json(root, "policy.json")

        self._by_id = {
            "ledger": _index((row["row_id"], _ledger_row(row)) for row in ledger),
            "invoices": _index((doc["doc_id"], doc) for doc in invoices),
            "payments": _index((doc["doc_id"], doc) for doc in payments),
            "fx": _index(
                ("/".join((row["date"], row["source_currency"], row["target_currency"])), row)
                for row in fx
            ),
            "policy": {key: {"id": key, "value": value} for key, value in policy.items()},
        }
        self._documents = _index((doc["doc_id"], doc) for doc in invoices + payments)
        self._ordered = {
            table: [lookup[key] for key in sorted(lookup)]
            for table, lookup in self._by_id.items()
        }

    def inspect(self, action: Action) -> ToolReply:
        if not isinstance(action, dict) or action.keys() != {"action", "args"}:
            return _error("invalid_action")
        name, args = action["action"], action["args"]
        if not isinstance(name, str) or not isinstance(args, dict):
            return _error("invalid_action")
        if name not in _ARGUMENTS:
            return _error("unknown_action")
        if args.keys() != _ARGUMENTS[name]:
            return _error("invalid_args")
        if name == "get_document":
            return self._get(self._documents, args["doc_id"])
        if name == "calculator":
            return self._calculate(args["expression"])

        table = args["table"]
        if not isinstance(table, str):
            return _error("invalid_args")
        if table not in self._by_id:
            return _error("unknown_table")
        if name == "get_record":
            return self._get(self._by_id[table], args["id"], policy=table == "policy")
        return self._list(table, args["cursor"], args["limit"])

    @staticmethod
    def _get(lookup: dict, key: object, *, policy: bool = False) -> ToolReply:
        if not isinstance(key, str):
            return _error("invalid_args")
        if key not in lookup:
            return _error("not_found")
        record = lookup[key]
        return _reply(record["value"] if policy else record)

    def _cursor(self, table: str, offset: int) -> str:
        payload = json.dumps([self._episode_id, table, offset], separators=(",", ":"))
        checksum = sha256(payload.encode("utf-8")).hexdigest()[:32]
        return f"{offset:x}.{checksum}"

    def _list(self, table: str, cursor: object, limit: object) -> ToolReply:
        if type(limit) is not int or not 1 <= limit <= PAGE_LIMIT:
            return _error("invalid_args")
        records = self._ordered[table]
        start = 0
        if cursor is not None:
            if not isinstance(cursor, str) or len(cursor) > 49:
                return _error("invalid_cursor")
            match = _CURSOR.fullmatch(cursor)
            if match is None:
                return _error("invalid_cursor")
            start = int(match[1], 16)
            if start >= len(records) or cursor != self._cursor(table, start):
                return _error("invalid_cursor")
        end = start + limit
        next_cursor = self._cursor(table, end) if end < len(records) else None
        return _reply({"records": records[start:end], "next_cursor": next_cursor})

    @staticmethod
    def _calculate(expression: object) -> ToolReply:
        if not isinstance(expression, str):
            return _error("invalid_args")
        try:
            return _reply(calculate(expression))
        except ValueError:
            return _error("invalid_expression")
        except ZeroDivisionError:
            return _error("division_by_zero")
        except ArithmeticError:
            return _error("arithmetic_limit")


def open_session(public_dir: Path) -> PublicSession:
    """Open the documented public files once and serve only in-memory records."""
    return _Session(Path(public_dir))
