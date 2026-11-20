"""Mandatory policies on the open train/dev sets for the receiver (OPS-02), with a strict summary.

Standard library only. The same file works with an unchanged v0.1.0-rc.3 bundle (copy it from
docs/demo/HANDOFF.md, section 5a) and with later bundles (``source/auditlab/demo/open_sets.py``).

Inside the runtime image (policies run as sibling containers without network, like ``verify``)::

    python - run --policy-image IMAGE --results /results < open_sets.py > summary.jsonl

On the host::

    python3.11 -I open_sets.py summarize summary.jsonl --datasets source/datasets/open --output open-sets.json

``run`` records, for every attempt, the child return code, the parsed final JSON line or the parse
error, and bounded stderr. It never invents a result. ``summarize`` accepts the run as complete only
when every expected (split, episode, policy) appears exactly once, the child exited with 0, the
status is ``submitted`` and score/reward are present. Otherwise the overall metrics of the affected
policy are ``null`` ("не проверено"), the measured part is reported separately with its denominator,
and the exit code is not 0: 1 for failed or malformed attempts, 2 for missing attempts only.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from pathlib import Path

SPLITS = ("train", "dev")
POLICIES = ("random", "reconcile", "flag_all", "oracle")
EPISODES_PER_SPLIT = 10
TAIL = 2000
MAX_FINDINGS = 30  # SPEC, submit: at most 30 findings, so tp + fp <= 30


# ----------------------------------------------------------------------------- run
def attempt_command(split: str, episode: str, policy: str, image: str, results: str) -> list[str]:
    snapshot = f"datasets/open/{split}/{episode}"
    command = [sys.executable, "-m", "auditlab.runner"]
    if policy == "oracle":
        command += ["oracle", "--snapshot", snapshot]
    else:
        command += ["policy", "--snapshot", snapshot, "--name", policy, "--image", image]
    return command + ["--mode", "closed", "--trusted-dir", f"{results}/attempts/{split}/{episode}/{policy}"]


def record(split: str, episode: str, policy: str, returncode: int, stdout: str, stderr: str) -> dict:
    """One attempt as observed: the result is the last JSON line of stdout, or null with the parse error."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    result, parse_error = None, None
    if not lines:
        parse_error = "no output"
    else:
        try:
            result = json.loads(lines[-1])
        except (ValueError, RecursionError) as error:
            parse_error = f"last stdout line is not JSON: {type(error).__name__}"
        else:
            if not isinstance(result, dict):
                result, parse_error = None, "last stdout line is not a JSON object"
    return {"split": split, "episode": episode, "policy": policy, "returncode": returncode,
            "result": result, "parse_error": parse_error, "stderr_tail": stderr[-TAIL:]}


def run(image: str, results: str, root: Path) -> int:
    failed = 0
    for split in SPLITS:
        try:
            episodes = sorted(os.listdir(root / split))
        except OSError as error:
            print(f"open_sets run: cannot list {root / split}: {type(error).__name__}", file=sys.stderr)
            return 1
        for episode in episodes:
            for policy in POLICIES:
                try:
                    done = subprocess.run(attempt_command(split, episode, policy, image, results),
                                          capture_output=True, text=True, timeout=300)
                    row = record(split, episode, policy, done.returncode, done.stdout, done.stderr)
                except (OSError, subprocess.SubprocessError) as error:
                    row = record(split, episode, policy, -1, "", f"{type(error).__name__}: {error}")
                failed += row["returncode"] != 0 or row["result"] is None
                print(json.dumps(row, ensure_ascii=False), flush=True)
    return 1 if failed else 0


# ----------------------------------------------------------------------- summarize
def attempt_problem(row: dict) -> str | None:
    """Reason why an attempt cannot be counted as measured, or None. Types are exact: bool is not a number."""
    returncode = row.get("returncode")
    if type(returncode) is not int:
        return f"нет целого кода возврата: {returncode!r}"
    if returncode != 0:
        return f"процесс завершился с кодом {returncode}"
    parse_error = row.get("parse_error")
    if parse_error is not None:
        if isinstance(parse_error, str) and parse_error:
            return f"ответ не разобран: {parse_error}"
        return f"некорректное поле parse_error: {parse_error!r}"
    result = row.get("result")
    if not isinstance(result, dict):
        return "нет результата"
    if result.get("status") != "submitted":
        return f"статус {result.get('status')!r}, а не submitted"
    score = result.get("score")
    if not isinstance(score, dict):
        return "нет score"
    for name in ("tp", "fp", "fn"):
        value = score.get(name)
        if type(value) is not int:
            return f"score.{name} не целое число: {value!r}"
        if value < 0:
            return f"score.{name} отрицательное: {value}"
    if score["tp"] + score["fp"] > MAX_FINDINGS:
        return f"tp + fp = {score['tp'] + score['fp']} больше лимита сдачи {MAX_FINDINGS} выводов"
    reward = result.get("reward")
    if type(reward) not in (int, float):
        return f"нет числового reward: {reward!r}"
    if not math.isfinite(reward):
        return f"reward не конечное число: {reward!r}"
    if not -1 <= reward <= 1:
        return f"reward {reward} вне диапазона [-1, 1] (SPEC §9)"
    return None


def row_key(row: object) -> tuple[str, str, str] | None:
    """(split, episode, policy) only for a JSON object with three non-empty string identifiers."""
    if not isinstance(row, dict):
        return None
    key = tuple(row.get(name) for name in ("split", "episode", "policy"))
    if not all(isinstance(part, str) and part for part in key):
        return None
    return key  # type: ignore[return-value]


def expected_episodes(datasets: Path | None) -> dict[str, list[str]] | None:
    if datasets is None:
        return None
    return {split: sorted(p.name for p in (datasets / split).iterdir() if p.is_dir()) for split in SPLITS}


def summarize(lines: list[str], expected: dict[str, list[str]] | None = None,
              input_problems: list[str] | tuple = ()) -> dict:
    """Strict report; never raises on bad input. ``input_problems`` are file/dataset read errors."""
    problems: list[str] = list(input_problems)
    malformed = bool(problems)  # unreadable, empty, duplicated or unexpected rows: an error, not just a gap
    rows: dict[tuple, dict] = {}
    duplicated: set[tuple] = set()
    if not any(line.strip() for line in lines) and not problems:
        problems.append("файл результатов пуст")
        malformed = True
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (ValueError, RecursionError) as error:
            problems.append(f"строка {number} не разобрана: {type(error).__name__}")
            malformed = True
            continue
        key = row_key(row)
        if key is None:
            problems.append(f"строка {number}: нужен JSON-объект с непустыми строками split, episode, policy")
            malformed = True
            continue
        if key in rows:
            duplicated.add(key)
            malformed = True
            problems.append(f"повтор строки {'/'.join(key)}")
            continue
        rows[key] = row

    seen = {split: sorted({k[1] for k in rows if k[0] == split}) for split in SPLITS}
    episodes = expected or seen
    for split in SPLITS:
        if expected is None and len(seen[split]) != EPISODES_PER_SPLIT:
            problems.append(f"{split}: компаний {len(seen[split])}, ожидается {EPISODES_PER_SPLIT}")
        if expected is not None and len(expected[split]) != EPISODES_PER_SPLIT:
            problems.append(f"{split}: в наборе данных {len(expected[split])} компаний, ожидается {EPISODES_PER_SPLIT}")
            malformed = True
    for key in sorted(rows):
        if key[0] not in SPLITS or key[2] not in POLICIES or key[1] not in episodes.get(key[0], []):
            problems.append(f"неожиданная строка {'/'.join(key)}")
            malformed = True

    policies, failed_any, missing_any = {}, False, False
    for split in SPLITS:
        for policy in POLICIES:
            planned = len(episodes[split])
            measured, failures, missing = [], [], []
            for episode in episodes[split]:
                key = (split, episode, policy)
                if key in duplicated:
                    failures.append({"episode": episode, "problem": "повтор строки: результат неоднозначен"})
                elif key not in rows:
                    missing.append(episode)
                else:
                    problem = attempt_problem(rows[key])
                    if problem:
                        failures.append({"episode": episode, "problem": problem})
                    else:
                        measured.append(rows[key]["result"])
            failed_any |= bool(failures)
            missing_any |= bool(missing)
            part = None
            if measured:
                part = {"measured": len(measured), "planned": planned,
                        "tp": sum(r["score"]["tp"] for r in measured),
                        "fp": sum(r["score"]["fp"] for r in measured),
                        "fn": sum(r["score"]["fn"] for r in measured),
                        "mean_reward": round(sum(r["reward"] for r in measured) / len(measured), 6)}
            complete = len(measured) == planned == EPISODES_PER_SPLIT and not failures and not missing
            policies[f"{split}/{policy}"] = {
                "planned": planned, "measured": len(measured), "failed": failures, "missing": missing,
                "status": "complete" if complete else "не проверено",
                "micro": {k: part[k] for k in ("tp", "fp", "fn")} if complete else None,
                "mean_reward": part["mean_reward"] if complete else None,
                "measured_only": None if complete else part,
            }
    ok = not problems and not failed_any and not missing_any
    exit_code = 0 if ok else 1 if (failed_any or malformed) else 2
    return {"status": "complete" if ok else "не проверено", "exit_code": exit_code,
            "expected_attempts": sum(len(v) for v in episodes.values()) * len(POLICIES),
            "rows": len(rows), "problems": problems, "policies": policies,
            "scope": "Internal diagnostics on the open train/dev sets; not a contest score."}


def read_lines(path: Path) -> tuple[list[str], list[str]]:
    """Lines of the JSONL file; unreadable files and non-UTF-8 lines become problems, not exceptions."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return [], [f"файл результатов {path} не найден"]
    except OSError as error:
        return [], [f"файл результатов {path} не прочитан: {type(error).__name__}"]
    lines, problems = [], []
    for number, raw in enumerate(data.split(b"\n"), start=1):
        try:
            lines.append(raw.decode("utf-8"))
        except UnicodeDecodeError:
            problems.append(f"строка {number} не в кодировке UTF-8")
            lines.append("")
    return lines, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="open_sets.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="inside the runtime image: run 4 policies on 10 train + 10 dev")
    r.add_argument("--policy-image", required=True)
    r.add_argument("--results", default="/results")
    r.add_argument("--datasets", type=Path, default=Path("datasets/open"))
    s = sub.add_parser("summarize", help="on the host: strict summary of the JSONL written by run")
    s.add_argument("file", type=Path)
    s.add_argument("--datasets", type=Path, default=None,
                   help="datasets/open of the bundle sources: the exact expected episode IDs")
    s.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.command == "run":
        return run(args.policy_image, args.results, args.datasets)
    lines, input_problems = read_lines(args.file)
    expected = None
    try:
        expected = expected_episodes(args.datasets)
    except OSError as error:
        input_problems.append(f"наборы данных {args.datasets} не прочитаны: {type(error).__name__}")
    report = summarize(lines, expected, input_problems)
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for name, row in report["policies"].items():
        if row["status"] == "complete":
            print(f"{name:<18} {row['measured']}/{row['planned']} TP/FP/FN {row['micro']['tp']}/{row['micro']['fp']}/"
                  f"{row['micro']['fn']} mean_reward {row['mean_reward']}")
        else:
            part = row["measured_only"]
            measured = (f"измерено {part['measured']}/{part['planned']}: TP/FP/FN {part['tp']}/{part['fp']}/{part['fn']}, "
                        f"среднее по измеренным {part['mean_reward']}") if part else "измерений нет"
            print(f"{name:<18} НЕ ПРОВЕРЕНО: mean_reward null; {measured}; ошибок {len(row['failed'])}, "
                  f"пропусков {len(row['missing'])}")
    for problem in report["problems"]:
        print(f"проблема: {problem}")
    print(f"status: {report['status']}, exit {report['exit_code']}")
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
