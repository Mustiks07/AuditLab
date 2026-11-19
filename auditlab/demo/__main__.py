"""CLI for the judge console. Works without a browser and without FastAPI.

    python -m auditlab.demo serve                  # console on http://127.0.0.1:8765
    python -m auditlab.demo list                   # open completed runs
    python -m auditlab.demo show --run ID          # investigation as text (same data as the UI)
    python -m auditlab.demo show --run ID --json   # exactly what GET /demo/runs/ID returns
    python -m auditlab.demo replay --run ID        # step-by-step replay of a recorded attempt
    python -m auditlab.demo replay --run ID --check  # re-execute actions, re-score, compare
    python -m auditlab.demo change --source ID --target ledger/R7 --value 2026-12-01T00:01:00+05:00
    python -m auditlab.demo rescore --source ID --replace ledger/R5 --with ledger/R6
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from . import lab, service
from .paths import DemoPaths
from .store import ExportStore, NotFound, Refused


REASONS = {"evidence_incomplete": "доказательства неполные", "amount_or_currency": "сумма или валюта не совпадает",
           "no_such_issue": "такой ошибки нет в эталоне", "duplicate": "повтор засчитанного вывода",
           "not_published": "причина не опубликована оценщиком (TP = 0)"}


def money(minor, currency: str | None, exponents: dict) -> str:
    if minor is None:
        return "—"
    sign = "−" if minor < 0 else ""
    exp = exponents.get(currency)
    if type(exp) is not int or not 0 <= exp <= 9:
        return f"{minor} мин. ед. {currency} (разрядность не указана)"
    whole, frac = divmod(abs(int(minor)), 10 ** exp)
    symbol = {"KZT": "₸", "USD": "$"}.get(str(currency), str(currency))
    decimal = f",{frac:0{exp}d}" if exp else ""
    return f"{sign}{whole:,}".replace(",", " ") + f"{decimal} {symbol} ({minor} мин. ед.)"


def _print_run(detail: dict, replay: bool = False) -> None:
    run = detail["run"]
    exponents = detail["snapshot"]["manifest"].get("currency_exponents") or {}
    print(f"[{run['label_text']}] {run['run_id']}  {run.get('company', '')}")
    print(f"  эпизод {run['episode_id']} · политика {run['policy']} · режим {run['mode']} · статус {run['status']}")
    engine = run.get("engine") or {}
    print("  движок: " + ", ".join(f"{k}={v}" for k, v in engine.items()))
    if run.get("change"):
        ch = run["change"]
        print(f"  изменённый факт: {ch.get('target')}.{ch.get('field')} = {ch.get('value')} (от {run.get('parent_run_id')})")
    if run.get("derived_from"):
        d = run["derived_from"]
        print(f"  копия сдачи {d['run_id']}: вывод #{d['finding_index']} {d['replaced']['from']} → {d['replaced']['to']}")
    if run.get("reason"):
        print(f"  обрыв: {run['reason']}; журнал {'доступен' if run.get('journal_available') else 'недоступен'}")
    print()
    print("Действия аудитора:" if not replay else "ПОВТОР ЗАПИСИ — действия аудитора:")
    for st in detail["steps"]:
        mark = "ok " if st["ok"] else "ERR"
        extra = f" → {st['opened_key']}" if st["opened_key"] else ""
        if st["action"].get("action") == "calculator" and st["ok"]:
            result = st["observation"]["result"]
            extra = f" = {result.get('value') if isinstance(result, dict) else result}"
        print(f"  {st['step']:>3}. {mark} {st['text']}{extra}  (осталось {st['remaining_steps']})")
    print()
    if detail["submission"] is None:
        print("Сдачи нет: попытка не завершена, итог не засчитан.")
    elif detail["empty_submission"]:
        print("Ошибок не заявлено (пустая сдача).")
        independent = detail["verifier"]
        if independent["connected"]:
            print(f"SQL ({independent.get('engine')}): найдено расхождений {len(independent['findings'])}")
            for finding in independent["findings"]:
                print(f"  {finding['type']}: {money(finding['claimed_amount_minor'], finding['currency'], exponents)}")
        else:
            print("SQL-сверка не подключена.")
    for f in detail["findings"]:
        fd = f["finding"]
        print(f"Вывод #{f['index']}: {f['type_name']} · {', '.join(fd['affected_ids'])}")
        print(f"  Агент:    {money(fd['claimed_amount_minor'], fd['currency'], exponents)}")
        for ev in f["evidence"]:
            where = f"{ev['file']}:{ev['line']}" if ev["exists"] else "нет такой записи в снимке"
            seen = (f"открыт на шаге {ev['opened_at_step']}" if ev["opened_at_step"] else
                    f"на странице шага {ev['listed_at_step']}" if ev.get("listed_at_step") else "аудитор не получал")
            print(f"    доказательство {ev['key']:<28} {where:<26} {seen}")
        calc = f["derivation"]
        if calc:
            print(f"  Консоль:  {calc['formula']}  → {money(calc['result_minor'], calc['currency'], exponents)}")
            for op in calc.get("operands") or []:
                value = op.get("value_minor", op.get("value"))
                print(f"    операнд {op['key']:<26} {op['file']}:{op['line']} {op['field']}={value}  ({op['label']})")
        if f["uncited_operands"]:
            print(f"  Не подкреплено ссылкой: {', '.join(f['uncited_operands'])}")
        v = f["verdict"]
        if v:
            src = " (выведено из итога TP/FP/FN)" if v.get("source") == "aggregate" else ""
            print(f"  Оценщик:  {'засчитан (TP)' if v['matched'] else 'не засчитан: ' + REASONS.get(v['reason'], v['reason'])}{src}")
        elif detail["score"]:
            print("  Оценщик:  опубликован только итог TP/FP/FN, вердикт по выводу неоднозначен")
        vr = f["verifier"]
        print("  SQL:      " + (vr.get("text") or f"{money(vr.get('amount_minor'), vr.get('currency'), exponents)} ({vr.get('engine')})"))
    score = detail["score"]
    if score:
        print()
        print(f"Оценка: TP {score['tp']} / FP {score['fp']} / FN {score['fn']} · F1 {score['f1']:.4f} · "
              f"инспекций {score.get('inspection_steps')} · награда {score['reward']:.6f}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m auditlab.demo", description="AuditLab judge console")
    parser.add_argument("--root", type=Path, default=None, help="demo workspace (default runs/demo)")
    parser.add_argument("--exports", type=Path, action="append", default=[],
                        help="additional read-only directory of completed open exports (e.g. the runner's)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--no-samples", action="store_true", help="do not build the labelled training exports")
    sub.add_parser("samples")
    sub.add_parser("list")
    sh = sub.add_parser("show")
    sh.add_argument("--run", required=True)
    sh.add_argument("--json", action="store_true")
    rp = sub.add_parser("replay")
    rp.add_argument("--run", required=True)
    rp.add_argument("--check", action="store_true")
    ch = sub.add_parser("change")
    ch.add_argument("--source", required=True)
    ch.add_argument("--target", required=True)
    ch.add_argument("--field", default="timestamp")
    ch.add_argument("--value", required=True)
    ch.add_argument("--prediction", default=None)
    ch.add_argument("--op", default=None)
    rs = sub.add_parser("rescore")
    rs.add_argument("--source", required=True)
    rs.add_argument("--finding", type=int, default=0)
    rs.add_argument("--replace", required=True)
    rs.add_argument("--with", dest="replacement", required=True)
    rs.add_argument("--op", default=None)
    args = parser.parse_args(argv)

    paths = (DemoPaths(args.root.resolve()) if args.root else DemoPaths.default()).ensure()
    extra = [p.resolve() for p in args.exports]
    store = ExportStore([paths.exports, *extra], paths.ops)

    if args.cmd == "serve":
        try:
            import uvicorn
            from .server import create_app
        except ImportError:
            print("FastAPI/uvicorn are not installed: pip install -r web/requirements-demo.txt\n"
                  "Fallback without a browser: python -m auditlab.demo show --run TRAIN-S02-reconcile", file=sys.stderr)
            return 3
        app = create_app(paths, extra, build_samples=not args.no_samples)
        print(f"AuditLab judge console: http://{args.host}:{args.port}/  (exports: {paths.exports})")
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
        return 0
    if args.cmd in ("samples", "change", "rescore"):
        lab_args = ["--root", str(paths.root)] + sum((["--exports", str(p)] for p in extra), [])
        if args.cmd == "samples":
            return lab.main(lab_args + ["samples"])
        op = args.op or f"cli-{uuid.uuid4().hex[:12]}"
        if args.cmd == "change":
            rest = ["change", "--source", args.source, "--target", args.target, "--field", args.field,
                    "--value", args.value, "--op", op] + (["--prediction", args.prediction] if args.prediction else [])
        else:
            rest = ["rescore", "--source", args.source, "--finding", str(args.finding), "--replace", args.replace,
                    "--with", args.replacement, "--op", op]
        return lab.main(lab_args + rest)
    if args.cmd == "list":
        runs = service.list_runs(store)
        if not runs:
            print("Нет завершённых открытых запусков. Создайте учебные: python -m auditlab.demo samples")
        for r in runs:
            reward = "—" if r.get("reward") is None else f"{r['reward']:.6f}"
            print(f"{r['run_id']:<24} [{r['label_text']}] {r['episode_id']:<12} {r['policy']:<10} {r['status']:<10} награда {reward}")
        return 0
    try:
        detail = service.run_detail(store, args.run)
    except (NotFound, Refused) as exc:
        print(f"Нет открытого завершённого запуска {args.run}: {type(exc).__name__}", file=sys.stderr)
        return 2
    if args.cmd == "show" and args.json:
        print(json.dumps(detail, ensure_ascii=False, indent=1))
        return 0
    _print_run(detail, replay=args.cmd == "replay")
    if args.cmd == "replay" and args.check:
        result = lab.replay_check(paths, args.run, extra)
        print()
        print(f"Повтор действий: {result['steps_checked']} шагов, наблюдения "
              f"{'совпали' if result['observations_match'] else 'РАЗОШЛИСЬ на шагах ' + str(result['mismatches'])}")
        if result.get("score_match") is not None:
            print(f"Повтор оценки: {'совпала' if result['score_match'] else 'РАЗОШЛАСЬ'} {result['score_again']}")
        return 0 if result["observations_match"] and result.get("score_match") is not False else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
