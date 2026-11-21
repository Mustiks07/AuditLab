"""Trusted evidence availability: is a cited key reachable through the declared public tools?

A finding may cite more than a minimal evidence set, but every cited key must exist in THIS
episode (Genesis fintech-checks 2026-11-20, "evidence_ids"; integrator review defect 2). The
reference list in truth is not enough: an available extra record need not belong to any set.

Keys follow the published citation form ``<table>/<lookup id>``: ``ledger/ROW``,
``invoices/DOC``, ``payments/DOC``, ``fx/DATE/SOURCE/TARGET``, ``policy/RULE``. A key is
available exactly when ``get_record(table, id)`` succeeds on the episode's public session,
so availability can never differ from what a policy could actually read.
"""
from __future__ import annotations

from pathlib import Path

EVIDENCE_TABLES = ("ledger", "invoices", "payments", "fx", "policy")


class PublicEvidence:
    """Membership test over the public snapshot. Reads public files only, never truth."""

    def __init__(self, public_dir: str | Path, session_opener=None):
        if session_opener is None:
            from auditlab.environment import open_session as session_opener
        self._session = session_opener(Path(public_dir))
        self._known: dict[str, bool] = {}

    def __contains__(self, key: object) -> bool:
        if type(key) is not str:
            return False
        if key not in self._known:
            table, separator, ident = key.partition("/")
            available = bool(separator) and bool(ident) and table in EVIDENCE_TABLES
            if available:
                reply = self._session.inspect({"action": "get_record", "args": {"table": table, "id": ident}})
                available = isinstance(reply, dict) and reply.get("ok") is True
            self._known[key] = available
        return self._known[key]
