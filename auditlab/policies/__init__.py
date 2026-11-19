"""Ordinary audit policies that operate on runner observations and public tools."""

from collections import defaultdict, deque
from itertools import islice
from random import Random
from typing import Iterator, TypedDict, cast

from auditlab.contracts import MAX_FINDINGS, PAGE_LIMIT, Action, Finding, Observation, Policy

from ._reconcile import Record, reconcile

POLICY_VERSION = "1.1"

__all__ = ["POLICY_VERSION", "make_policy"]


class _ListPage(TypedDict):
    records: list[Record]
    next_cursor: str | None


def _page(table: str, cursor: str | None = None, limit: int = PAGE_LIMIT) -> Action:
    return {"action": "list_records", "args": {"table": table, "cursor": cursor, "limit": limit}}


def _submit(manifest: Record, findings: list[Finding]) -> Action:
    return {"action": "submit", "args": {"episode_id": manifest["episode_id"], "findings": findings}}


def _suspect(document: Record) -> Finding:
    if "payment_id" in document:
        return {
            "type": "payment_allocation",
            "affected_ids": [f"payment/{document['payment_id']}",
                             *(f"invoice/{ref}" for ref in document["invoice_refs"])],
            "claimed_amount_minor": abs(document["amount_minor"]),
            "currency": document["currency"],
            "evidence_ids": [f"payments/{document['doc_id']}"],
            "explanation": "Possible unapplied payment based on its stated amount.",
        }
    return {
        "type": "duplicate_invoice",
        "affected_ids": [f"invoice/{document['invoice_id']}"],
        "claimed_amount_minor": abs(document["total_minor"]),
        "currency": document["currency"],
        "evidence_ids": [f"invoices/{document['doc_id']}"],
        "explanation": "Possible repeated recognition based on the invoice amount.",
    }


def _flag_all(records: dict[str, list[Record]]) -> Iterator[Finding]:
    seen = set()
    for table, id_field in (("payments", "payment_id"), ("invoices", "invoice_id")):
        for document in records.get(table, []):
            key = table, document[id_field]
            if key not in seen:
                seen.add(key)
                yield _suspect(document)


class _ScanPolicy:
    def __init__(self, name: str):
        self._name = name
        tables = ("policy", "payments", "invoices", "ledger", "fx") if name == "reconcile" else (
            "payments", "invoices"
        )
        self._tables = deque(tables)
        self._complete: set[str] = set()
        self._records: dict[str, list[Record]] = defaultdict(list)
        self._manifest: Record = {}
        self._cursor: str | None = None

    def act(self, public_observation: Observation) -> Action:
        if "manifest" in public_observation:
            self._manifest = cast(Record, public_observation["manifest"])
        elif self._tables:
            table = self._tables[0]
            result = public_observation.get("result") if public_observation.get("ok") else None
            self._cursor = None
            if isinstance(result, dict):
                page = cast(_ListPage, result)
                self._records[table].extend(page["records"])
                self._cursor = page["next_cursor"]
                if self._cursor is None:
                    self._complete.add(table)
            if self._cursor is None:
                self._tables.popleft()

        if self._tables and cast(int, public_observation["remaining_steps"]) > 1:
            return _page(self._tables[0], self._cursor)
        findings = (reconcile(self._manifest, self._records, self._complete)
                    if self._name == "reconcile" else _flag_all(self._records))
        return _submit(self._manifest, list(islice(findings, MAX_FINDINGS)))


class _RandomPolicy:
    def __init__(self, seed: int):
        self._random = Random(seed)
        self._manifest: Record = {}
        self._stop_at = 1
        self._last_table: str | None = None
        self._cursors: dict[str, str | None] = {}
        self._documents: dict[str, Record] = {}

    def act(self, public_observation: Observation) -> Action:
        remaining = cast(int, public_observation["remaining_steps"])
        if "manifest" in public_observation:
            self._manifest = cast(Record, public_observation["manifest"])
            self._stop_at = max(1, remaining - self._random.randint(4, 12))
        elif self._last_table and public_observation.get("ok"):
            result = cast(_ListPage, public_observation["result"])
            self._cursors[self._last_table] = result["next_cursor"]
            if self._last_table in {"invoices", "payments"}:
                self._documents.update((document["doc_id"], document) for document in result["records"])

        if remaining <= self._stop_at:
            count = self._random.randint(0, min(3, len(self._documents)))
            choices = self._random.sample(list(self._documents.values()), count)
            return _submit(self._manifest, [_suspect(document) for document in choices])

        choices = ["ledger", "invoices", "payments", "fx", "policy", "calculator"]
        if self._documents:
            choices.append("document")
        choice = self._random.choice(choices)
        self._last_table = None
        if choice == "calculator":
            left, right = self._random.sample(range(100), 2)
            return {"action": "calculator", "args": {"expression": f"{left}+{right}"}}
        if choice == "document":
            doc_id = self._random.choice(list(self._documents))
            return {"action": "get_document", "args": {"doc_id": doc_id}}
        self._last_table = choice
        return _page(choice, self._cursors.get(choice), self._random.randint(1, PAGE_LIMIT))


def make_policy(name: str, seed: int) -> Policy:
    """Create a policy with fresh state for a single episode."""
    if name == "random":
        return _RandomPolicy(seed)
    if name in {"reconcile", "flag_all"}:
        return _ScanPolicy(name)
    raise ValueError(f"Unknown public policy: {name}")
