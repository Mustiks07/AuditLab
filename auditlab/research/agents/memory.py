"""Structured investigation memory of the LLM auditor and the public reference index.

Notes are facts, references, open questions and testable hypotheses the MODEL writes through local
tools; they are not hidden chains of thought. The only automatic judgment is about references: a note
or a finding citing an evidence key the agent has not yet seen in a public observation of THIS
episode is marked unverified. Nothing here computes a financial answer.
"""
from __future__ import annotations

import json

NOTE_KINDS = ("fact", "question", "hypothesis")
NOTE_STATUSES = ("open", "confirmed", "rejected")
MAX_NOTES = 200
MAX_TEXT = 600


class ReferenceIndex:
    """Evidence keys (`table/id`) and affected ids the agent has actually received from the environment."""

    def __init__(self):
        self.evidence: set[str] = set()
        self.affected: set[str] = set()

    def _record(self, table: str, record: object) -> None:
        if not isinstance(record, dict):
            return
        key = None
        if table == "ledger" and isinstance(record.get("row_id"), str):
            key = record["row_id"]
        elif table in ("invoices", "payments") and isinstance(record.get("doc_id"), str):
            key = record["doc_id"]
        elif table == "fx" and all(isinstance(record.get(k), str) for k in ("date", "source_currency", "target_currency")):
            key = f"{record['date']}/{record['source_currency']}/{record['target_currency']}"
        elif table == "policy" and isinstance(record.get("id"), str):
            key = record["id"]
        if key:
            self.evidence.add(f"{table}/{key}")
        if isinstance(record.get("invoice_id"), str):
            self.affected.add(f"invoice/{record['invoice_id']}")
        if isinstance(record.get("payment_id"), str):
            self.affected.add(f"payment/{record['payment_id']}")

    def observe(self, action: dict | None, observation: dict) -> None:
        """Update from one official action and the observation the environment returned for it."""
        if not action or not isinstance(observation, dict) or observation.get("error") is not None:
            return
        name, args, result = action.get("name"), action.get("args") or {}, observation.get("result")
        if name == "list_records" and isinstance(result, dict):
            for record in result.get("records") or []:
                self._record(str(args.get("table")), record)
        elif name == "get_record" and result is not None:
            table, ident = str(args.get("table")), args.get("id")
            if isinstance(ident, str):
                self.evidence.add(f"{table}/{ident}")
            if isinstance(result, dict):
                self._record(table, result)
        elif name == "get_document" and isinstance(result, dict):
            self._record("payments" if "payment_id" in result else "invoices", result)


class InvestigationMemory:
    """The model's own structured notes; ids are stable integers in creation order."""

    def __init__(self, references: ReferenceIndex):
        self.notes: list[dict] = []
        self._refs = references

    def _check_refs(self, evidence_ids: object) -> tuple[list[str], list[str]]:
        ids = [value for value in evidence_ids if isinstance(value, str)] if isinstance(evidence_ids, list) else []
        return ids, [value for value in ids if value not in self._refs.evidence]

    def add(self, kind: str, payload: dict) -> dict:
        if kind not in NOTE_KINDS:
            return {"ok": False, "error": f"kind must be one of {NOTE_KINDS}"}
        if len(self.notes) >= MAX_NOTES:
            return {"ok": False, "error": f"memory is full ({MAX_NOTES} notes); update or reject old notes"}
        text = str(payload.get("text", ""))[:MAX_TEXT]
        if not text.strip():
            return {"ok": False, "error": "text is required"}
        evidence, unseen = self._check_refs(payload.get("evidence_ids", []))
        note = {"id": len(self.notes) + 1, "kind": kind, "status": "open", "text": text, "evidence_ids": evidence,
                "unseen_evidence_ids": unseen}
        if kind == "hypothesis":
            for key in ("type", "affected_ids", "claimed_amount_minor", "currency"):
                if key in payload:
                    note[key] = payload[key]
        self.notes.append(note)
        return {"ok": True, "note_id": note["id"], "unseen_evidence_ids": unseen}

    def update(self, note_id: object, status: object, comment: object = "") -> dict:
        if not isinstance(note_id, int) or not 1 <= note_id <= len(self.notes):
            return {"ok": False, "error": "unknown note_id"}
        if status not in NOTE_STATUSES:
            return {"ok": False, "error": f"status must be one of {NOTE_STATUSES}"}
        note = self.notes[note_id - 1]
        note["status"] = status
        if comment:
            note["comment"] = str(comment)[:MAX_TEXT]
        return {"ok": True, "note_id": note_id, "status": status}

    def render(self) -> str:
        """Compact state shown to the model after every environment result."""
        if not self.notes:
            return "Investigation notes: (none yet)"
        lines = ["Investigation notes (id kind status: text [evidence]):"]
        for note in self.notes:
            extra = ""
            if note["kind"] == "hypothesis":
                extra = " " + json.dumps({k: note[k] for k in ("type", "affected_ids", "claimed_amount_minor", "currency")
                                          if k in note}, separators=(",", ":"))
            unseen = f" UNSEEN:{note['unseen_evidence_ids']}" if note["unseen_evidence_ids"] else ""
            comment = f" ({note['comment']})" if note.get("comment") else ""
            lines.append(f"{note['id']} {note['kind']} {note['status']}: {note['text']}{extra} "
                         f"{note['evidence_ids']}{unseen}{comment}")
        return "\n".join(lines)

    def snapshot(self) -> list[dict]:
        return json.loads(json.dumps(self.notes))
