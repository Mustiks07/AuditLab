"""External API-05 adapter: Python standard library + the published schema files only.

Run as a policy process (one observation per stdin line, one action per stdout line):
    python -I schema_adapter.py SCHEMA_DIR REPORT_PATH
SCHEMA_DIR is schemas/public/1.0; the validator is loaded by file path from SCHEMA_DIR/../../validate.py.
It imports no AuditLab module (python -I also drops PYTHONPATH and the current directory), reads the
golden JSON itself, validates every message it receives and every action it sends, exercises all five
actions plus bounded errors, and writes its findings to REPORT_PATH before the final submit.
"""
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

SCHEMA_DIR = Path(sys.argv[1])
REPORT = Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("published_validator", SCHEMA_DIR.parent.parent / "validate.py")
published = importlib.util.module_from_spec(spec)
spec.loader.exec_module(published)
VALIDATOR = published.Validator.from_path(SCHEMA_DIR / "auditlab-public.schema.json")
CLOSED_KEYS = {"truth", "issues", "issue_id", "evidence_sets", "expected_amount_minor", "provenance", "seed",
               "true_reward", "score", "private", "mutation", "mutations"}

report = {"messages_validated": 0, "actions_validated": 0, "failures": [], "pointers": {},
          "golden_valid": 0, "golden_invalid": 0, "golden_mismatches": []}


def fail(text):
    report["failures"].append(text)


def check(instance, pointer, what):
    problems = VALIDATOR.errors(instance, pointer)
    report["pointers"][pointer] = report["pointers"].get(pointer, 0) + 1
    if problems:
        fail(f"{what} vs {pointer}: {problems[:3]}")


def closed_keys(value, path="$"):
    """Independent second look for closed data, not relying on the schema."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in CLOSED_KEYS:
                fail(f"closed key {key!r} at {path}")
            closed_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            closed_keys(child, f"{path}[{index}]")


def receive(pointer):
    observation = published.load_json(sys.stdin.readline())
    report["messages_validated"] += 1
    check(observation, pointer, "observation")
    closed_keys(observation)
    return observation


def send(action, strict=True):
    report["actions_validated"] += 1
    if strict:
        check(action, "#/$defs/action", "own action")
    else:
        check(action, "#/$defs/actionEnvelope", "own deliberately invalid action")
        if VALIDATOR.is_valid(action, "#/$defs/action"):
            fail(f"deliberately invalid action accepted by #/$defs/action: {action}")
    print(json.dumps(action), flush=True)


def call(action, reply_pointer, strict=True):
    send(action, strict)
    return receive(reply_pointer)


def golden():
    for kind, expect in (("valid", True), ("invalid", False)):
        for entry in json.loads((SCHEMA_DIR / "examples" / f"{kind}.json").read_text(encoding="utf-8")):
            report[f"golden_{kind}"] += 1
            if VALIDATOR.is_valid(entry["instance"], entry["pointer"]) is not expect:
                report["golden_mismatches"].append(entry["name"])


golden()
initial = receive("#/$defs/initialObservation")
episode_id = initial["manifest"]["episode_id"]
page = call({"action": "list_records", "args": {"table": "payments", "cursor": None, "limit": 50}},
            "#/$defs/replyListPayments")["result"]
payment = page["records"][0]
document = call({"action": "get_document", "args": {"doc_id": payment["doc_id"]}}, "#/$defs/replyGetDocument")["result"]
if document != payment:
    fail("get_document differs from the listed payment")
call({"action": "get_record", "args": {"table": "policy", "id": "allocation"}}, "#/$defs/replyGetPolicy")
credit = debit = 0
evidence = ["payments/" + payment["doc_id"]]
for row_id in payment["settlement_index"]["unapplied_row_ids"]:
    row = call({"action": "get_record", "args": {"table": "ledger", "id": row_id}}, "#/$defs/replyGetLedger")["result"]
    credit, debit = credit + row["credit_minor"], debit + row["debit_minor"]
    evidence.append("ledger/" + row_id)
call({"action": "list_records", "args": {"table": "fx", "cursor": None, "limit": 50}}, "#/$defs/replyListFx")
amount = Decimal(call({"action": "calculator", "args": {"expression": f"{credit}-{debit}"}}, "#/$defs/replyCalculator")["result"])
if amount != credit - debit:
    fail("calculator disagrees with integer arithmetic")
missing = call({"action": "get_document", "args": {"doc_id": "ADAPTER-MISSING"}}, "#/$defs/errorObservation")
if missing["error"]["code"] != "not_found":
    fail(f"expected not_found, got {missing['error']}")
limit = call({"action": "list_records", "args": {"table": "ledger", "cursor": None, "limit": 0}},
             "#/$defs/errorObservation", strict=False)
if limit["error"]["code"] != "invalid_args":
    fail(f"expected invalid_args, got {limit['error']}")
# Policy input may carry any key (PR #22 review); the reply must not echo it (closed_keys() checks).
seeded = call({"action": "calculator", "args": {"expression": "1+1", "seed": 123}}, "#/$defs/errorObservation",
              strict=False)
if seeded["error"]["code"] != "invalid_args":
    fail(f"expected invalid_args for an extra argument, got {seeded['error']}")
findings = []
if amount > 0:
    findings = [{"type": "payment_allocation",
                 "affected_ids": ["payment/" + payment["payment_id"], "invoice/" + payment["invoice_refs"][0]],
                 "claimed_amount_minor": int(amount), "currency": payment["currency"],
                 "evidence_ids": evidence + ["policy/allocation"],
                 "explanation": "Settlement index credits minus debits before the exclusive boundary."}]
submission = {"episode_id": episode_id, "findings": findings}
check(submission, "#/$defs/submission", "own submission")
report["imported_auditlab"] = sorted(name for name in sys.modules if name == "auditlab" or name.startswith("auditlab."))
REPORT.write_text(json.dumps(report, indent=1, sort_keys=True), encoding="utf-8")
send({"action": "submit", "args": submission})
