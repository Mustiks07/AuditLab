"""Views of public exports shared by the HTTP API and the CLI.

Reads exclusively through ExportStore (completed open exports). The same
functions back `GET /demo/runs/{id}` and `python -m auditlab.demo show`, so the
console and the CLI print identical numbers.
"""
from __future__ import annotations

from . import derive
from .snapshot import Snapshot, diff_snapshots
from .store import ExportStore

LABELS = {
    "training_sample": "УЧЕБНЫЙ ЭКСПОРТ",
    "live_experiment": "ЖИВОЙ ОПЫТ",
    "rescore_copy": "КОПИЯ СДАЧИ",
    "recorded": "ПОВТОР ЗАПИСИ",
}
TYPE_NAMES = {
    "payment_allocation": "Нераспределённый платёж",
    "fx_conversion": "Валютная оценка",
    "duplicate_invoice": "Дублирующий счёт",
    "period_cutoff": "Граница периода",
}


def _summary(meta: dict, children: dict[str, list[str]]) -> dict:
    out = dict(meta)
    out["label_text"] = LABELS.get(str(meta.get("label")), "ЭКСПОРТ")
    out["children"] = children.get(str(meta.get("run_id")), [])
    return out


def list_runs(store: ExportStore) -> list[dict]:
    runs = store.list_runs()
    children: dict[str, list[str]] = {}
    for meta in runs:
        if meta.get("parent_run_id"):
            children.setdefault(str(meta["parent_run_id"]), []).append(str(meta["run_id"]))
    order = {"training_sample": 0, "live_experiment": 1, "rescore_copy": 2}
    runs.sort(key=lambda m: (order.get(str(m.get("label")), 3), str(m.get("created_at") or ""), str(m["run_id"])))
    return [_summary(m, children) for m in runs]


def _action_text(action: dict) -> str:
    name = action.get("action")
    args = action.get("args") or {}
    if name == "list_records":
        return f"list_records {args.get('table')}" + (" (далее)" if args.get("cursor") else "")
    if name == "get_record":
        return f"get_record {args.get('table')}/{args.get('id')}"
    if name == "get_document":
        return f"get_document {args.get('doc_id')}"
    if name == "calculator":
        return f"calculator {args.get('expression')}"
    if name == "submit":
        return f"submit · выводов: {len(args.get('findings') or [])}"
    return str(name)


def steps(snapshot: Snapshot, trajectory: list[dict]) -> list[dict]:
    out = []
    for line in trajectory:
        action = line.get("action") or {}
        obs = line.get("observation") or {}
        opened = snapshot.key_for_action(action)
        listed = []
        if action.get("action") == "list_records" and obs.get("ok"):
            table = (action.get("args") or {}).get("table")
            for rec in (obs.get("result") or {}).get("records") or []:
                lid = rec.get("row_id") or rec.get("doc_id")
                if lid and snapshot.get(f"{table}/{lid}"):
                    listed.append(f"{table}/{lid}")
        out.append({
            "step": line.get("step"), "action": action, "text": _action_text(action), "ok": obs.get("ok"),
            "error": obs.get("error"), "observation": obs, "remaining_steps": obs.get("remaining_steps"),
            "reward": line.get("reward"), "terminated": line.get("terminated"), "truncated": line.get("truncated"),
            "opened_key": opened, "listed_keys": listed,
        })
    return out


def _verifier_line(verifier: dict | None, finding: dict) -> dict:
    if verifier is None:
        return {"connected": False, "text": "SQL-сверка не подключена в этой сборке (ожидается auditlab.verifier)"}
    for item in verifier.get("findings") or []:
        if item.get("type") == finding.get("type") and sorted(item.get("affected_ids") or []) == sorted(finding.get("affected_ids") or []):
            return {"connected": True, "engine": verifier.get("engine"), "amount_minor": item.get("claimed_amount_minor"),
                    "currency": item.get("currency"), "evidence_ids": item.get("evidence_ids"),
                    "agrees": item.get("claimed_amount_minor") == finding.get("claimed_amount_minor")}
    return {"connected": True, "engine": verifier.get("engine"), "amount_minor": None, "agrees": False,
            "text": "Сверка не нашла такого расхождения"}


def findings_view(snapshot: Snapshot, submission: dict | None, score: dict | None, verifier: dict | None,
                  step_list: list[dict]) -> list[dict]:
    if not submission:
        return []
    opened_at: dict[str, int] = {}
    for st in step_list:
        if st["opened_key"] and st["opened_key"] not in opened_at:
            opened_at[st["opened_key"]] = st["step"]
    verdicts = {int(v["index"]): v for v in (score or {}).get("per_finding") or [] if "index" in v}
    out = []
    for index, finding in enumerate(submission.get("findings") or []):
        evidence = []
        for key in finding.get("evidence_ids") or []:
            rec = snapshot.get(key)
            evidence.append({"key": key, "exists": rec is not None, "file": rec.file if rec else None,
                             "line": rec.line if rec else None, "opened_at_step": opened_at.get(key)})
        affected = []
        for aid in finding.get("affected_ids") or []:
            rec = snapshot.resolve_affected(aid)
            affected.append({"id": aid, "key": rec.key if rec else None})
        calc = derive.derive_for(snapshot, finding)
        uncited = []
        if calc:
            cited = set(finding.get("evidence_ids") or [])
            needed = [o["key"] for o in calc.get("operands") or []] + [calc["rule"]["key"]] + [calc["subject"]]
            uncited = [k for k in dict.fromkeys(needed) if k not in cited]
        out.append({
            "index": index, "finding": finding, "type_name": TYPE_NAMES.get(str(finding.get("type")), finding.get("type")),
            "evidence": evidence, "affected": affected, "derivation": calc, "uncited_operands": uncited,
            "console_agrees": bool(calc) and calc.get("result_minor") is not None and abs(int(calc["result_minor"])) == finding.get("claimed_amount_minor"),
            "verdict": verdicts.get(index), "verifier": _verifier_line(verifier, finding),
        })
    return out


def run_detail(store: ExportStore, run_id: str) -> dict:
    meta = store.read(run_id, "run.json")
    snapshot = Snapshot(store.public_dir(run_id))
    trajectory = store.read(run_id, "trajectories.jsonl")
    submission = store.read(run_id, "submission.json")
    score = store.read(run_id, "score.json")
    verifier = store.read(run_id, "verifier.json")
    step_list = steps(snapshot, trajectory or [])
    all_runs = store.list_runs()
    children = [m["run_id"] for m in all_runs if m.get("parent_run_id") == run_id]
    meta = dict(meta)
    meta["label_text"] = LABELS.get(str(meta.get("label")), "ЭКСПОРТ")
    console = [{"type": c["type"], "subject": c["subject"], "result_minor": c["result_minor"], "currency": c["currency"],
                "affected_ids": c["affected_ids"]} for c in derive.scan(snapshot)]
    return {
        "run": meta,
        "snapshot": snapshot.summary(),
        "journal": {"available": trajectory is not None, "lines": len(trajectory or [])},
        "steps": step_list,
        "submission": submission,
        "empty_submission": submission is not None and not submission.get("findings"),
        "findings": findings_view(snapshot, submission, score, verifier, step_list),
        "score": score,
        "verifier": {"connected": verifier is not None, "engine": (verifier or {}).get("engine")},
        "console_scan": console,
        "children": children,
        "keys": snapshot.order,
    }


def record_view(store: ExportStore, run_id: str, key: str) -> dict | None:
    snapshot = Snapshot(store.public_dir(run_id))
    record = snapshot.get(key)
    if record is None:
        return None
    lines = snapshot.file_lines(record.file.split("/", 1)[1])
    lo = max(0, record.line - 1 - 12)
    hi = min(len(lines), record.line + 12)
    keys_by_line = {r.line: r.key for r in snapshot.records.values() if r.file == record.file}
    return {**record.to_json(),
            "context": [{"line": n + 1, "text": lines[n], "key": keys_by_line.get(n + 1)} for n in range(lo, hi)],
            "header": lines[0] if record.file.endswith(".csv") and lines else None,
            "total_lines": len(lines)}


def _unconfirmed(f: dict) -> list[dict]:
    """Operands of the amount that the finding no longer cites, with their financial meaning."""
    calc = f.get("derivation") or {}
    labels = {o["key"]: f"{o['label']}: {o.get('value_minor', o.get('value'))}" for o in calc.get("operands") or []}
    if calc.get("rule"):
        labels.setdefault(calc["rule"]["key"], "правило расчёта")
    if calc.get("subject"):
        labels.setdefault(calc["subject"], "исходный документ")
    return [{"key": k, "label": labels.get(k, "")} for k in f.get("uncited_operands") or []]


def _claims(detail: dict) -> list[dict]:
    return [{"type": f["finding"]["type"], "affected_ids": f["finding"]["affected_ids"],
             "amount_minor": f["finding"]["claimed_amount_minor"], "currency": f["finding"]["currency"],
             "evidence_ids": f["finding"]["evidence_ids"], "verdict": f["verdict"],
             "unconfirmed": _unconfirmed(f)} for f in detail["findings"]]


def compare(store: ExportStore, left_id: str, right_id: str) -> dict:
    left, right = run_detail(store, left_id), run_detail(store, right_id)
    ls, rs = Snapshot(store.public_dir(left_id)), Snapshot(store.public_dir(right_id))
    changes = diff_snapshots(ls, rs)
    # Side-by-side arithmetic for every payment touched by the change (or all payments).
    touched = {c["key"] for c in changes}
    calcs = []
    for pay in rs.by_table("payments"):
        lp = ls.get(pay.key)
        related = pay.key in touched or any(
            k.startswith("ledger/") and rs.get(k) and rs.get(k).fields.get("payment_id") == pay.fields.get("payment_id")
            for k in touched)
        if lp is None or not related:
            continue
        for fn in (derive.allocation, derive.fx_valuation):
            a, b = fn(ls, lp), fn(rs, pay)
            if a is None or b is None:
                continue
            if fn is derive.allocation and not pay.fields.get("settlement_index"):
                continue
            calcs.append({"type": a["type"], "subject": pay.key, "left": a, "right": b,
                          "difference_minor": (b.get("result_minor") or 0) - (a.get("result_minor") or 0)})
    return {
        "left": {"run": left["run"], "claims": _claims(left), "score": left["score"], "empty": left["empty_submission"]},
        "right": {"run": right["run"], "claims": _claims(right), "score": right["score"], "empty": right["empty_submission"]},
        "changes": changes,
        "calculations": calcs,
        "change_request": right["run"].get("change"),
        "prediction": right["run"].get("prediction"),
        "derived_from": right["run"].get("derived_from"),
    }
