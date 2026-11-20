#!/usr/bin/env python3
"""Summarize collect_host_memory.py output into facts with explicit limits (standard library only).

    python3.11 summarize_host_memory.py SAMPLES_DIR [--phases phases.jsonl] [--limit-bytes 4000000000]
        [--out summary.json] [--markdown table.md]

The summary never turns a sampled value into a continuous maximum and never adds container peaks
into a host figure. Exit code: 0 summary written; 1 unreadable or empty input.
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

LIMIT_BYTES = 4_000_000_000


def load_samples(directory: Path) -> tuple[dict, list[dict]]:
    meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    samples = []
    for line in (directory / "samples.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                samples.append(json.loads(line))
            except ValueError:
                break  # a collector killed mid-write leaves one truncated last line
    return meta, samples


def load_phases(path: Path | None, meta: dict) -> list[dict]:
    """phases.jsonl rows carry CLOCK_MONOTONIC start/end (start_mono/end_mono); map them onto sample time."""
    if path is None or not path.is_file():
        return []
    origin = meta.get("monotonic_start")
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if "start_mono" in row and "end_mono" in row and origin is not None:
            row["start_t"], row["end_t"] = row["start_mono"] - origin, row["end_mono"] - origin
        if "start_t" in row and "end_t" in row:
            rows.append(row)
    return rows


def _max(samples, getter):
    best = None
    for item in samples:
        value = getter(item)
        if value is not None and (best is None or value > best[0]):
            best = (value, item["t"], item["utc"])
    return {"bytes": best[0], "at_t": best[1], "at_utc": best[2]} if best else None


def _min(samples, getter):
    best = None
    for item in samples:
        value = getter(item)
        if value is not None and (best is None or value < best[0]):
            best = (value, item["t"], item["utc"])
    return {"bytes": best[0], "at_t": best[1], "at_utc": best[2]} if best else None


def _delta(first, last, getter):
    a, b = getter(first), getter(last)
    return None if a is None or b is None else b - a


def summarize(meta: dict, samples: list[dict], phases: list[dict], limit: int = LIMIT_BYTES) -> dict:
    if not samples:
        raise ValueError("no samples")
    first, last = samples[0], samples[-1]
    gaps = [b["t"] - a["t"] for a, b in zip(samples, samples[1:])]
    used = lambda s: s["derived"]["used_excluding_reclaimable"]  # noqa: E731
    with_cache = lambda s: s["derived"]["used_including_cache"]  # noqa: E731
    total = first["meminfo"].get("MemTotal")
    host = {
        "mem_total_bytes": total,
        "swap_total_bytes": first["meminfo"].get("SwapTotal"),
        "sampled_peak_used_excluding_reclaimable": _max(samples, used),
        "sampled_peak_used_including_cache": _max(samples, with_cache),
        "sampled_min_mem_available": _min(samples, lambda s: s["meminfo"].get("MemAvailable")),
        "sampled_peak_swap_used": _max(samples, lambda s: s["derived"]["swap_used"]),
        "sampled_peak_psi_some_avg10": max(((s.get("psi") or {}).get("some", {}).get("avg10", 0.0) for s in samples),
                                           default=None),
        "sampled_peak_psi_full_avg10": max(((s.get("psi") or {}).get("full", {}).get("avg10", 0.0) for s in samples),
                                           default=None),
    }
    oom = {"vmstat_oom_kill_delta": _delta(first, last, lambda s: s["vmstat"].get("oom_kill"))}
    named = {}
    for name in last.get("cgroups", {}):
        figures = [s["cgroups"].get(name) for s in samples if s["cgroups"].get(name)]
        if not figures:
            named[name] = None
            continue
        named[name] = {
            "continuous_peak_bytes": max((f["peak"] for f in figures if f.get("peak") is not None), default=None),
            "sampled_peak_current_bytes": max((f["current"] for f in figures if f.get("current") is not None), default=None),
            "oom_kill_delta": (figures[-1]["events"].get("oom_kill", 0) - figures[0]["events"].get("oom_kill", 0)),
        }
        oom[f"{name}_oom_kill_delta"] = named[name]["oom_kill_delta"]
    containers = {}
    for item in samples:
        for cid, figures in item.get("containers", {}).items():
            row = containers.setdefault(cid, {"first_t": item["t"], "last_t": item["t"], "samples": 0,
                                              "last_seen_continuous_peak_bytes": None,
                                              "sampled_peak_current_bytes": None, "limit_bytes": None,
                                              "samples_without_limit": 0, "oom_kill": 0})
            row["last_t"], row["samples"] = item["t"], row["samples"] + 1
            # A new cgroup can be read before the runtime writes memory.max: keep the last limit seen and
            # count unlimited readings instead of reporting the first reading as the container's limit.
            if figures.get("max") is not None:
                row["limit_bytes"] = figures["max"]
            else:
                row["samples_without_limit"] += 1
            if figures.get("peak") is not None:
                row["last_seen_continuous_peak_bytes"] = max(row["last_seen_continuous_peak_bytes"] or 0, figures["peak"])
            if figures.get("current") is not None:
                row["sampled_peak_current_bytes"] = max(row["sampled_peak_current_bytes"] or 0, figures["current"])
            row["oom_kill"] = max(row["oom_kill"], figures.get("events", {}).get("oom_kill", 0))
    daemons = {}
    for item in samples:
        for comm, row in item.get("processes", {}).items():
            out = daemons.setdefault(comm, {"max_processes": 0, "sampled_peak_rss_sum_bytes": 0,
                                            "max_single_process_hwm_bytes": 0})
            out["max_processes"] = max(out["max_processes"], row["count"])
            # RSS summed over processes with that name at one sample (sampled);
            out["sampled_peak_rss_sum_bytes"] = max(out["sampled_peak_rss_sum_bytes"], row["rss"])
            # VmHWM is per process: the largest continuous peak of any ONE such process, never a group sum.
            out["max_single_process_hwm_bytes"] = max(out["max_single_process_hwm_bytes"], row["hwm_max"])
    by_phase = []
    for phase in phases:
        inside = [s for s in samples if phase["start_t"] <= s["t"] <= phase["end_t"]]
        by_phase.append({"phase": phase.get("phase"), "exit_code": phase.get("exit_code"),
                         "duration_s": round(phase["end_t"] - phase["start_t"], 3), "samples": len(inside),
                         "sampled_peak_used_excluding_reclaimable": _max(inside, used),
                         "sampled_peak_used_including_cache": _max(inside, with_cache)})
    peak = host["sampled_peak_used_excluding_reclaimable"]
    within = None if peak is None else peak["bytes"] <= limit
    return {
        "method": meta.get("method"), "interval_s": meta.get("interval_s"), "kernel": meta.get("kernel"),
        "started_utc": first["utc"], "stopped_utc": last["utc"], "duration_s": last["t"],
        "samples": len(samples),
        "gaps_s": {"median": statistics.median(gaps) if gaps else None, "max": max(gaps) if gaps else None},
        "host": host, "oom": oom, "named_cgroups": named, "containers": containers, "daemons": daemons,
        "phases": by_phase,
        "limit_bytes": limit,
        "mem_total_within_limit": None if total is None else total <= limit,
        "sampled_peak_within_limit": within,
        "limits_of_inference": [
            "Host figures are sampled every interval_s; a shorter spike can be missed. They are not a continuous maximum.",
            "continuous_peak_bytes comes from one cgroup's memory.peak; peaks of different cgroups need not coincide, "
            "so their sum is not the host peak and is never reported as such.",
            "A container's memory.peak is lost when its cgroup is removed; last_seen_continuous_peak_bytes is the last "
            "value read while it existed and can be below its true final peak.",
            "used_excluding_reclaimable = MemTotal - MemAvailable (kernel estimate). used_including_cache adds page cache.",
            "MemTotal is what the guest kernel reports; it is below the RAM assigned by the hypervisor.",
        ],
    }


def markdown(summary: dict) -> str:
    def b(value):
        return "—" if value is None else f"{value:,}".replace(",", " ")

    host = summary["host"]
    rows = [
        ("MemTotal гостевой ОС", b(host["mem_total_bytes"]), "из /proc/meminfo"),
        ("SwapTotal", b(host["swap_total_bytes"]), "должно быть 0"),
        ("Пик used (MemTotal − MemAvailable), sampled", b((host["sampled_peak_used_excluding_reclaimable"] or {}).get("bytes")), f"интервал {summary['interval_s']} с"),
        ("Пик used вместе с кешем, sampled", b((host["sampled_peak_used_including_cache"] or {}).get("bytes")), "MemTotal − MemFree"),
        ("Минимум MemAvailable, sampled", b((host["sampled_min_mem_available"] or {}).get("bytes")), ""),
        ("Пик swap used, sampled", b((host["sampled_peak_swap_used"] or {}).get("bytes")), ""),
        ("OOM kill (vmstat, прирост)", str(summary["oom"]["vmstat_oom_kill_delta"]), "за весь интервал записи"),
        ("Длительность записи, с", str(summary["duration_s"]), f"{summary['samples']} сэмплов, max gap {summary['gaps_s']['max']}"),
    ]
    for name, row in summary["named_cgroups"].items():
        if row:
            rows.append((f"{name}: memory.peak (непрерывный пик этой cgroup)", b(row["continuous_peak_bytes"]),
                         f"oom_kill +{row['oom_kill_delta']}"))
    for comm, row in sorted(summary["daemons"].items()):
        rows.append((f"{comm}: VmHWM (непрерывный пик RSS одного процесса)", b(row["max_single_process_hwm_bytes"]),
                     f"процессов до {row['max_processes']}; сумма RSS sampled {b(row['sampled_peak_rss_sum_bytes'])}"))
    lines = ["| Показатель | Байт | Примечание |", "|---|---:|---|"]
    lines += [f"| {a} | {v} | {c} |" for a, v, c in rows]
    if summary["phases"]:
        lines += ["", "| Шаг | Код | Длительность, с | Пик used sampled | Пик с кешем sampled |", "|---|---:|---:|---:|---:|"]
        for phase in summary["phases"]:
            lines.append(f"| {phase['phase']} | {phase['exit_code']} | {phase['duration_s']} | "
                         f"{b((phase['sampled_peak_used_excluding_reclaimable'] or {}).get('bytes'))} | "
                         f"{b((phase['sampled_peak_used_including_cache'] or {}).get('bytes'))} |")
    if summary["containers"]:
        lines += ["", "Контейнеры (отдельно; их пики не складываются в память хоста):", "",
                  "| Контейнер | Видим, с | Последний memory.peak | Последний лимит | Сэмплов без лимита | oom_kill |",
                  "|---|---:|---:|---:|---:|---:|"]
        for cid, row in sorted(summary["containers"].items(), key=lambda item: item[1]["first_t"]):
            lines.append(f"| {cid} | {row['first_t']}–{row['last_t']} | {b(row['last_seen_continuous_peak_bytes'])} | "
                         f"{b(row['limit_bytes'])} | {row['samples_without_limit']}/{row['samples']} | {row['oom_kill']} |")
    lines += ["", "Границы вывода:", ""] + [f"- {text}" for text in summary["limits_of_inference"]]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("samples_dir", type=Path)
    parser.add_argument("--phases", type=Path)
    parser.add_argument("--limit-bytes", type=int, default=LIMIT_BYTES)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args(argv)
    try:
        meta, samples = load_samples(args.samples_dir)
        summary = summarize(meta, samples, load_phases(args.phases, meta), args.limit_bytes)
    except (OSError, ValueError, KeyError) as error:
        print(f"summary failed: {error}")
        return 1
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    if args.markdown:
        args.markdown.write_text(markdown(summary), encoding="utf-8")
    if not args.out and not args.markdown:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
