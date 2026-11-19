"""Read-only view of a public snapshot with evidence keys resolved to file lines.

Only the six published files are opened. Every record keeps the file name and
1-based line number it came from, so the console can point at the exact line.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from pathlib import Path

PUBLIC_FILES = ("manifest.json", "ledger.csv", "invoices.jsonl", "payments.jsonl", "fx.csv", "policy.json")
INT_FIELDS = {"debit_minor", "credit_minor", "amount_minor", "total_minor"}
NULL_MARKER = "null"


@dataclass
class Record:
    key: str                 # public evidence key, e.g. ledger/R7
    table: str               # ledger | payments | invoices | fx | policy
    local_id: str            # R7, D9, 2026-11-15/USD/KZT, allocation
    file: str                # public/ledger.csv
    line: int                # 1-based line number in that file
    raw: str                 # exact text of that line (or JSON fragment for policy)
    fields: dict[str, object] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        return {"key": self.key, "table": self.table, "local_id": self.local_id,
                "file": self.file, "line": self.line, "raw": self.raw, "fields": self.fields}


def _csv_value(name: str, value: str) -> object:
    if value == NULL_MARKER:
        return None
    if name in INT_FIELDS:
        return int(value)
    return value


class Snapshot:
    """Indexes one public/ directory. Never reaches outside it."""

    def __init__(self, public_dir: Path):
        self.public_dir = Path(public_dir)
        self.records: dict[str, Record] = {}
        self.order: list[str] = []
        self.manifest: dict[str, object] = {}
        self.policy: dict[str, object] = {}
        self._load()

    # -- loading -----------------------------------------------------------------
    def _text(self, name: str) -> str:
        if name not in PUBLIC_FILES:
            raise ValueError(f"not a public file: {name}")
        path = self.public_dir / name
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def _add(self, record: Record) -> None:
        self.records[record.key] = record
        self.order.append(record.key)

    def _load(self) -> None:
        manifest_text = self._text("manifest.json")
        self.manifest = json.loads(manifest_text) if manifest_text else {}
        policy_text = self._text("policy.json")
        self.policy = json.loads(policy_text) if policy_text else {}

        ledger_lines = self._text("ledger.csv").splitlines()
        if ledger_lines:
            header = next(csv.reader([ledger_lines[0]]))
            for number, line in enumerate(ledger_lines[1:], start=2):
                if not line.strip():
                    continue
                values = next(csv.reader([line]))
                row = {name: _csv_value(name, value) for name, value in zip(header, values)}
                rid = str(row["row_id"])
                self._add(Record(f"ledger/{rid}", "ledger", rid, "public/ledger.csv", number, line, row))

        for table, name in (("invoices", "invoices.jsonl"), ("payments", "payments.jsonl")):
            for number, line in enumerate(self._text(name).splitlines(), start=1):
                if not line.strip():
                    continue
                doc = json.loads(line)
                did = str(doc["doc_id"])
                self._add(Record(f"{table}/{did}", table, did, f"public/{name}", number, line, doc))

        fx_lines = self._text("fx.csv").splitlines()
        if fx_lines:
            header = next(csv.reader([fx_lines[0]]))
            for number, line in enumerate(fx_lines[1:], start=2):
                if not line.strip():
                    continue
                row = dict(zip(header, next(csv.reader([line]))))
                lid = f"{row['date']}/{row['source_currency']}/{row['target_currency']}"
                self._add(Record(f"fx/{lid}", "fx", lid, "public/fx.csv", number, line, row))

        policy_lines = policy_text.splitlines()
        for rule, value in self.policy.items():
            needle = f'"{rule}"'
            number = next((i for i, text in enumerate(policy_lines, start=1) if text.strip().startswith(needle)), 1)
            raw = json.dumps({rule: value}, ensure_ascii=False)
            fields = value if isinstance(value, dict) else {"rule": value}
            self._add(Record(f"policy/{rule}", "policy", rule, "public/policy.json", number, raw, dict(fields)))

    # -- lookups -----------------------------------------------------------------
    def get(self, key: str) -> Record | None:
        return self.records.get(key)

    def by_table(self, table: str) -> list[Record]:
        return [self.records[k] for k in self.order if self.records[k].table == table]

    def payment(self, payment_id: str) -> Record | None:
        return next((r for r in self.by_table("payments") if r.fields.get("payment_id") == payment_id), None)

    def invoice(self, invoice_id: str) -> Record | None:
        return next((r for r in self.by_table("invoices") if r.fields.get("invoice_id") == invoice_id), None)

    def resolve_affected(self, affected_id: str) -> Record | None:
        """payment/P9 -> payments/D9, invoice/I4 -> invoices/D4."""
        kind, _, local = affected_id.partition("/")
        if kind == "payment":
            return self.payment(local)
        if kind == "invoice":
            return self.invoice(local)
        return self.get(affected_id)

    def key_for_action(self, action: dict[str, object]) -> str | None:
        """Map an inspection action to the evidence key it opened, if any."""
        name = action.get("action")
        args = action.get("args") or {}
        if not isinstance(args, dict):
            return None
        if name == "get_record":
            key = f"{args.get('table')}/{args.get('id')}"
            return key if key in self.records else None
        if name == "get_document":
            doc = str(args.get("doc_id"))
            for table in ("payments", "invoices"):
                if f"{table}/{doc}" in self.records:
                    return f"{table}/{doc}"
        return None

    def file_lines(self, name: str) -> list[str]:
        return self._text(name).splitlines()

    def summary(self) -> dict[str, object]:
        return {"manifest": self.manifest,
                "tables": {t: len(self.by_table(t)) for t in ("ledger", "invoices", "payments", "fx", "policy")}}


def diff_snapshots(left: Snapshot, right: Snapshot) -> list[dict[str, object]]:
    """Field-level differences between two public snapshots, keyed by evidence key."""
    changes: list[dict[str, object]] = []
    keys = list(dict.fromkeys(left.order + right.order))
    for key in keys:
        a, b = left.get(key), right.get(key)
        if a is None or b is None:
            changes.append({"key": key, "field": None, "before": a and a.raw, "after": b and b.raw})
            continue
        for name in dict.fromkeys(list(a.fields) + list(b.fields)):
            if a.fields.get(name) != b.fields.get(name):
                changes.append({"key": key, "field": name, "before": a.fields.get(name), "after": b.fields.get(name),
                                "line_before": a.line, "line_after": b.line})
    for name in sorted(set(left.manifest) | set(right.manifest)):
        if name != "episode_id" and left.manifest.get(name) != right.manifest.get(name):
            changes.append({"key": "manifest", "field": name, "before": left.manifest.get(name),
                            "after": right.manifest.get(name)})
    return changes


def ledger_csv(rows: list[dict[str, object]], header: list[str]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([NULL_MARKER if row.get(h) is None else row.get(h) for h in header])
    return out.getvalue()
