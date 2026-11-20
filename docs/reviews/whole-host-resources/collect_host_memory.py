#!/usr/bin/env python3
"""Whole-guest memory sampler for the OPS-01 review (Linux, Python 3.11 standard library only).

Runs inside the machine under test, next to Docker, and writes one JSON line per sample:

    python3.11 collect_host_memory.py --out DIR [--interval 0.5] [--stop-file PATH]

Stop with SIGINT/SIGTERM or by creating --stop-file. Output in DIR:
  meta.json      method, interval, kernel, start/stop times, sources that were (un)available
  samples.jsonl  one object per sample (see sample() below)

What each figure means (the report must keep these apart):
  * meminfo.*            whole guest OS as the kernel sees it: kernel, page cache, daemons, containers.
                         Only SAMPLED: a spike shorter than the interval can be missed.
  * cgroups.*.peak       kernel high-water mark of one cgroup (memory.peak, Linux >= 5.19). This IS a
                         continuous maximum, but only of that cgroup; peaks of different cgroups need not
                         coincide in time, so their sum is not the whole-host peak.
  * processes.*.hwm      VmHWM: continuous peak RSS of one process since it started.
  * vmstat.oom_kill and cgroups.*.events.oom_kill are cumulative counters: any increase is an OOM kill.
Nothing here needs root; files that cannot be read are recorded as null, never guessed.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

CGROUP = Path("/sys/fs/cgroup")
MEMINFO_KEYS = ("MemTotal", "MemFree", "MemAvailable", "Buffers", "Cached", "SwapCached", "Active", "Inactive",
                "Shmem", "Slab", "SReclaimable", "SUnreclaim", "KernelStack", "PageTables", "AnonPages",
                "Mapped", "Committed_AS", "SwapTotal", "SwapFree", "Dirty", "Writeback")
VMSTAT_KEYS = ("oom_kill", "pgmajfault", "pswpin", "pswpout", "workingset_refault_file")
NAMED_CGROUPS = {
    "system.slice": "system.slice",
    "user.slice": "user.slice",
    "init.scope": "init.scope",
    "docker.service": "system.slice/docker.service",
    "containerd.service": "system.slice/containerd.service",
}
DAEMONS = ("dockerd", "containerd", "containerd-shim", "containerd-shim-runc-v2", "docker-proxy")
STOP = False


def utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def read_int(path: Path) -> int | None:
    text = read_text(path)
    if text is None:
        return None
    text = text.strip()
    if text == "max":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def meminfo() -> dict:
    values = {}
    for line in (read_text(Path("/proc/meminfo")) or "").splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if name in MEMINFO_KEYS and parts:
            values[name] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1] == "kB" else 1)
    total, free, available = values.get("MemTotal"), values.get("MemFree"), values.get("MemAvailable")
    derived = {
        # Everything not free, page cache included.
        "used_including_cache": total - free if total is not None and free is not None else None,
        # What the kernel could not hand out without swapping or OOM: the usual "used" figure.
        "used_excluding_reclaimable": total - available if total is not None and available is not None else None,
        "swap_used": (values["SwapTotal"] - values["SwapFree"]) if "SwapTotal" in values and "SwapFree" in values else None,
    }
    return {"meminfo": values, "derived": derived}


def vmstat() -> dict:
    out = {}
    for line in (read_text(Path("/proc/vmstat")) or "").splitlines():
        name, _, value = line.partition(" ")
        if name in VMSTAT_KEYS:
            out[name] = int(value)
    return out


def pressure() -> dict | None:
    text = read_text(Path("/proc/pressure/memory"))
    if text is None:
        return None
    result = {}
    for line in text.splitlines():
        kind, *fields = line.split()
        result[kind] = {key: float(value) for key, value in (field.split("=") for field in fields)}
    return result


def cgroup_figures(path: Path) -> dict | None:
    if not path.is_dir():
        return None
    events = {}
    for line in (read_text(path / "memory.events") or "").splitlines():
        name, _, value = line.partition(" ")
        if name in ("oom", "oom_kill", "max", "high"):
            events[name] = int(value)
    return {"current": read_int(path / "memory.current"), "peak": read_int(path / "memory.peak"),
            "max": read_int(path / "memory.max"), "swap_current": read_int(path / "memory.swap.current"),
            "events": events}


def containers() -> dict:
    found = {}
    patterns = ("system.slice/docker-*.scope", "docker/*")  # systemd and cgroupfs Docker drivers
    for pattern in patterns:
        for path in CGROUP.glob(pattern):
            name = path.name.removeprefix("docker-").removesuffix(".scope")
            if len(name) >= 12 and all(c in "0123456789abcdef" for c in name[:12]):
                figures = cgroup_figures(path)
                if figures is not None:
                    found[name[:12]] = figures
    return found


def processes() -> dict:
    out = {}
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        comm = (read_text(proc / "comm") or "").strip()
        if comm not in DAEMONS and not comm.startswith("containerd-shim"):
            continue
        status = read_text(proc / "status") or ""
        fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)

        def kb(name):
            value = fields.get(name, "").split()
            return int(value[0]) * 1024 if value else None

        row = out.setdefault(comm, {"count": 0, "rss": 0, "hwm_max": 0})
        row["count"] += 1
        row["rss"] += kb("VmRSS") or 0
        row["hwm_max"] = max(row["hwm_max"], kb("VmHWM") or 0)
    return out


def sample(started: float) -> dict:
    return {"t": round(time.monotonic() - started, 3), "utc": utc(), **meminfo(), "vmstat": vmstat(),
            "psi": pressure(), "cgroups": {name: cgroup_figures(CGROUP / rel) for name, rel in NAMED_CGROUPS.items()},
            "containers": containers(), "processes": processes()}


def describe_sources() -> dict:
    return {
        "cgroup_v2": (CGROUP / "cgroup.controllers").is_file(),
        "memory_peak_supported": any((CGROUP / rel / "memory.peak").is_file() for rel in NAMED_CGROUPS.values()),
        "psi": Path("/proc/pressure/memory").is_file(),
        "named_cgroups_present": {name: (CGROUP / rel).is_dir() for name, rel in NAMED_CGROUPS.items()},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--max-seconds", type=float, default=None, help="safety stop (default: none)")
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval must be positive")
    args.out.mkdir(parents=True, exist_ok=False)

    def stop(*_):
        global STOP
        STOP = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    meta = {"tool": "collect_host_memory.py", "version": "1.0", "interval_s": args.interval,
            "kernel": platform.release(), "machine": platform.machine(), "python": platform.python_version(),
            "pid": os.getpid(), "started_utc": utc(), "sources": describe_sources(),
            "method": "Sampled /proc/meminfo, /proc/vmstat, /proc/pressure/memory; cgroup v2 memory.current/"
                      "memory.peak/memory.events; VmRSS/VmHWM of Docker daemons. No root, no writes outside --out."}
    (args.out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    started = time.monotonic()
    meta["monotonic_start"] = started  # CLOCK_MONOTONIC is shared by all processes: phases use it too
    (args.out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    count = 0
    with (args.out / "samples.jsonl").open("w", encoding="utf-8") as stream:
        next_at = started
        while not STOP:
            stream.write(json.dumps(sample(started), separators=(",", ":")) + "\n")
            stream.flush()
            count += 1
            if args.stop_file and args.stop_file.exists():
                break
            if args.max_seconds and time.monotonic() - started >= args.max_seconds:
                break
            next_at += args.interval
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_at = time.monotonic()  # fell behind: do not burst to catch up
        stream.write(json.dumps(sample(started), separators=(",", ":")) + "\n")  # closing sample
        count += 1
    meta.update(stopped_utc=utc(), duration_s=round(time.monotonic() - started, 3), samples=count)
    (args.out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
