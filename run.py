"""AuditLab verification entry point. Uses only the Python standard library."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import unittest
import uuid
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
SUITES = ("foundation", "integration", "simulator", "evaluator", "runner",
          "verifier", "metamorphic", "demo")
GROUPS = {
    "contract_and_isolation": ["API-01", "API-02", "API-03", "API-04", "API-05",
                               "ISO-01", "ISO-02", "ISO-03"],
    "data_and_truth": ["DAT-01", "DAT-02", "DAT-03", "DAT-04", "DAT-05", "VER-01"],
    "reward": ["MAT-01", "MAT-02", "MAT-03", "MAT-04"],
    "reproducibility": ["REP-01", "REP-02", "REP-03"],
    "engineering": ["OPS-01", "OPS-02", "UI-01"],
}
LABELS = {"passed": "пройдено", "failed": "провалено", "not_checked": "не проверено"}


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def git_value(*args):
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True,
                                       stderr=subprocess.DEVNULL, timeout=10).strip()
    except (OSError, subprocess.SubprocessError):
        return None


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []

    def record(self, test, status, kind, detail=""):
        self.records.append({"id": test.id(), "status": status, "kind": kind, "detail": detail})

    def addSuccess(self, test):
        super().addSuccess(test)
        self.record(test, "passed", "test")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.record(test, "not_checked", "missing_component", reason)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.record(test, "failed", "test_failure", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        kind = "policy_error" if err[0].__name__ == "PolicyExecutionError" else "infrastructure_error"
        self.record(test, "failed", kind, self._exc_info_to_string(err, test))

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err:
            kind = "test_failure" if issubclass(err[0], test.failureException) else "infrastructure_error"
            if err[0].__name__ == "PolicyExecutionError":
                kind = "policy_error"
            self.record(subtest, "failed", kind, self._exc_info_to_string(err, test))

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.record(test, "not_checked", "expected_failure", self._exc_info_to_string(err, test))

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.record(test, "failed", "unexpected_success")


def test_worker(directory, destination):
    log = io.StringIO()
    with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        suite = unittest.defaultTestLoader.discover(str(directory), pattern="test_*.py")
        result = unittest.TextTestRunner(stream=log, verbosity=2, resultclass=RecordedResult).run(suite)
    write_json(destination, {"tests_run": result.testsRun, "records": result.records,
                             "log": log.getvalue()})


def run_suite(root, name, output, timeout):
    started = time.monotonic()
    directory = root / "tests" / name
    entry = {"id": name, "status": "not_checked", "kind": "missing_tests", "tests_run": 0,
             "records": [], "detail": "Набор тестов ещё не поставлен."}
    if not directory.is_dir() or not any(directory.rglob("test_*.py")):
        return entry
    destination = output / (name + ".json")
    artifacts = output / (name + "-artifacts")
    artifacts.mkdir()
    env = {**os.environ, "AUDITLAB_CHECK_ARTIFACTS": str(artifacts)}
    try:
        process = subprocess.run([sys.executable, str(ROOT / "run.py"), "_tests",
                                  str(directory), str(destination)], cwd=root, env=env,
                                 text=True, capture_output=True, timeout=timeout)
        (output / (name + ".log")).write_text(process.stdout + process.stderr, encoding="utf-8")
        if process.returncode != 0 or not destination.is_file():
            raise RuntimeError(f"Test worker exited {process.returncode}; see {name}.log")
        data = json.loads(destination.read_text(encoding="utf-8"))
        entry.update(tests_run=data["tests_run"], records=data["records"], detail="", kind="tests")
        (output / (name + ".log")).write_text(data["log"], encoding="utf-8")
        states = {row["status"] for row in data["records"]}
        entry["status"] = ("failed" if "failed" in states else
                           "not_checked" if "not_checked" in states or not states else "passed")
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        entry.update(status="failed", kind="infrastructure_error", detail=str(error))
    entry["elapsed_seconds"] = time.monotonic() - started
    return entry


def acceptance_plan():
    # Component tests are evidence for parts of these groups, not certification of a whole group.
    return [{"id": group, "criterion": criterion, "status": "not_checked",
             "detail": "Полная группа SPEC §14 ещё не принята; частичные тесты приведены отдельно."}
            for criterion, groups in GROUPS.items() for group in groups]


def rehearsal_plan():
    reason = "Внутренний набор и доверенные адаптеры ещё не приняты; запуск не выполнялся."
    return {
        "episodes": [{"id": f"{policy}/internal-{episode:02d}", "policy": policy,
                      "status": "not_checked", "detail": reason}
                     for policy in ("random", "reconcile", "flag_all", "oracle")
                     for episode in range(1, 21)],
        "pairs": [{"id": f"pair-{pair:02d}", "status": "not_checked", "detail": reason}
                  for pair in range(1, 21)],
    }


def exit_code(checks, require_complete=False):
    if any(row["status"] == "failed" for row in checks):
        return 1
    if require_complete and any(row["status"] == "not_checked" for row in checks):
        return 2
    return 0


def render(report):
    rows = ["# Локальная проверка AuditLab", "", f"Commit: `{report['commit']}`",
            f"Python: {report['python']}", f"Рабочее дерево изменено: {report['dirty']}", "",
            "| Проверка | Статус | Учтено тестов (включая пропуски) |", "|---|---|---:|"]
    for check in report["checks"]:
        rows.append(f"| {check['id']} | {LABELS[check['status']]} | {check['tests_run']} |")
    rows += ["", "## Матрица SPEC §14", "", "| ID | Статус |", "|---|---|"]
    rows += [f"| {row['id']} | {LABELS[row['status']]} |" for row in report["acceptance"]]
    rows += ["", "## Пять критериев (без пересчёта в баллы жюри)", ""]
    rows += [f"- {name}: не проверено; полная приёмка не выполнена." for name in GROUPS]
    if report["rehearsal"]:
        rows += ["", "Внутренняя репетиция: выполнено 0/80 опытов и 0/20 пар; не проверено."]
    rows += ["", "Офлайн-релиз, изоляция, пиковая память и целевые лимиты: не проверено.",
             "Это проверка разработки, а не сертификат готовности релиза."]
    return "\n".join(rows) + "\n"


def verify(args):
    output = args.output.resolve()
    # Existing attempts are immutable, including incomplete ones.
    output.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": "1.0", "status": "in_progress", "suite": args.suite,
              "started_at": datetime.now(timezone.utc).isoformat(), "commit": git_value("rev-parse", "HEAD"),
              "dirty": (None if git_value("status", "--porcelain") is None else
                        bool(git_value("status", "--porcelain"))),
              "python": platform.python_version(), "platform": platform.platform(),
              "checks": [], "acceptance": acceptance_plan(),
              "rehearsal": rehearsal_plan() if args.suite == "rehearsal" else None,
              "measurements": {"offline_release": None, "peak_memory_bytes": None,
                               "cpu_limit": None, "release_size_bytes": None}}
    write_json(output / "report.json", report)
    started = time.monotonic()
    for name in SUITES:
        check = run_suite(ROOT, name, output, args.timeout)
        report["checks"].append(check)
        write_json(output / "report.json", report)
        print(f"{name}: {LABELS[check['status']]} ({check['tests_run']} tests)", flush=True)
        for row in check["records"]:
            if row["status"] == "failed":
                print(f"  {row['id']}: {row['kind']}", flush=True)
    all_checks = report["checks"] + report["acceptance"]
    if report["rehearsal"]:
        all_checks += report["rehearsal"]["episodes"] + report["rehearsal"]["pairs"]
    strict = args.require_complete or args.suite == "rehearsal"
    code = exit_code(all_checks, strict)
    report.update(status="failed" if code == 1 else "incomplete" if any(
        row["status"] == "not_checked" for row in all_checks) else "passed",
        exit_code=code, elapsed_seconds=time.monotonic() - started,
        finished_at=datetime.now(timezone.utc).isoformat())
    write_json(output / "report.json", report)
    (output / "report.md").write_text(render(report), encoding="utf-8")
    print(f"Report: {output / 'report.json'}; status={report['status']}; exit={code}")
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("verify", help="Run available checks and report missing components")
    command.add_argument("--suite", choices=("quick", "rehearsal"), default="quick")
    command.add_argument("--require-complete", action="store_true")
    command.add_argument("--output", type=Path, default=ROOT / "artifacts" / ("verify-" + uuid.uuid4().hex))
    command.add_argument("--timeout", type=float, default=180)
    demo = commands.add_parser("demo", help="Serve the local judge console")
    demo.add_argument("--host", default="127.0.0.1")
    demo.add_argument("--port", type=int, default=8765)
    demo.add_argument("--no-samples", action="store_true")
    replay = commands.add_parser("replay", help="Replay a completed open run")
    replay.add_argument("--run", required=True)
    replay.add_argument("--check", action="store_true", help="Re-execute actions and compare observations and score")
    for command in (demo, replay):
        command.add_argument("--root", type=Path)
        command.add_argument("--exports", type=Path, action="append", default=[])
    worker = commands.add_parser("_tests", help=argparse.SUPPRESS)
    worker.add_argument("directory", type=Path)
    worker.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.command == "_tests":
        test_worker(args.directory, args.destination)
        return 0
    if args.command != "verify":
        from auditlab.demo.__main__ import main as console_main
        forwarded = ["--root", str(args.root)] if args.root else []
        for export in args.exports:
            forwarded += ["--exports", str(export)]
        if args.command == "demo":
            forwarded += ["serve", "--host", args.host, "--port", str(args.port)]
            if args.no_samples:
                forwarded.append("--no-samples")
        else:
            forwarded += ["replay", "--run", args.run]
            if args.check:
                forwarded.append("--check")
        return console_main(forwarded)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        return verify(args)
    except OSError as error:
        print(f"Ошибка каталога отчёта: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
