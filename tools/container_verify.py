"""Run trusted verification and record final controller cgroup measurements before it exits."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

CGROUP = Path("/sys/fs/cgroup")


def controller_resources():
    result = {"source": "cgroup_v2", "scope": "trusted controller and its in-container subprocesses; sibling policy/verifier containers excluded"}
    for name in ("memory.peak", "memory.max", "memory.swap.max", "cpu.max", "cpuset.cpus.effective", "pids.max"):
        try:
            value = (CGROUP / name).read_text().strip()
            result[name] = int(value) if value.isdigit() else value
        except OSError:
            result[name] = None
    for name in ("cpu.stat", "memory.events"):
        try:
            result[name] = {key: int(value) for key, value in
                            (line.split() for line in (CGROUP / name).read_text().splitlines())}
        except (OSError, ValueError):
            result[name] = None
    result["status"] = "measured" if result["memory.peak"] is not None and result["cpu.stat"] is not None else "not_checked"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a verification command is required after --")
    started = time.monotonic()
    exit_code = 1
    try:
        exit_code = subprocess.run(command).returncode
        if exit_code < 0:
            exit_code = 128 - exit_code
    except KeyboardInterrupt:
        exit_code = 130
    except OSError as error:
        print(f"Cannot start verification: {error}", file=sys.stderr)
    finally:
        report = controller_resources()
        report.update(command=command, exit_code=exit_code, elapsed_seconds=time.monotonic() - started)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        temporary.replace(args.output)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
