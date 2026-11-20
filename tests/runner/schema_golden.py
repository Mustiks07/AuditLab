"""Builds the golden examples of the public wire format from the REAL components.

    python tests/runner/schema_golden.py            # rewrite schemas/public/1.0/examples/*.json
    python tests/runner/schema_golden.py --check    # exit 1 if the committed files differ

valid.json   — messages actually produced by make_env (real open_session, evaluator in its own
               process) for every action, every reachable error code and every way to end an episode,
               in train and closed mode. Each entry names the schema pointer it must satisfy.
invalid.json — the same real messages with one deliberate defect each (missing field, wrong type,
               closed data, non-neutral closed reward, ...). Each must be rejected.
The output is deterministic: no run ids, clocks or temp paths are recorded.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from auditlab.environment import open_session  # noqa: E402
from auditlab.runner import make_env  # noqa: E402

EXAMPLES = ROOT / "schemas" / "public" / "1.0" / "examples"
FIXTURES = ROOT / "fixtures" / "foundation"
FX_SNAPSHOT = ROOT / "datasets" / "open" / "dev" / "E1ADBA2ED9E09A7CE01C5B3A8D7CBE13E"
GOOD_S02 = {"type": "payment_allocation", "affected_ids": ["payment/P9", "invoice/I4"],
            "claimed_amount_minor": 2_000_000, "currency": "KZT",
            "evidence_ids": ["payments/D9", "ledger/R4", "ledger/R5", "policy/allocation"],
            "explanation": "50 000 - 30 000 = 20 000 KZT remain unapplied before the exclusive boundary."}
D9 = {"action": "get_document", "args": {"doc_id": "D9"}}
# Keys an auditor may put into its OWN request. They are policy input, not disclosure (PR #22 review).
FOREIGN = {"seed": 123, "truth": {"issues": [{"issue_id": "X1", "evidence_sets": [["ledger/R4"]]}]},
           "score": {"reward": 1.0}}


def _nest(depth):
    value = 0
    for _ in range(depth - 1):
        value = [value]
    return value


def _deep_submit(total_depth):
    """submit whose whole action object is exactly total_depth levels deep (Issue #23)."""
    return {"action": "submit", "args": {"episode_id": "S02", "findings": _nest(total_depth - 2)}}


def _trajectory_pointer(mode):
    return "#/$defs/trajectoryLineTrain" if mode == "train" else "#/$defs/trajectoryLineClosed"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _step_pointer(mode: str) -> str:
    return "#/$defs/stepTrain" if mode == "train" else "#/$defs/stepClosed"


class Recorder:
    def __init__(self):
        self.entries: list[dict] = []

    def add(self, name: str, pointer: str, instance) -> None:
        self.entries.append({"name": name, "pointer": pointer, "instance": copy.deepcopy(instance)})

    def step(self, env, name: str, mode: str, action, reply_pointer: str | None):
        result = env.step(action)  # the runner keeps its own bounded copy (#23)
        self.add(f"{name}.step", _step_pointer(mode), list(result))
        if reply_pointer:
            self.add(f"{name}.observation", reply_pointer, result[0])
        if isinstance(action, dict) and action.get("action") == "submit" and result[0].get("ok") is True:
            self.add(f"{name}.submission", "#/$defs/submission", action["args"])  # accepted bodies only
        return result


def _transcript(rec: Recorder, label: str, mode: str) -> None:
    prefix = f"{label}.{mode}"
    fresh = make_env(mode)
    result = fresh.step(D9)
    rec.add(f"{prefix}.before_reset.step", _step_pointer(mode), list(result))
    env = make_env(mode)
    observation, info = env.reset(0, {"snapshot_dir": str(FIXTURES / label)})
    rec.add(f"{prefix}.reset", "#/$defs/reset", [observation, info])
    rec.add(f"{prefix}.reset.observation", "#/$defs/initialObservation", observation)
    rec.add(f"{prefix}.reset.manifest", "#/$defs/manifest", observation["manifest"])
    try:
        first = rec.step(env, f"{prefix}.list_ledger_page1", mode,
                         {"action": "list_records", "args": {"table": "ledger", "cursor": None, "limit": 3}},
                         "#/$defs/replyListLedger")
        cursor = first[0]["result"]["next_cursor"]
        rec.step(env, f"{prefix}.list_ledger_page2", mode,
                 {"action": "list_records", "args": {"table": "ledger", "cursor": cursor, "limit": 50}},
                 "#/$defs/replyListLedger")
        for table, pointer in (("invoices", "replyListInvoices"), ("payments", "replyListPayments"),
                               ("fx", "replyListFx"), ("policy", "replyListPolicy")):
            rec.step(env, f"{prefix}.list_{table}", mode,
                     {"action": "list_records", "args": {"table": table, "cursor": None, "limit": 50}},
                     f"#/$defs/{pointer}")
        for table, key, pointer in (("ledger", "R4", "replyGetLedger"), ("invoices", "D4", "replyGetInvoice"),
                                    ("payments", "D9", "replyGetPayment"), ("policy", "allocation", "replyGetPolicy"),
                                    ("policy", "fx_date", "replyGetPolicy")):
            rec.step(env, f"{prefix}.get_{table}_{key}", mode,
                     {"action": "get_record", "args": {"table": table, "id": key}}, f"#/$defs/{pointer}")
        for doc in ("D4", "D9"):
            rec.step(env, f"{prefix}.get_document_{doc}", mode,
                     {"action": "get_document", "args": {"doc_id": doc}}, "#/$defs/replyGetDocument")
        rec.step(env, f"{prefix}.calculator", mode,
                 {"action": "calculator", "args": {"expression": "(50000 - 30000) * 100 / 3"}}, "#/$defs/replyCalculator")
        errors = [
            ("not_found", {"action": "get_document", "args": {"doc_id": "NO-SUCH-DOC"}}),
            ("unknown_table", {"action": "get_record", "args": {"table": "truth", "id": "X1"}}),
            ("invalid_args_limit0", {"action": "list_records", "args": {"table": "ledger", "cursor": None, "limit": 0}}),
            ("invalid_args_limit51", {"action": "list_records", "args": {"table": "ledger", "cursor": None, "limit": 51}}),
            ("invalid_args_bool_limit", {"action": "list_records", "args": {"table": "ledger", "cursor": None, "limit": True}}),
            ("invalid_args_extra", {"action": "get_document", "args": {"doc_id": "D9", "debug": True}}),
            ("invalid_args_type", {"action": "get_record", "args": {"table": "ledger", "id": 4}}),
            ("invalid_cursor_garbage", {"action": "list_records", "args": {"table": "ledger", "cursor": "zz", "limit": 5}}),
            ("invalid_cursor_other_table", {"action": "list_records", "args": {"table": "invoices", "cursor": cursor, "limit": 5}}),
            ("division_by_zero", {"action": "calculator", "args": {"expression": "1/0"}}),
            ("invalid_expression", {"action": "calculator", "args": {"expression": "2*(3"}}),
            ("arithmetic_limit", {"action": "calculator", "args": {"expression": "9" * 40 + "*" + "9" * 40}}),
            ("invalid_request", {"action": "get_document"}),
            ("unknown_action", {"action": "read_truth", "args": {}}),
        ]
        for name, action in errors:
            rec.step(env, f"{prefix}.error_{name}", mode, action, "#/$defs/errorObservation")
        policy_inputs = [
            ("calculator", {"action": "calculator", "args": {"expression": "1+1", "seed": 123}}),
            ("get_document", {"action": "get_document", "args": {"doc_id": "D9", **FOREIGN}}),
            ("get_record", {"action": "get_record", "args": {"table": "ledger", "id": "R4", "truth": FOREIGN["truth"]}}),
            ("list_records", {"action": "list_records", "args": {"table": "ledger", "cursor": None, "limit": 5, "score": 1}}),
            ("unknown_action", {"action": "read_truth", "args": FOREIGN}),
        ]
        for name, action in policy_inputs:
            rec.add(f"{prefix}.policy_input_{name}.action", "#/$defs/actionEnvelope", action)
            rec.step(env, f"{prefix}.policy_input_{name}", mode, action, "#/$defs/errorObservation")
        rec.step(env, f"{prefix}.malformed_action", mode, {"args": {}}, "#/$defs/errorObservation")
        answer = {"episode_id": label, "findings": [] if label == "S01" else [GOOD_S02]}
        rec.step(env, f"{prefix}.submit", mode, {"action": "submit", "args": answer}, "#/$defs/replySubmitAccepted")
        rec.step(env, f"{prefix}.after_finish", mode, D9, "#/$defs/protocolObservation")
        for row in env._trajectory:
            rec.add(f"{prefix}.trajectory.{row['step']:02d}",
                    "#/$defs/trajectoryLineTrain" if mode == "train" else "#/$defs/trajectoryLineClosed", row)
    finally:
        env.close()


def _endings(rec: Recorder, mode: str) -> None:
    prefix = f"S02.{mode}"
    snapshot = {"snapshot_dir": str(FIXTURES / "S02")}
    env = make_env(mode)
    env.reset(0, snapshot)
    rec.step(env, f"{prefix}.invalid_submission", mode, {"action": "submit", "args": {"episode_id": "S02", "findings": "x"}},
             "#/$defs/errorObservation")
    for name, body in (
            ("invalid_submission_extra_keys", {"episode_id": "S02", "findings": [], **FOREIGN}),
            ("invalid_submission_finding_extra_keys",
             {"episode_id": "S02", "findings": [dict(GOOD_S02, truth={"expected_amount_minor": 2000000}, seed=7)]})):
        env = make_env(mode)
        env.reset(0, snapshot)
        rec.add(f"{prefix}.{name}.action", "#/$defs/actionEnvelope", {"action": "submit", "args": body})
        rec.step(env, f"{prefix}.{name}", mode, {"action": "submit", "args": body}, "#/$defs/errorObservation")
        rec.add(f"{prefix}.{name}.trajectory", _trajectory_pointer(mode), env._trajectory[-1])
    # Issue #23: depth 64 reaches the evaluator, deeper is refused by the runner; both end with -1.
    for name, depth in (("depth_602", 602), ("depth_64", 64), ("depth_65", 65)):
        env = make_env(mode)
        env.reset(0, snapshot)
        rec.step(env, f"{prefix}.submit_{name}", mode, _deep_submit(depth), "#/$defs/errorObservation")
        rec.add(f"{prefix}.submit_{name}.trajectory", _trajectory_pointer(mode), env._trajectory[-1])
        rec.add(f"{prefix}.submit_{name}.after", _step_pointer(mode), list(env.step(D9)))
    env = make_env(mode)
    env.reset(0, snapshot)
    rec.step(env, f"{prefix}.tool_depth_65", mode, {"action": "get_document", "args": {"doc_id": _nest(63)}},
             "#/$defs/errorObservation")
    rec.add(f"{prefix}.tool_depth_65.trajectory", _trajectory_pointer(mode), env._trajectory[-1])
    rec.step(env, f"{prefix}.non_json_value", mode, {"action": "submit", "args": {"episode_id": "S02", "findings": [{1, 2}]}},
             "#/$defs/errorObservation")
    env = make_env(mode)
    env.reset(0, snapshot)
    for _ in range(99):
        env.step(D9)
    rec.step(env, f"{prefix}.truncated_budget_100th_inspection", mode, D9, "#/$defs/okObservation")
    env = make_env(mode)
    env.reset(0, snapshot)
    for _ in range(99):
        env.step(D9)
    rec.step(env, f"{prefix}.submit_on_step_100", mode, {"action": "submit", "args": {"episode_id": "S02", "findings": [GOOD_S02]}},
             "#/$defs/replySubmitAccepted")
    clock = Clock()
    env = make_env(mode, clock=clock)
    env.reset(0, snapshot)
    env.step(D9)
    clock.now += 61
    rec.step(env, f"{prefix}.truncated_timeout", mode, D9, "#/$defs/errorObservation")
    env = make_env(mode)
    env.reset(0, snapshot)
    env.step(D9)
    result = env.abort("policy crashed")
    rec.add(f"{prefix}.policy_failure.step", _step_pointer(mode), list(result))
    rec.add(f"{prefix}.policy_failure.trajectory",
            "#/$defs/trajectoryLineTrain" if mode == "train" else "#/$defs/trajectoryLineClosed", env._trajectory[-1])

    class Failing:
        def __init__(self, public_dir):
            self._session = open_session(public_dir)

        def inspect(self, action):
            if action["args"].get("doc_id") == "FAIL":
                raise RuntimeError("C:/secret/private/truth.json is unreadable")
            return self._session.inspect(action)

    env = make_env(mode, session_opener=Failing)
    env.reset(0, snapshot)
    rec.step(env, f"{prefix}.internal_error", mode, {"action": "get_document", "args": {"doc_id": "FAIL"}},
             "#/$defs/errorObservation")


def _fx(rec: Recorder) -> None:
    env = make_env("train")
    env.reset(0, {"snapshot_dir": str(FX_SNAPSHOT)})
    page = rec.step(env, "dataset.train.list_fx", "train",
                    {"action": "list_records", "args": {"table": "fx", "cursor": None, "limit": 50}}, "#/$defs/replyListFx")
    row = page[0]["result"]["records"][0]
    key = "/".join((row["date"], row["source_currency"], row["target_currency"]))
    rec.step(env, "dataset.train.get_fx", "train", {"action": "get_record", "args": {"table": "fx", "id": key}},
             "#/$defs/replyGetFx")


def _exports(rec: Recorder) -> None:
    for run in sorted((ROOT / "tests" / "runner" / "golden").iterdir()):
        rec.add(f"export.{run.name}.run", "#/$defs/exportRun", json.loads((run / "run.json").read_text(encoding="utf-8")))
        rec.add(f"export.{run.name}.score", "#/$defs/score", json.loads((run / "score.json").read_text(encoding="utf-8")))
        rec.add(f"export.{run.name}.submission", "#/$defs/submission",
                json.loads((run / "submission.json").read_text(encoding="utf-8")))


def build_valid() -> list[dict]:
    rec = Recorder()
    for label in ("S01", "S02"):
        for mode in ("train", "closed"):
            _transcript(rec, label, mode)
    for mode in ("train", "closed"):
        _endings(rec, mode)
    _fx(rec)
    _exports(rec)
    for name, action in (("list_records", {"action": "list_records", "args": {"table": "payments", "cursor": None, "limit": 50}}),
                         ("get_record", {"action": "get_record", "args": {"table": "ledger", "id": "R4"}}),
                         ("get_document", D9),
                         ("calculator", {"action": "calculator", "args": {"expression": "-2.5 * (4 + 1)"}}),
                         ("submit", {"action": "submit", "args": {"episode_id": "S02", "findings": [GOOD_S02]}})):
        rec.add(f"action.{name}", "#/$defs/action", action)
    return rec.entries


def _find(valid: list[dict], name: str) -> dict:
    return copy.deepcopy(next(entry for entry in valid if entry["name"] == name))


def build_invalid(valid: list[dict]) -> list[dict]:
    """One defect per entry, derived from a real message. Every entry must be rejected."""
    out = []

    def bad(name, base, pointer, why, mutate):
        instance = copy.deepcopy(_find(valid, base)["instance"])
        mutate(instance)
        out.append({"name": name, "base": base, "pointer": pointer, "instance": instance, "why": why})

    # Missing required fields.
    bad("observation_without_remaining_steps", "S02.train.get_document_D9.observation", "#/$defs/okObservation",
        "missing required field", lambda o: o.pop("remaining_steps"))
    bad("observation_without_ok", "S02.train.error_not_found.observation", "#/$defs/errorObservation",
        "missing required field", lambda o: o.pop("ok"))
    bad("error_without_code", "S02.train.error_not_found.observation", "#/$defs/errorObservation",
        "missing required field", lambda o: o["error"].pop("code"))
    bad("manifest_without_episode_id", "S02.train.reset.manifest", "#/$defs/manifest",
        "missing required field", lambda o: o.pop("episode_id"))
    bad("ledger_row_without_currency", "S02.train.get_ledger_R4.observation", "#/$defs/replyGetLedger",
        "missing required field", lambda o: o["result"].pop("currency"))
    bad("finding_without_explanation", "S02.train.submit.submission", "#/$defs/submission",
        "missing required field", lambda o: o["findings"][0].pop("explanation"))
    bad("reset_without_info", "S02.train.reset", "#/$defs/reset", "missing tuple element", lambda o: o.pop())
    # Wrong types.
    bad("remaining_steps_as_string", "S02.train.get_document_D9.observation", "#/$defs/okObservation",
        "wrong type", lambda o: o.__setitem__("remaining_steps", "88"))
    bad("remaining_steps_as_float", "S02.train.get_document_D9.observation", "#/$defs/okObservation",
        "wrong type (integer means no fraction)", lambda o: o.__setitem__("remaining_steps", 88.0))
    bad("ok_as_string", "S02.train.get_document_D9.observation", "#/$defs/okObservation",
        "wrong type", lambda o: o.__setitem__("ok", "true"))
    bad("debit_as_string", "S02.train.get_ledger_R4.observation", "#/$defs/replyGetLedger",
        "wrong type", lambda o: o["result"].__setitem__("debit_minor", "0"))
    bad("debit_as_bool", "S02.train.get_ledger_R4.observation", "#/$defs/replyGetLedger",
        "wrong type", lambda o: o["result"].__setitem__("credit_minor", True))
    bad("records_as_object", "S02.train.list_payments.observation", "#/$defs/replyListPayments",
        "wrong type", lambda o: o["result"].__setitem__("records", {}))
    bad("cursor_as_number", "S02.train.list_ledger_page1.observation", "#/$defs/replyListLedger",
        "wrong type", lambda o: o["result"].__setitem__("next_cursor", 3))
    bad("calculator_result_as_number", "S02.train.calculator.observation", "#/$defs/replyCalculator",
        "wrong type (decimal string expected)", lambda o: o.__setitem__("result", 666666.67))
    bad("terminated_as_int", "S02.train.submit.step", "#/$defs/stepTrain",
        "wrong type", lambda o: o.__setitem__(2, 1))
    bad("reward_as_string", "S02.train.get_document_D9.step", "#/$defs/stepTrain",
        "wrong type", lambda o: o.__setitem__(1, "0"))
    bad("amount_as_float", "S02.train.submit.submission", "#/$defs/submission",
        "integer amount written as 2000000.0", lambda o: o["findings"][0].__setitem__("claimed_amount_minor", 2000000.0))
    bad("amount_as_bool", "S02.train.submit.submission", "#/$defs/submission",
        "wrong type", lambda o: o["findings"][0].__setitem__("claimed_amount_minor", True))
    bad("amount_negative", "S02.train.submit.submission", "#/$defs/submission",
        "negative amount", lambda o: o["findings"][0].__setitem__("claimed_amount_minor", -1))
    bad("findings_31", "S02.train.submit.submission", "#/$defs/submission",
        "more than 30 findings", lambda o: o.__setitem__("findings", o["findings"] * 31))
    bad("evidence_11", "S02.train.submit.submission", "#/$defs/submission",
        "more than 10 evidence ids", lambda o: o["findings"][0].__setitem__("evidence_ids", [f"ledger/R{i}" for i in range(11)]))
    bad("explanation_1001", "S02.train.submit.submission", "#/$defs/submission",
        "explanation over 1000 characters", lambda o: o["findings"][0].__setitem__("explanation", "я" * 1001))
    bad("finding_unknown_type", "S02.train.submit.submission", "#/$defs/submission",
        "unknown issue type", lambda o: o["findings"][0].__setitem__("type", "fraud"))
    bad("submission_extra_field", "S02.train.submit.submission", "#/$defs/submission",
        "extra field", lambda o: o.__setitem__("confidence", 1))
    bad("action_limit_0", "action.list_records", "#/$defs/action", "limit outside 1..50",
        lambda o: o["args"].__setitem__("limit", 0))
    bad("action_unknown_table", "action.get_record", "#/$defs/action", "unknown table",
        lambda o: o["args"].__setitem__("table", "truth"))
    bad("action_extra_envelope_field", "action.get_document", "#/$defs/action", "extra field",
        lambda o: o.__setitem__("debug", True))
    bad("action_unknown_name", "action.get_document", "#/$defs/action", "unknown action",
        lambda o: o.__setitem__("action", "read_truth"))
    bad("action_without_args", "action.calculator", "#/$defs/action", "missing args", lambda o: o.pop("args"))
    # Closed data and leaks.
    bad("closed_key_in_observation", "S02.closed.get_document_D9.observation", "#/$defs/okObservation",
        "truth field next to the result", lambda o: o.__setitem__("evidence_sets", [["ledger/R4"]]))
    bad("closed_key_in_record", "S02.closed.get_document_D9.observation", "#/$defs/replyGetDocument",
        "hidden field inside a public document", lambda o: o["result"].__setitem__("expected_amount_minor", 2000000))
    bad("closed_key_nested_in_policy_value", "S02.closed.get_policy_allocation.observation", "#/$defs/okObservation",
        "closed key nested in a free-form rule", lambda o: o["result"].__setitem__("details", {"issue_id": "X1"}))
    bad("closed_key_in_policy_document", "S02.closed.reset.observation", "#/$defs/initialObservation",
        "truth in the initial policy", lambda o: o["policy"].__setitem__("truth", {"issues": []}))
    bad("manifest_reveals_issue_count", "S02.closed.reset.manifest", "#/$defs/manifest",
        "number of errors must stay hidden", lambda o: o.__setitem__("issue_count", 1))
    bad("reset_info_reveals_seed", "S02.closed.reset", "#/$defs/reset", "seed in public info",
        lambda o: o[1].__setitem__("seed", 0))
    bad("closed_info_with_score", "S02.closed.submit.step", "#/$defs/stepClosed",
        "grading leaked in closed mode", lambda o: o[4].__setitem__("score", {"tp": 1, "fp": 0, "fn": 0, "precision": 1.0,
                                                                            "recall": 1.0, "f1": 1.0, "reward": 0.992}))
    bad("closed_nonzero_reward", "S02.closed.submit.step", "#/$defs/stepClosed",
        "true reward leaked in closed mode", lambda o: o.__setitem__(1, 0.992))
    bad("closed_trajectory_reward", "S02.closed.trajectory.24", "#/$defs/trajectoryLineClosed",
        "true reward in a closed trajectory", lambda o: o.__setitem__("reward", -1.0))
    bad("train_info_true_reward", "S02.train.submit.step", "#/$defs/stepTrain",
        "trusted field in info", lambda o: o[4].__setitem__("true_reward", 0.992))
    bad("error_message_with_internal_path", "S02.train.internal_error.observation", "#/$defs/errorObservation",
        "free text in a bounded error", lambda o: o["error"].__setitem__("message", "C:/secret/private/truth.json is unreadable"))
    bad("error_with_detail_field", "S02.train.error_not_found.observation", "#/$defs/errorObservation",
        "extra field in error", lambda o: o["error"].__setitem__("detail", "row R4 exists in private/"))
    bad("error_unknown_code", "S02.train.error_not_found.observation", "#/$defs/errorObservation",
        "unpublished error code", lambda o: o["error"].__setitem__("code", "truth_mismatch"))
    bad("ledger_row_extra_field", "S02.train.get_ledger_R4.observation", "#/$defs/replyGetLedger",
        "hidden mutation marker", lambda o: o["result"].__setitem__("mutated", True))
    bad("protocol_with_remaining_and_flags", "S02.train.after_finish.step", "#/$defs/stepTrain",
        "finished episode must not report a reward", lambda o: o.__setitem__(1, 0.5))
    bad("nonterminal_with_reward", "S02.train.get_document_D9.step", "#/$defs/stepTrain",
        "intermediate reward must be 0", lambda o: o.__setitem__(1, 0.1))
    bad("both_terminated_and_truncated", "S02.train.submit.step", "#/$defs/stepTrain",
        "terminated and truncated together", lambda o: o.__setitem__(3, True))
    bad("export_status_submitted", "export.S02-reconcile.run", "#/$defs/exportRun",
        "console reads only completed/truncated/failed", lambda o: o.__setitem__("status", "submitted"))
    # Policy input may carry any key; what the environment sends back may not (PR #22 review).
    bad("action_args_with_seed", "action.calculator", "#/$defs/action", "extra argument is not a valid action",
        lambda o: o["args"].__setitem__("seed", 123))
    bad("trajectory_observation_with_seed", "S02.train.invalid_submission_extra_keys.trajectory",
        "#/$defs/trajectoryLineTrain", "seed echoed in an observation", lambda o: o["observation"].__setitem__("seed", 123))
    bad("closed_trajectory_observation_with_truth", "S02.closed.policy_input_get_document.observation",
        "#/$defs/errorObservation", "truth echoed in an error", lambda o: o.__setitem__("truth", {"issues": []}))
    bad("closed_info_with_seed", "S02.closed.invalid_submission_extra_keys.step", "#/$defs/stepClosed",
        "seed in closed info", lambda o: o[4].__setitem__("seed", 123))
    bad("train_info_score_after_rejected_submit", "S02.train.submit_depth_602.step", "#/$defs/stepTrain",
        "positive reward for a refused submission", lambda o: o.__setitem__(1, 0.992))
    return out


def dump(path: Path, value) -> str:
    return json.dumps(value, ensure_ascii=True, indent=1, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    valid = build_valid()
    files = {EXAMPLES / "valid.json": dump(EXAMPLES / "valid.json", valid),
             EXAMPLES / "invalid.json": dump(EXAMPLES / "invalid.json", build_invalid(valid))}
    if "--check" in argv:
        stale = [str(p.relative_to(ROOT)) for p, text in files.items()
                 if not p.is_file() or p.read_text(encoding="utf-8") != text]
        print("stale:" if stale else "golden examples are current", *stale)
        return 1 if stale else 0
    EXAMPLES.mkdir(parents=True, exist_ok=True)
    for path, text in files.items():
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
