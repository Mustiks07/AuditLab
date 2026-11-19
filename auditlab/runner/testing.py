"""Test double for PublicSession over a public/ directory, used until auditlab.environment lands.

It only reads public/ files. It is NOT the product session: it implements just enough of
list_records / get_record / get_document / calculator for runner tests and the demo export.
"""
from __future__ import annotations

import ast
import csv
import json
import operator
from pathlib import Path

from auditlab.contracts import PAGE_LIMIT, Action, ToolReply

_TABLES = {"ledger": ("ledger.csv", "row_id"), "invoices": ("invoices.jsonl", "invoice_id"),
           "payments": ("payments.jsonl", "payment_id")}
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def _err(code: str, message: str) -> ToolReply:
    return {"ok": False, "error": {"code": code, "message": message}}


class FixturePublicSession:
    def __init__(self, public_dir: Path):
        self._dir = Path(public_dir)
        self._rows: dict[str, list[dict]] = {}
        with (self._dir / "ledger.csv").open(encoding="utf-8", newline="") as reader:
            self._rows["ledger"] = [{k: (None if v == "null" else
                                         int(v) if k.endswith("_minor") else v) for k, v in row.items()}
                                    for row in csv.DictReader(reader)]
        for table in ("invoices", "payments"):
            text = (self._dir / _TABLES[table][0]).read_text(encoding="utf-8")
            self._rows[table] = [json.loads(line) for line in text.splitlines() if line.strip()]
        self._policy = json.loads((self._dir / "policy.json").read_text(encoding="utf-8"))

    def inspect(self, action: Action) -> ToolReply:
        args = action["args"]
        name = action["action"]
        if name == "list_records":
            table, cursor, limit = args.get("table"), args.get("cursor"), args.get("limit")
            if table not in self._rows or not isinstance(limit, int) or isinstance(limit, bool) \
                    or not 1 <= limit <= PAGE_LIMIT:
                return _err("invalid_request", "Invalid list request.")
            start = 0
            if cursor is not None:
                if not (isinstance(cursor, str) and cursor.startswith(table + ":") and cursor[len(table) + 1:].isdigit()):
                    return _err("invalid_cursor", "Invalid cursor.")
                start = int(cursor[len(table) + 1:])
            rows = self._rows[table]
            end = start + limit
            return {"ok": True, "result": {"records": rows[start:end],
                                           "next_cursor": f"{table}:{end}" if end < len(rows) else None}}
        if name == "get_record":
            table, key = args.get("table"), args.get("id")
            if table == "policy":
                value = self._policy.get(key) if isinstance(key, str) else None
                return {"ok": True, "result": value} if value is not None else _err("not_found", "Not found.")
            if table not in self._rows or not isinstance(key, str):
                return _err("not_found", "Not found.")
            id_field = _TABLES[table][1]
            for row in self._rows[table]:
                if row[id_field] == key:
                    return {"ok": True, "result": row}
            return _err("not_found", "Not found.")
        if name == "get_document":
            doc_id = args.get("doc_id")
            for table in ("invoices", "payments"):
                for row in self._rows[table]:
                    if row["doc_id"] == doc_id:
                        return {"ok": True, "result": row}
            return _err("not_found", "Not found.")
        if name == "calculator":
            expression = args.get("expression")
            if not isinstance(expression, str) or len(expression) > 200:
                return _err("invalid_expression", "Invalid expression.")
            try:
                return {"ok": True, "result": str(self._calc(ast.parse(expression, mode="eval").body))}
            except (SyntaxError, ValueError, ZeroDivisionError, RecursionError):
                return _err("invalid_expression", "Invalid expression.")
        return _err("unknown_action", "Unknown action.")

    def _calc(self, node):
        from decimal import Decimal
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return Decimal(str(node.value))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = self._calc(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](self._calc(node.left), self._calc(node.right))
        raise ValueError("unsupported")
