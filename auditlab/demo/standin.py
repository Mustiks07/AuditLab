"""Labelled stand-ins for modules other workstreams own and have not merged yet.

Every run produced with these pieces records ``engine.<part> = "demo-standin"``
and the console shows that label. As soon as the real module exists
(auditlab.environment, auditlab.evaluator, auditlab.policies, auditlab.generator)
``engines.py`` uses it instead.

* StandinSession   — public tools list_records/get_record/get_document/calculator (§7)
* run_episode      — 100-step budget, §11 trajectory lines, terminal reward (§8)
* ReconcilePolicy  — scripted auditor using only public actions (§12 "простая сверка")
* score            — one-to-one matching and reward of §9
* apply_change     — rebuild a public snapshot after one fact change and derive its
                     training truth from public arithmetic (generator stand-in)
"""
from __future__ import annotations

import ast
import json
import shutil
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import derive
from .snapshot import PUBLIC_FILES, Snapshot, ledger_csv

MAX_STEPS = 100
PAGE_LIMIT = 50
STEP_COST = Decimal("0.002")


class ChangeRejected(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- public session
def _err(code: str, message: str) -> dict[str, object]:
    return {"ok": False, "error": {"code": code, "message": message}}


def calculate(expression: str) -> str:
    if not isinstance(expression, str) or not expression or len(expression) > 200:
        raise ValueError("expression_length")
    tree = ast.parse(expression, mode="eval")

    def ev(node: ast.AST, depth: int = 0) -> Decimal:
        if depth > 20:
            raise ValueError("expression_depth")
        if isinstance(node, ast.Expression):
            return ev(node.body, depth + 1)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return Decimal(str(node.value))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            value = ev(node.operand, depth + 1)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left, right = ev(node.left, depth + 1), ev(node.right, depth + 1)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if right == 0:
                raise ValueError("division_by_zero")
            return left / right
        raise ValueError("unsupported_expression")

    value = ev(tree)
    if abs(value) > Decimal(10) ** 18:
        raise ValueError("out_of_range")
    text = format(value.normalize(), "f")
    return text


class StandinSession:
    """Answers inspection actions from public files only."""

    TABLES = ("ledger", "invoices", "payments", "fx", "policy")

    def __init__(self, public_dir: Path):
        self.snapshot = Snapshot(public_dir)
        self.episode_id = str(self.snapshot.manifest.get("episode_id"))

    def inspect(self, action: dict[str, object]) -> dict[str, object]:
        name = action.get("action")
        args = action.get("args") if isinstance(action.get("args"), dict) else {}
        if name == "list_records":
            table = args.get("table")
            if table not in self.TABLES:
                return _err("unknown_table", "Unknown table.")
            limit = args.get("limit", PAGE_LIMIT)
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= PAGE_LIMIT:
                return _err("invalid_limit", "Limit must be 1-50.")
            cursor = args.get("cursor")
            start = 0
            if cursor is not None:
                prefix = f"{self.episode_id}:{table}:"
                if not isinstance(cursor, str) or not cursor.startswith(prefix) or not cursor[len(prefix):].isdigit():
                    return _err("invalid_cursor", "Invalid cursor.")
                start = int(cursor[len(prefix):])
            records = sorted(self.snapshot.by_table(str(table)), key=lambda r: r.local_id)
            if table == "policy":  # public contract: policy pages expose id/value pairs
                rows = [{"id": r.local_id, "value": self.snapshot.policy[r.local_id]} for r in records]
            else:
                rows = [r.fields for r in records]
            page = rows[start:start + limit]
            nxt = start + limit
            return {"ok": True, "result": {"records": page,
                                           "next_cursor": f"{self.episode_id}:{table}:{nxt}" if nxt < len(rows) else None}}
        if name == "get_record":
            record = self.snapshot.get(f"{args.get('table')}/{args.get('id')}")
            if record is None:
                return _err("not_found", "Record not found.")
            if record.table == "policy":  # get_record returns the rule value itself
                return {"ok": True, "result": self.snapshot.policy[record.local_id]}
            return {"ok": True, "result": record.fields}
        if name == "get_document":
            for table in ("payments", "invoices"):
                record = self.snapshot.get(f"{table}/{args.get('doc_id')}")
                if record is not None:
                    return {"ok": True, "result": record.fields}
            return _err("not_found", "Document not found.")
        if name == "calculator":
            try:
                return {"ok": True, "result": calculate(args.get("expression"))}  # decimal string
            except (ValueError, SyntaxError, InvalidOperation):
                return _err("calculator_error", "Expression rejected.")
        return _err("unknown_action", "Unknown action.")


# ---------------------------------------------------------------------- scoring
def _matches(finding: dict, issue: dict) -> tuple[bool, str]:
    if finding.get("type") != issue.get("type") or sorted(finding.get("affected_ids") or []) != sorted(issue.get("affected_ids") or []):
        return False, "no_such_issue"
    if finding.get("currency") != issue.get("currency") or finding.get("claimed_amount_minor") != issue.get("expected_amount_minor"):
        return False, "amount_or_currency"
    cited = set(finding.get("evidence_ids") or [])
    if not any(set(group) <= cited for group in issue.get("evidence_sets") or []):
        return False, "evidence_incomplete"
    return True, "matched"


def score(submission: dict, truth: dict, inspection_steps: int) -> dict[str, object]:
    findings = list(submission.get("findings") or [])
    issues = list(truth.get("issues") or [])
    edges = [[j for j, issue in enumerate(issues) if _matches(f, issue)[0]] for f in findings]
    owner: dict[int, int] = {}

    def augment(i: int, seen: set[int]) -> bool:
        for j in edges[i]:
            if j in seen:
                continue
            seen.add(j)
            if j not in owner or augment(owner[j], seen):
                owner[j] = i
                return True
        return False

    for i in range(len(findings)):
        augment(i, set())
    matched = {i: j for j, i in owner.items()}
    tp = len(matched)
    fp, fn = len(findings) - tp, len(issues) - tp
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 1.0 if not findings and not issues else (2 * tp / (2 * tp + fp + fn) if (2 * tp + fp + fn) else 0.0)
    raw = Decimal(2) * Decimal(str(f1)) - 1 - STEP_COST * inspection_steps
    reward = float(max(Decimal(-1), min(Decimal(1), raw)))
    per_finding = []
    for i, finding in enumerate(findings):
        if i in matched:
            per_finding.append({"index": i, "matched": True, "reason": "matched"})
            continue
        reasons = [_matches(finding, issue)[1] for issue in issues]
        reason = "duplicate" if edges[i] else next((r for r in ("evidence_incomplete", "amount_or_currency") if r in reasons), "no_such_issue")
        per_finding.append({"index": i, "matched": False, "reason": reason})
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1,
            "reward": reward, "inspection_steps": inspection_steps, "per_finding": per_finding}


# --------------------------------------------------------------- runner + policy
class ReconcilePolicy:
    """Scripted reconciliation through public actions only. One instance per episode."""

    name = "reconcile"

    def __init__(self, seed: int = 0):
        self.seed = seed
        self._plan = None

    def act(self, observation: dict[str, object]) -> dict[str, object]:
        if self._plan is None:
            self._plan = self._script(observation)
            return next(self._plan)
        return self._plan.send(observation)

    def _script(self, first: dict):
        manifest = first.get("manifest") or {}
        base = manifest.get("base_currency", "KZT")
        exps = manifest.get("currency_exponents") or {}
        tz = manifest.get("timezone")
        payments, cursor = [], None
        while True:
            obs = yield {"action": "list_records", "args": {"table": "payments", "cursor": cursor, "limit": PAGE_LIMIT}}
            if not obs.get("ok"):
                break
            payments += obs["result"]["records"]
            cursor = obs["result"]["next_cursor"]
            if cursor is None:
                break
        findings = []
        policy_checked: set[str] = set()
        ledger_cache = None
        for row in payments:
            doc = yield {"action": "get_document", "args": {"doc_id": row["doc_id"]}}
            if not doc.get("ok"):
                continue
            payment = doc["result"]
            pid = payment["payment_id"]
            index = (payment.get("settlement_index") or {}).get("unapplied_row_ids") or []
            if index:
                if "allocation" not in policy_checked:
                    policy_checked.add("allocation")
                    yield {"action": "get_record", "args": {"table": "policy", "id": "allocation"}}
                terms = []
                for rid in index:
                    rec = yield {"action": "get_record", "args": {"table": "ledger", "id": rid}}
                    if rec.get("ok"):
                        r = rec["result"]
                        terms.append(f"+{r['credit_minor']}" if r["credit_minor"] else f"-{r['debit_minor']}")
                calc = yield {"action": "calculator", "args": {"expression": "".join(terms).lstrip("+") or "0"}}
                balance = Decimal(calc["result"]) if calc.get("ok") else Decimal(0)
                if balance != 0:
                    findings.append({
                        "type": "payment_allocation",
                        "affected_ids": sorted([f"payment/{pid}"] + [f"invoice/{i}" for i in payment.get("invoice_refs") or []]),
                        "claimed_amount_minor": int(abs(balance)), "currency": payment["currency"],
                        "evidence_ids": [f"payments/{payment['doc_id']}"] + [f"ledger/{rid}" for rid in index] + ["policy/allocation"],
                        "explanation": f"По перечню движений до границы {payment['settlement_index']['before']} платёж {pid} имеет нераспределённый остаток {int(abs(balance))} минимальных единиц {payment['currency']}."})
            if payment["currency"] != base:
                if "fx_date" not in policy_checked:
                    policy_checked.add("fx_date")
                    yield {"action": "get_record", "args": {"table": "policy", "id": "fx_date"}}
                date = derive.local_date(payment["timestamp"], tz)
                fx_id = f"{date}/{payment['currency']}/{base}"
                rate = yield {"action": "get_record", "args": {"table": "fx", "id": fx_id}}
                if ledger_cache is None:
                    ledger_cache, lcur = [], None
                    while True:
                        page = yield {"action": "list_records", "args": {"table": "ledger", "cursor": lcur, "limit": PAGE_LIMIT}}
                        if not page.get("ok"):
                            break
                        ledger_cache += page["result"]["records"]
                        lcur = page["result"]["next_cursor"]
                        if lcur is None:
                            break
                memo = next((r for r in ledger_cache if r.get("payment_id") == pid and r.get("account") == "FX_VALUATION"
                             and r.get("currency") == base), None)
                if rate.get("ok") and memo is not None:
                    src_exp, dst_exp = exps.get(payment["currency"], 2), exps.get(base, 2)
                    expr = f"{payment['amount_minor']}/{10 ** src_exp}*{rate['result']['rate']}*{10 ** dst_exp}-{memo['debit_minor']}"
                    calc = yield {"action": "calculator", "args": {"expression": expr}}
                    diff = Decimal(calc["result"]) if calc.get("ok") else Decimal(0)
                    if diff != 0:
                        findings.append({
                            "type": "fx_conversion", "affected_ids": [f"payment/{pid}"],
                            "claimed_amount_minor": int(abs(diff).to_integral_value()), "currency": base,
                            "evidence_ids": [f"payments/{payment['doc_id']}", f"fx/{fx_id}", f"ledger/{memo['row_id']}", "policy/fx_date"],
                            "explanation": f"Платёж {pid} по курсу {rate['result']['rate']} на {date} даёт иную оценку в {base}, чем FX_VALUATION {memo['row_id']}; разница {int(abs(diff))}."})
        yield {"action": "submit", "args": {"episode_id": manifest.get("episode_id"), "findings": findings}}


def run_episode(session, policy, truth: dict, manifest: dict, score_fn=score) -> dict[str, object]:
    """Drive one open (train) episode. Returns trajectory, submission and score."""
    observation = {"task": "Audit the reporting period and submit provable findings.",
                   "manifest": manifest, "remaining_steps": MAX_STEPS}
    trajectory, remaining, inspections = [], MAX_STEPS, 0
    episode_id = manifest.get("episode_id")
    submission, final_score, status = None, None, "truncated"
    for step in range(1, MAX_STEPS + 1):
        action = policy.act(observation)
        remaining -= 1
        if action.get("action") == "submit":
            submission = dict(action.get("args") or {})
            final_score = score_fn(submission, truth, inspections)
            obs = {"ok": True, "result": {"accepted": True}, "remaining_steps": remaining}
            trajectory.append({"episode_id": episode_id, "step": step, "action": action, "observation": obs,
                               "reward": final_score["reward"], "terminated": True, "truncated": False})
            status = "completed"
            break
        inspections += 1
        obs = dict(session.inspect(action))
        obs["remaining_steps"] = remaining
        last = remaining == 0
        if last:
            submission = {"episode_id": episode_id, "findings": []}
            final_score = score_fn(submission, truth, inspections)
        trajectory.append({"episode_id": episode_id, "step": step, "action": action, "observation": obs,
                           "reward": final_score["reward"] if last else 0, "terminated": False, "truncated": last})
        observation = obs
    return {"trajectory": trajectory, "submission": submission, "score": final_score, "status": status,
            "inspection_steps": inspections}


# ------------------------------------------------------------ generator stand-in
EDITABLE_FIELDS = {"timestamp"}


def _validate_timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise ChangeRejected("invalid_value", "Timestamp must be an ISO-8601 string with offset.")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise ChangeRejected("invalid_value", "Timestamp must be an ISO-8601 string with offset.") from None
    if moment.tzinfo is None:
        raise ChangeRejected("invalid_value", "Timestamp needs a UTC offset.")
    if not 2026 <= moment.year <= 2027:
        raise ChangeRejected("out_of_range", "Timestamp is outside the supported synthetic years.")
    return moment.isoformat()


def _financial_signature(issue: dict) -> tuple:
    return (issue.get("type"), tuple(sorted(issue.get("affected_ids") or [])),
            issue.get("expected_amount_minor"), issue.get("currency"))


def validate_change_scope(snapshot: Snapshot, record, new_ts: str, source_truth: dict | None) -> None:
    """Only the two documented counterfactual rules, never a general truth generator.

    Trusted source truth checks complete type coverage. It is not sent to the web.
    Source IDs/names do not select eligibility; financial structure does.
    """
    def reject(message):
        raise ChangeRejected("unsupported_counterfactual", message)

    if not isinstance(source_truth, dict) or not isinstance(source_truth.get("issues"), list):
        reject("A verified source with trusted training truth is required for a fact change.")
    if source_truth.get("episode_id") != snapshot.manifest.get("episode_id"):
        reject("Source truth does not belong to this public snapshot.")
    allowed = {"payment_allocation", "fx_conversion"}
    if any(issue.get("type") not in allowed for issue in source_truth["issues"]):
        reject("This fact-change adapter supports only allocation and FX cases; other source errors must be preserved by a full generator.")
    current = truth_from_public(snapshot, str(snapshot.manifest.get("episode_id")))
    if sorted(map(_financial_signature, current["issues"])) != sorted(map(_financial_signature, source_truth["issues"])):
        reject("The supported public calculations do not reproduce the complete trusted source answer.")
    # Only direct positive designated payments with a complete UNAPPLIED index.
    for payment in snapshot.by_table("payments"):
        fields = payment.fields
        if (len(fields.get("invoice_refs") or []) != 1 or int(fields.get("amount_minor") or 0) <= 0
                or not fields.get("settlement_index")):
            reject("Fact changes require positive single-invoice payments and complete settlement indexes.")
        calc = derive.allocation(snapshot, payment)
        if not calc["index_check"]["complete"]:
            reject("The public settlement index is incomplete.")
        rows = [r.fields for r in snapshot.by_table("ledger")
                if r.fields.get("payment_id") == fields.get("payment_id") and r.fields.get("account") == "UNAPPLIED"]
        if any(r.get("currency") != fields.get("currency") for r in rows):
            reject("Mixed-currency settlement postings are outside the supported fact-change rules.")
    if record.table == "payments":
        start, end = (datetime.fromisoformat(str(snapshot.manifest[k])) for k in ("period_start", "period_end"))
        before, after = datetime.fromisoformat(str(record.fields["timestamp"])), datetime.fromisoformat(new_ts)
        if not (start <= before < end and start <= after < end):
            reject("Payment-date changes must stay inside the same target period; period changes need the full generator.")
    else:
        journal = record.fields.get("journal_id")
        rows = [r.fields for r in snapshot.by_table("ledger") if journal and r.fields.get("journal_id") == journal]
        if len(rows) != 2 or {r.get("account") for r in rows} != {"UNAPPLIED", "AR"}:
            reject("Only the date of a complete UNAPPLIED-to-AR settlement journal can be changed.")
        unapplied = next(r for r in rows if r["account"] == "UNAPPLIED")
        ar = next(r for r in rows if r["account"] == "AR")
        if (not unapplied.get("payment_id") or not unapplied.get("invoice_id")
                or any(unapplied.get(k) != ar.get(k) for k in ("currency", "payment_id", "invoice_id", "entity_id", "timestamp"))
                or unapplied.get("debit_minor", 0) <= 0 or unapplied.get("credit_minor") != 0
                or ar.get("debit_minor") != 0 or ar.get("credit_minor") != unapplied["debit_minor"]):
            reject("The selected journal is not a balanced positive designated settlement.")


def apply_change(src_public: Path, change: dict, out_public: Path, episode_id: str,
                 *, source_truth: dict | None = None) -> dict[str, object]:
    """Copy a public snapshot, apply one fact change consistently, return the training truth."""
    snap = Snapshot(src_public)
    target, field = change.get("target"), change.get("field")
    if field not in EDITABLE_FIELDS:
        raise ChangeRejected("unsupported_field", f"Only {sorted(EDITABLE_FIELDS)} can be changed in this build.")
    record = snap.get(str(target))
    if record is None or record.table not in ("ledger", "payments"):
        raise ChangeRejected("unknown_target", "Target must be a ledger row or payment document of this run.")
    new_ts = _validate_timestamp(change.get("value"))
    old_ts = str(record.fields.get("timestamp"))
    if datetime.fromisoformat(old_ts) == datetime.fromisoformat(new_ts):
        raise ChangeRejected("no_change", "The new value equals the current one.")

    validate_change_scope(snap, record, new_ts, source_truth)

    ledger = [dict(r.fields) for r in snap.by_table("ledger")]
    header = list(ledger[0].keys()) if ledger else []
    payments = [dict(r.fields) for r in snap.by_table("payments")]
    moved: list[str] = []
    if record.table == "ledger":
        journal = record.fields.get("journal_id")
        for row in ledger:
            if (journal and row.get("journal_id") == journal) or row["row_id"] == record.local_id:
                row["timestamp"] = new_ts
                moved.append(f"ledger/{row['row_id']}")
    else:
        pid = record.fields.get("payment_id")
        for doc in payments:
            if doc["doc_id"] == record.local_id:
                doc["timestamp"] = new_ts
                moved.append(record.key)
        for row in ledger:
            if row.get("payment_id") == pid and datetime.fromisoformat(str(row["timestamp"])) == datetime.fromisoformat(old_ts):
                row["timestamp"] = new_ts
                moved.append(f"ledger/{row['row_id']}")

    # Rebuild the published settlement index from the moved ledger (all UNAPPLIED rows before the boundary).
    for doc in payments:
        index = doc.get("settlement_index")
        if not index:
            continue
        boundary = datetime.fromisoformat(index["before"])
        index["unapplied_row_ids"] = [row["row_id"] for row in ledger
                                      if row.get("account") == "UNAPPLIED" and row.get("payment_id") == doc["payment_id"]
                                      and datetime.fromisoformat(str(row["timestamp"])) < boundary]

    out_public.mkdir(parents=True, exist_ok=True)
    for name in PUBLIC_FILES:
        if (src_public / name).exists():
            shutil.copyfile(src_public / name, out_public / name)
    manifest = dict(snap.manifest)
    manifest["episode_id"] = episode_id
    (out_public / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if header:
        (out_public / "ledger.csv").write_text(ledger_csv(ledger, header), encoding="utf-8")
    (out_public / "payments.jsonl").write_text(
        "".join(json.dumps(d, ensure_ascii=False, separators=(",", ":")) + "\n" for d in payments), encoding="utf-8")

    new_snap = Snapshot(out_public)
    for doc in new_snap.by_table("payments") + new_snap.by_table("invoices"):
        if doc.fields.get("currency") != manifest.get("base_currency"):
            calc = derive.fx_valuation(new_snap, doc)
            if calc is not None and calc.get("result_minor") is None:
                raise ChangeRejected("no_rate_for_date", "No published FX rate for the new local date; the generator rejects this combination.")
    return {"truth": truth_from_public(new_snap, episode_id), "moved": moved}


def truth_from_public(snapshot: Snapshot, episode_id: str) -> dict[str, object]:
    issues = []
    for n, calc in enumerate(derive.scan(snapshot), start=1):
        issues.append({"issue_id": f"X{n}", "type": calc["type"], "affected_ids": sorted(calc["affected_ids"]),
                       "expected_amount_minor": abs(int(calc["result_minor"])), "currency": calc["currency"],
                       "evidence_sets": [list(calc["evidence"])]})
    return {"episode_id": episode_id, "issues": issues}
