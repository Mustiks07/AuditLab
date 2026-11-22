"""LLM auditor: the model chooses every official action and the moment of submit.

Interface (CONTRACTS.md): start(public_observation, agent_config), act(public_observation) ->
official_action, close(). The controller calls env.step itself; the next act receives the real
observation. The agent never sees truth, provenance, paths or seed: only what the official adapter
returns to an ordinary policy.

Two variants on the same model, prompt and tools:
* "tools"  - the five official actions only;
* "memory" - plus local note tools (facts with references, open questions, hypotheses) whose state
  is shown after every environment result. Notes cost model calls and time, never environment actions.

Both variants share one pre-submit check of FORMAT and REFERENCES (Genesis structure limits, the
episode id, evidence keys already seen in this episode). Problems are returned to the model as a tool
error; the model decides what to submit. The check never fills in or corrects a financial value.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from typing import Callable

from auditlab.research.agents.memory import InvestigationMemory, ReferenceIndex
from auditlab.research.agents.providers import Provider, ProviderError, ProviderRequest, ToolSpec
from auditlab.research.agents.reliability import ModelCaller, RetryPolicy, SpendGuard

AGENT_VERSION = "1.0"
PROMPT_VERSION = "llm-auditor-prompt-1.0"
VARIANTS = ("tools", "memory")
ENV_TOOLS = ("list_records", "get_record", "get_document", "calculator", "submit")
MEMORY_TOOLS = ("note", "update_note")
ISSUE_TYPES = ("duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff")
TABLES = ("ledger", "invoices", "payments", "fx", "policy")

SYSTEM_PROMPT = """You are a financial auditor working inside a simulated accounting environment. One company, one reporting period. Find the financial errors that are really present in the books and submit them with evidence, or submit no findings if the books are clean.

You act only through the provided tools. Every call of list_records, get_record, get_document, calculator and submit is one environment action: at most 100 actions in total including submit, and the episode also has a wall-clock limit. Exploration actions lower the final reward slightly (0.002 per action), but a wrong or missing finding costs far more than a few extra reads. Submit exactly once; after submit the episode ends.

Data: ledger rows (row_id, timestamp, account, debit_minor, credit_minor, currency, entity_id, invoice_id, payment_id, journal_id), invoices and payments documents (doc_id), fx rates (date/SOURCE/TARGET, decimal strings) and policy rules (period boundary, fx date choice, rounding, allocation). Money is in integer minor units of the stated currency. Use the calculator for every arithmetic step; do not do arithmetic in your head. Different currencies are never compared without conversion under the policy.

Error types: duplicate_invoice (one obligation recognised twice), payment_allocation (a designated payment missing or only partly applied by the reporting boundary), fx_conversion (wrong date or rate used for a conversion under the policy), period_cutoff (recognised in the wrong reporting period). Correct look-alikes exist: split payments, refunds, legitimate repeated amounts, archive copies, boundary cases that the policy allows.

A finding is {type, affected_ids, claimed_amount_minor, currency, evidence_ids, explanation}. affected_ids use invoice/<invoice_id> and payment/<payment_id>. evidence_ids cite records you actually read: ledger/<row_id>, invoices/<doc_id>, payments/<doc_id>, fx/<date>/<SOURCE>/<TARGET>, policy/<rule>. Cite every record needed to verify the finding and nothing you have not seen. claimed_amount_minor is the size of the discrepancy, an integer, in the currency you state. explanation is a short justification (under 1000 characters), not your reasoning process. Limits: 30 findings, 10 evidence ids per finding. False accusations are penalised as much as missed errors; a duplicate of a finding gains nothing."""

MEMORY_PROMPT = """

You also have an investigation notebook (tools note and update_note) that costs no environment actions. Record confirmed facts with the evidence ids they rest on, open questions you still need to check, and hypotheses about possible findings with type, affected ids, amount and currency. Mark a hypothesis confirmed or rejected once you have checked it with evidence. The notebook is shown to you after every environment result; base your final submission on confirmed hypotheses."""


def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


FINDING_SCHEMA = _schema({
    "type": {"type": "string", "description": "One of: " + ", ".join(ISSUE_TYPES)},
    "affected_ids": {"type": "array", "items": {"type": "string"}},
    "claimed_amount_minor": {"type": "integer"},
    "currency": {"type": "string"},
    "evidence_ids": {"type": "array", "items": {"type": "string"}},
    "explanation": {"type": "string"},
}, ["type", "affected_ids", "claimed_amount_minor", "currency", "evidence_ids", "explanation"])

ENV_TOOL_SPECS = [
    ToolSpec("list_records", "Read one page (at most 50 rows) of a public table. Start with cursor null and pass "
             "next_cursor to continue. One environment action.",
             _schema({"table": {"type": "string", "enum": list(TABLES)},
                      "cursor": {"type": ["string", "null"]},
                      "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, ["table", "cursor", "limit"])),
    ToolSpec("get_record", "Read one record by its lookup id (ledger row_id, invoices/payments doc_id, fx "
             "DATE/SOURCE/TARGET, policy rule name). One environment action.",
             _schema({"table": {"type": "string", "enum": list(TABLES)}, "id": {"type": "string"}}, ["table", "id"])),
    ToolSpec("get_document", "Read one invoice or payment document by doc_id. One environment action.",
             _schema({"doc_id": {"type": "string"}}, ["doc_id"])),
    ToolSpec("calculator", "Exact decimal arithmetic (+ - * / and parentheses on decimal numbers). Returns a "
             "decimal string. One environment action.",
             _schema({"expression": {"type": "string"}}, ["expression"])),
    ToolSpec("submit", "Submit the final findings (possibly an empty list) and end the episode. Counts as an "
             "environment action. Use the episode_id from the manifest.",
             _schema({"episode_id": {"type": "string"},
                      "findings": {"type": "array", "items": FINDING_SCHEMA}}, ["episode_id", "findings"])),
]

MEMORY_TOOL_SPECS = [
    ToolSpec("note", "Add a note to your investigation notebook. No environment action.",
             _schema({"kind": {"type": "string", "enum": ["fact", "question", "hypothesis"]},
                      "text": {"type": "string"},
                      "evidence_ids": {"type": "array", "items": {"type": "string"}},
                      "type": {"type": ["string", "null"]},
                      "affected_ids": {"type": ["array", "null"], "items": {"type": "string"}},
                      "claimed_amount_minor": {"type": ["integer", "null"]},
                      "currency": {"type": ["string", "null"]}},
                     ["kind", "text", "evidence_ids", "type", "affected_ids", "claimed_amount_minor", "currency"])),
    ToolSpec("update_note", "Change the status of a note (open, confirmed, rejected). No environment action.",
             _schema({"note_id": {"type": "integer"}, "status": {"type": "string", "enum": ["open", "confirmed", "rejected"]},
                      "comment": {"type": "string"}}, ["note_id", "status", "comment"])),
]


def tool_specs(variant: str) -> list[ToolSpec]:
    if variant not in VARIANTS:
        raise ValueError(f"variant must be one of {VARIANTS}")
    return ENV_TOOL_SPECS + (MEMORY_TOOL_SPECS if variant == "memory" else [])


def system_prompt(variant: str, prompt_variant: str = "A") -> str:
    from auditlab.research.agents.prompts import addition
    return SYSTEM_PROMPT + addition(prompt_variant) + (MEMORY_PROMPT if variant == "memory" else "")


def prompt_hash(variant: str, provider_description: dict, max_tokens: int, prompt_variant: str = "A") -> str:
    payload = {"prompt_version": PROMPT_VERSION, "system": system_prompt(variant, prompt_variant),
               "tools": [(t.name, t.description, t.input_schema) for t in tool_specs(variant)],
               "provider": provider_description, "max_tokens": max_tokens}
    if prompt_variant != "A":  # variant A keeps the exact hash of the original auditor (pilot comparability)
        from auditlab.research.agents.prompts import PROMPT_SET_VERSION
        payload["prompt_variant"] = [PROMPT_SET_VERSION, prompt_variant]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def precheck_submission(body: object, episode_id: str, refs: ReferenceIndex) -> list[str]:
    """Format and reference problems only (never financial content). Empty list = nothing to report."""
    if not isinstance(body, dict) or set(body) != {"episode_id", "findings"}:
        return ["submit needs exactly {episode_id, findings}"]
    problems = []
    if body["episode_id"] != episode_id:
        problems.append(f"episode_id must be {episode_id!r} from the manifest")
    findings = body["findings"]
    if not isinstance(findings, list):
        return problems + ["findings must be a list"]
    if len(findings) > 30:
        problems.append(f"{len(findings)} findings; the limit is 30")
    keys = set(FINDING_SCHEMA["properties"])
    for index, finding in enumerate(findings):
        where = f"finding {index}"
        if not isinstance(finding, dict) or set(finding) != keys:
            problems.append(f"{where}: needs exactly the fields {sorted(keys)}")
            continue
        if finding["type"] not in ISSUE_TYPES:
            problems.append(f"{where}: type {finding['type']!r} is not a documented error type")
        amount = finding["claimed_amount_minor"]
        if type(amount) is not int:
            problems.append(f"{where}: claimed_amount_minor must be an integer")
        for field in ("affected_ids", "evidence_ids"):
            if not isinstance(finding[field], list) or not all(isinstance(v, str) for v in finding[field]):
                problems.append(f"{where}: {field} must be a list of strings")
        if isinstance(finding["evidence_ids"], list):
            if len(finding["evidence_ids"]) > 10:
                problems.append(f"{where}: {len(finding['evidence_ids'])} evidence ids; the limit is 10 (repeats count)")
            unseen = [v for v in finding["evidence_ids"] if isinstance(v, str) and v not in refs.evidence]
            if unseen:
                problems.append(f"{where}: evidence ids not read in this episode: {unseen}")
        if not isinstance(finding["explanation"], str) or len(finding["explanation"]) > 1000:
            problems.append(f"{where}: explanation must be a string of at most 1000 characters")
    return problems


class AgentFailure(RuntimeError):
    """The model could not produce an admissible action. `kind` mirrors attempt statuses."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class LLMAuditor:
    """One episode of the LLM auditor. Not thread-safe; create one per attempt."""

    def __init__(self, provider: Provider, variant: str, *, max_tokens: int = 16000,
                 max_model_calls_per_action: int = 12, max_prechecks: int = 2, max_nudges: int = 2,
                 retry: RetryPolicy | None = None, guard: SpendGuard | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 prompt_variant: str = "A"):
        if variant not in VARIANTS:
            raise ValueError(f"variant must be one of {VARIANTS}")
        self.provider, self.variant, self.max_tokens = provider, variant, max_tokens
        self.limits = {"max_model_calls_per_action": max_model_calls_per_action, "max_prechecks": max_prechecks,
                       "max_nudges": max_nudges}
        self.clock = clock
        self.caller = ModelCaller(provider, retry or RetryPolicy(), guard or SpendGuard(None), clock=clock, sleep=sleep)
        self.refs = ReferenceIndex()
        self.memory = InvestigationMemory(self.refs) if variant == "memory" else None
        self.transcript: list[dict] = []
        self.prompt_variant = prompt_variant
        self.system = system_prompt(variant, prompt_variant)
        self.tools = tool_specs(variant)
        self.prompt_hash = prompt_hash(variant, provider.describe(), max_tokens, prompt_variant)
        self._pending: dict | None = None  # {"call_id", "action"} awaiting the environment's answer
        self._episode_id: str | None = None
        self._prechecks = 0
        self._deadline = float("inf")
        self._emitted = 0  # transcript entries already reported in telemetry

    # ------------------------------------------------------------------ interface
    def describe(self) -> dict:
        return {"agent_version": AGENT_VERSION, "prompt_version": PROMPT_VERSION, "variant": self.variant,
                "prompt_variant": self.prompt_variant,
                "prompt_hash": self.prompt_hash, "max_tokens": self.max_tokens, "limits": self.limits,
                "provider": self.provider.describe(), "test_double": bool(self.provider.is_test_double)}

    def prompt_record(self) -> dict:
        return {"system": self.system, "tools": [{"name": t.name, "description": t.description,
                                                  "input_schema": t.input_schema} for t in self.tools],
                "prompt_hash": self.prompt_hash, "prompt_version": PROMPT_VERSION}

    def start(self, public_observation: dict, agent_config: dict | None = None) -> None:
        manifest = public_observation.get("manifest") or {}
        self._episode_id = manifest.get("episode_id")
        text = ("Episode start. Public observation from the environment:\n"
                + json.dumps(public_observation, ensure_ascii=False, sort_keys=True))
        self.transcript = [{"role": "user", "text": text}]

    def set_deadline(self, seconds_left: float) -> None:
        self._deadline = self.clock() + max(0.0, seconds_left)

    def act(self, public_observation: dict) -> dict:
        if self._pending is not None:
            self.refs.observe(self._pending["action"], public_observation)
            content = json.dumps(public_observation, ensure_ascii=False, sort_keys=True)
            note = self.memory.render() if self.memory is not None else None
            self.transcript.append({"role": "tool_results", "results": [
                {"id": self._pending["call_id"], "content": content,
                 "is_error": public_observation.get("error") is not None}], "text": note})
            self._pending = None
        elif not self.transcript:
            self.start(public_observation)
        nudges = 0
        for _ in range(self.limits["max_model_calls_per_action"]):
            response = self.caller.call(ProviderRequest(self.system, copy.deepcopy(self.transcript), self.tools,
                                                        self.max_tokens, 0.0), self._deadline)
            self.transcript.append({"role": "assistant", "native": response.native, "text": response.text,
                                    "tool_calls": [{"id": c.id, "name": c.name, "input": c.input}
                                                   for c in response.tool_calls]})
            if response.stop_reason == "refusal":
                raise AgentFailure("provider_refusal", "the model refused the request")
            if not response.tool_calls:
                if response.stop_reason == "max_tokens":
                    raise AgentFailure("invalid_response", "the model hit max_tokens without choosing an action")
                nudges += 1
                if nudges > self.limits["max_nudges"]:
                    raise AgentFailure("invalid_response", "the model stopped without choosing an action")
                self.transcript.append({"role": "user", "text": "Call exactly one tool to continue the audit; "
                                                                "use submit when you are done."})
                continue
            call = response.tool_calls[0]
            local = self._local_tool(call)
            if local is not None:
                self.transcript.append({"role": "tool_results", "results": [
                    {"id": call.id, "content": json.dumps(local, ensure_ascii=False), "is_error": not local.get("ok", True)}],
                    "text": None})
                continue
            if call.name not in ENV_TOOLS:
                action = {"name": call.name, "args": call.input}  # the environment answers unknown_action
            elif call.name == "submit":
                problems = precheck_submission(call.input, self._episode_id, self.refs)
                if problems and self._prechecks < self.limits["max_prechecks"]:
                    self._prechecks += 1
                    self.transcript.append({"role": "tool_results", "results": [
                        {"id": call.id, "is_error": True, "content": json.dumps(
                            {"precheck": "not submitted, no environment action used", "problems": problems,
                             "note": "Fix the problems or submit again unchanged; the decision is yours."})}],
                        "text": None})
                    continue
                action = {"name": "submit", "args": {"findings": call.input}}
            else:
                action = {"name": call.name, "args": call.input}
            self._pending = {"call_id": call.id, "action": action}
            return action
        raise AgentFailure("invalid_response", "too many model calls without an environment action")

    def _local_tool(self, call) -> dict | None:
        if call.name not in MEMORY_TOOLS:
            return None
        if self.memory is None:
            return {"ok": False, "error": "this tool is not available"}
        args = call.input if isinstance(call.input, dict) else {}
        if call.name == "note":
            payload = {k: v for k, v in args.items() if v is not None and k != "kind"}
            return self.memory.add(str(args.get("kind")), payload)
        return self.memory.update(args.get("note_id"), args.get("status"), args.get("comment", ""))

    def drain_telemetry(self) -> dict:
        """Provider tries and transcript entries produced since the previous drain (public data only)."""
        calls, self.caller.records = self.caller.records, []
        delta, self._emitted = self.transcript[self._emitted:], len(self.transcript)
        return {"provider_calls": calls, "transcript": copy.deepcopy(delta),
                "memory": self.memory.snapshot() if self.memory is not None else None,
                "spent_usd": self.caller.guard.spent_usd}

    def close(self) -> None:
        self._pending = None
