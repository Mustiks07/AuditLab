"""Cross-check what the console shows against the raw export files and a live re-execution.

    python -m auditlab.demo crosscheck                 # every open completed run
    python -m auditlab.demo crosscheck --run ID --live # one run, plus replay of actions and re-scoring

The console view comes from ``service.run_detail`` (the same function behind ``GET /demo/runs/ID``
and ``show --json``). The expected values are read here directly from run.json, score.json,
submission.json, trajectories.jsonl, verifier.json and the public files, without the console code.
With ``--live`` the recorded actions are executed again through the public environment and the
recorded submission is scored again by the evaluator; that part really runs, it is not read back.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from . import lab, service
from .paths import DemoPaths
from .store import ExportStore

SCORE_KEYS = ("tp", "fp", "fn", "reward")


def _load(path: Path):
    if not path.exists():
        return None
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return json.loads(path.read_text(encoding="utf-8"))


def _plain_amount(minor: int, currency: str, exponents: dict) -> str:
    """Independent of the console formatter: Decimal shift by the manifest exponent."""
    exp = exponents.get(currency)
    if type(exp) is not int:
        return f"{minor} мин. ед. {currency} (разрядность не указана)"
    return f"{Decimal(int(minor)).scaleb(-exp):.{exp}f} {currency}"


def _line_holds(public: Path, file: str, line: int, key: str) -> bool:
    """The cited file:line really contains the record: every part of its local id is on that line."""
    lines = (public / file.split("/", 1)[1]).read_text(encoding="utf-8").splitlines()
    if not 1 <= line <= len(lines):
        return False
    local = key.split("/", 1)[1]
    return all(part in lines[line - 1] for part in local.split("/"))


def check_run(store: ExportStore, run_id: str) -> dict:
    directory = store.run_dir(run_id)
    raw = {name: _load(directory / name) for name in
           ("run.json", "score.json", "submission.json", "trajectories.jsonl", "verifier.json")}
    public = directory / "public"
    manifest = _load(public / "manifest.json") or {}
    exponents = manifest.get("currency_exponents") or {}
    detail = service.run_detail(store, run_id)
    problems: list[str] = []
    amounts: list[str] = []

    run = raw["run.json"] or {}
    for key in ("run_id", "episode_id", "policy", "mode", "status"):
        if detail["run"].get(key) != run.get(key):
            problems.append(f"run.{key}: консоль {detail['run'].get(key)!r}, run.json {run.get(key)!r}")

    score = raw["score.json"]
    if (score is None) != (detail["score"] is None):
        problems.append("score.json: наличие различается")
    elif score is not None:
        for key in SCORE_KEYS:
            if detail["score"].get(key) != score.get(key):
                problems.append(f"score.{key}: консоль {detail['score'].get(key)!r}, score.json {score.get(key)!r}")

    trajectory = raw["trajectories.jsonl"] or []
    if len(detail["steps"]) != len(trajectory):
        problems.append(f"шагов: консоль {len(detail['steps'])}, trajectories.jsonl {len(trajectory)}")
    for shown, line in zip(detail["steps"], trajectory):
        observation = line.get("observation") or {}
        if (shown["step"], shown["action"], shown["ok"], shown["remaining_steps"]) != (
                line.get("step"), line.get("action"), observation.get("ok"), observation.get("remaining_steps")):
            problems.append(f"шаг {line.get('step')}: действие/статус/остаток бюджета отличаются")
    numbers = [line.get("step") for line in trajectory]
    if numbers != list(range(1, len(trajectory) + 1)):
        problems.append(f"номера шагов не подряд: {numbers}")
    inspections = sum(1 for line in trajectory if (line.get("action") or {}).get("action") != "submit")
    recorded_inspections = run.get("inspection_steps", (score or {}).get("inspection_steps"))
    if recorded_inspections is not None and recorded_inspections != inspections:
        problems.append(f"инспекций: журнал {inspections}, экспорт {recorded_inspections}")
    final = trajectory[-1] if trajectory else {}
    if (final.get("action") or {}).get("action") == "submit":
        if raw["submission.json"] is not None and (final["action"].get("args") or {}).get("findings") != raw["submission.json"].get("findings"):
            problems.append("submit в журнале не равен submission.json")
        if score is not None and final.get("reward") is not None and final["reward"] != score.get("reward"):
            problems.append(f"награда: журнал {final['reward']}, score.json {score.get('reward')}")

    submission = raw["submission.json"] or {}
    findings = submission.get("findings") or []
    if len(detail["findings"]) != len(findings):
        problems.append(f"выводов: консоль {len(detail['findings'])}, submission.json {len(findings)}")
    if detail["empty_submission"] != (raw["submission.json"] is not None and not findings):
        problems.append("пустая сдача показана неверно")
    links = 0
    for view, finding in zip(detail["findings"], findings):
        shown = view["finding"]
        for key in ("type", "affected_ids", "claimed_amount_minor", "currency", "evidence_ids"):
            if shown.get(key) != finding.get(key):
                problems.append(f"вывод #{view['index']}.{key}: консоль {shown.get(key)!r}, сдача {finding.get(key)!r}")
        currency = finding.get("currency")
        if currency not in exponents:
            problems.append(f"вывод #{view['index']}: валюты {currency} нет в manifest.currency_exponents")
        amounts.append(_plain_amount(finding.get("claimed_amount_minor", 0), currency, exponents))
        for ev in view["evidence"]:
            links += 1
            if not ev["exists"]:
                problems.append(f"ссылка {ev['key']}: записи нет в снимке")
            elif not _line_holds(public, ev["file"], ev["line"], ev["key"]):
                problems.append(f"ссылка {ev['key']} → {ev['file']}:{ev['line']}: строка не содержит запись")

    verifier = raw["verifier.json"]
    if verifier is not None and detail["verifier"]["findings"] != (verifier.get("findings") or []):
        problems.append("SQL-сверка: консоль показывает не то, что в verifier.json")

    return {"run_id": run_id, "episode_id": run.get("episode_id"), "status": run.get("status"),
            "label": detail["run"].get("label"), "steps": len(trajectory), "inspections": inspections,
            "findings": len(findings), "links": links, "amounts": amounts,
            "score": {k: (score or {}).get(k) for k in SCORE_KEYS} if score else None,
            "problems": problems}


def live_check(paths: DemoPaths, run_id: str, extra: list[Path]) -> dict:
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    t0 = time.monotonic()
    result = lab.replay_check(paths, run_id, extra)
    result["executed_at"] = started_at
    result["elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
    return result


def main(paths: DemoPaths, store: ExportStore, extra: list[Path], run_ids: list[str], live: bool,
         as_json: bool) -> int:
    ids = run_ids or [r["run_id"] for r in service.list_runs(store)]
    reports = []
    for run_id in ids:
        report = check_run(store, run_id)
        if live:
            replay = live_check(paths, run_id, extra)
            report["live"] = replay
            if not replay["observations_match"]:
                report["problems"].append(f"живой повтор: наблюдения разошлись на шагах {replay['mismatches']}")
            if replay.get("score_match") is False:
                report["problems"].append(f"живой пересчёт оценки разошёлся: {replay.get('score_again')}")
        reports.append(report)
    if as_json:
        print(json.dumps({"runs": reports, "ok": not any(r["problems"] for r in reports)}, ensure_ascii=False, indent=1))
    else:
        for r in reports:
            mark = "OK " if not r["problems"] else "ERR"
            score = r["score"] or {}
            line = (f"{mark} {r['run_id']:<14} {str(r['episode_id'])[:34]:<34} {r['status']:<10} шагов {r['steps']:>3} "
                    f"выводов {r['findings']} ссылок {r['links']:>2} TP/FP/FN {score.get('tp')}/{score.get('fp')}/{score.get('fn')} "
                    f"награда {score.get('reward')}")
            if r["amounts"]:
                line += "  суммы: " + "; ".join(r["amounts"])
            print(line)
            if "live" in r:
                lv = r["live"]
                print(f"    живой пересчёт {lv['executed_at']} ({lv['elapsed_ms']} мс): {lv['steps_checked']} действий через "
                      f"{lv['session']} — наблюдения {'совпали' if lv['observations_match'] else 'РАЗОШЛИСЬ'}; оценка "
                      f"{lv.get('evaluator', '—')}: {'совпала' if lv.get('score_match') else lv.get('score_note') or 'РАЗОШЛАСЬ'}")
            for problem in r["problems"]:
                print(f"    ✗ {problem}")
        bad = sum(1 for r in reports if r["problems"])
        print(f"Проверено запусков: {len(reports)}; расхождений: {bad}.")
    return 1 if any(r["problems"] for r in reports) else 0


def rehearsal_parity(paths: DemoPaths, rehearsal: Path, policy: str = "reconcile") -> list[dict]:
    """Trusted side: rerun the console's lab on the rehearsal snapshots and compare with the CLI-measured attempts.

    ``rehearsal`` is ``results/verify-*/verify/rehearsal`` of ``offline.py verify --suite rehearsal``.
    Rehearsal attempts ran in closed mode and are never published in the console; here the same
    snapshot is run again in train mode and the console export must give the same findings, evidence,
    TP/FP/FN, inspection steps, reward and action sequence as the CLI ``final.json``/trajectory.
    """
    store = ExportStore([paths.exports], paths.ops)
    out = []
    for attempt in sorted((rehearsal / "attempts" / policy).iterdir()):
        name = attempt.name
        snapshot = rehearsal / "snapshots" / name
        final = _load(attempt / "final.json")
        cli_actions = [line.get("action") for line in
                       _load(attempt / "trusted" / "attempt" / "trajectory.jsonl") or []]
        truth = _load(snapshot / "private" / "truth.json")
        run = lab.execute(paths, snapshot / "public", truth, {
            "key": f"rehearsal:{policy}:{name}:{final['submission']['episode_id']}", "run_id": f"REH-{name}",
            "label": "training_sample", "company": f"Внутренняя репетиция {name}", "policy": policy,
            "note": "Повтор снимка внутренней репетиции в режиме train для сверки консоли с CLI."}, policy_name=policy)
        detail = service.run_detail(store, run["run_id"])
        problems = list(check_run(store, run["run_id"])["problems"])
        cli_score, shown = final["graded"]["score"], detail["score"] or {}
        for key in SCORE_KEYS:
            if cli_score.get(key) != shown.get(key):
                problems.append(f"{key}: CLI {cli_score.get(key)!r}, консоль {shown.get(key)!r}")
        if final.get("inspection_steps") != shown.get("inspection_steps"):
            problems.append(f"инспекций: CLI {final.get('inspection_steps')}, консоль {shown.get('inspection_steps')}")
        cli_findings = final["submission"].get("findings") or []
        shown_findings = [view["finding"] for view in detail["findings"]]
        if cli_findings != shown_findings:
            problems.append("выводы (тип, затронутые ID, сумма, валюта, доказательства) отличаются от CLI")
        if [step["action"] for step in detail["steps"]] != cli_actions:
            problems.append("последовательность действий отличается от CLI-траектории")
        out.append({"episode": name, "run_id": run["run_id"], "cli_status": final.get("status"),
                    "findings": len(cli_findings), "tp": cli_score.get("tp"), "fp": cli_score.get("fp"),
                    "fn": cli_score.get("fn"), "reward": cli_score.get("reward"),
                    "inspection_steps": final.get("inspection_steps"),
                    "amounts": [_plain_amount(f["claimed_amount_minor"], f["currency"],
                                              (_load(snapshot / "public" / "manifest.json") or {}).get("currency_exponents") or {})
                                for f in cli_findings],
                    "problems": problems})
    return out
