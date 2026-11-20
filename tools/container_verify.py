"""Run trusted verification and record final controller cgroup measurements before it exits."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

CGROUP = Path("/sys/fs/cgroup")


def controller_resources():
    # Shared strict parser prevents policy/controller reports using different scope rules.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from auditlab.runner.isolation import _parse_cgroup_measurements, _verify_cgroup_scope
    values, unavailable = {}, {}
    for name in ("memory.peak", "memory.max", "memory.swap.max", "cpu.max",
                 "cpuset.cpus.effective", "pids.max", "cpu.stat", "memory.events"):
        try:
            values[name] = (CGROUP / name).read_text().strip()
        except OSError as error:
            unavailable[name] = type(error).__name__
    try:
        affinity = sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity = None
    try:
        cgroup = Path('/proc/self/cgroup').read_text().strip()
    except OSError:
        cgroup = None
    requested = {"memory": os.environ.get("AUDITLAB_CONTROLLER_MEMORY", "1536m"),
                 "cpus": os.environ.get("AUDITLAB_CONTROLLER_CPUS", "2"),
                 "cpuset": os.environ.get("AUDITLAB_CPUSET"),
                 "pids": os.environ.get("AUDITLAB_CONTROLLER_PIDS", "256")}
    payload = {"values": values, "unavailable": unavailable, "affinity": affinity, "cgroup": cgroup}
    parsed = _parse_cgroup_measurements(payload)
    # An explicitly selected CPU set is part of the combined controller+policy profile.
    if requested['cpuset'] is None:
        requested['cpuset'] = 'unknown'
    result = _verify_cgroup_scope(parsed, payload, requested)
    result.update(source="cgroup_v2", requested_limits=requested,
                  note="Counters belong to the visible cgroup only after kernel limits/affinity validation; "
                       "sibling policy/verifier containers excluded. Raw parent counters are diagnostic only.")
    # Preserve old report field names, but never publish parent counters as controller usage.
    verified = result['resource_limits_verified']
    result['memory.peak'] = result['peak_memory_bytes']
    result['memory.max'] = result['memory_limit_bytes']
    result['cpu.max'] = values.get('cpu.max')
    result['cpuset.cpus.effective'] = values.get('cpuset.cpus.effective')
    result['pids.max'] = values.get('pids.max')
    result['cpu.stat'] = ({key: int(value) for key, value in
                           (line.split() for line in values.get('cpu.stat', '').splitlines())}
                          if verified else None)
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
