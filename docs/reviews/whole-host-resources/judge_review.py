#!/usr/bin/env python3
"""Verdict for one run_review.sh directory: confirmed / failed / not_checked (standard library only).

    python3.11 judge_review.py REVIEW_DIR [--source-sha SHA] [--limit-bytes 4000000000]

Reads only the files the review saved and trusts none of them blindly:
  * the machine profile is re-evaluated from qualification.json facts, not from its "qualified" flag,
    and cross-checked against the memory samples;
  * the memory summary is recomputed from memory/samples.jsonl and compared with memory/summary.json;
  * every HANDOFF step must be covered by samples (no gap larger than the allowed one inside a step,
    and samples on both sides of it);
  * the rehearsal acceptance matrix must contain exactly the 24 SPEC §14 groups: 22 passed and only
    OPS-01 / OPS-02 not checked;
  * the evidence checker summary must be the positive one for the expected source, with every field.
Missing data gives not_checked; a proven violation or contradiction gives failed.
Exit code: 0 confirmed, 1 failed, 2 not_checked.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIMIT_BYTES = 4_000_000_000
ACCEPTANCE_GROUPS = (
    "API-01", "API-02", "API-03", "API-04", "API-05", "ISO-01", "ISO-02", "ISO-03",
    "DAT-01", "DAT-02", "DAT-03", "DAT-04", "DAT-05", "VER-01",
    "MAT-01", "MAT-02", "MAT-03", "MAT-04",
    "REP-01", "REP-02", "REP-03",
    "OPS-01", "OPS-02", "UI-01",
)
EXPECTED_NOT_CHECKED = {"OPS-01", "OPS-02"}
EXPECTED_STEP_CODES = {"check": 0, "rehearsal": 2, "image_runtime": 0, "evidence_checker": 0, "launch_cleanup": 0,
                       "demo_start": 0, "demo_health": 0, "demo_runs": 0, "crosscheck_live": 0, "replay_check": 0}
EVIDENCE_COUNTS = {"datasets": 20, "attempts": 80, "pairs": 20, "ordinary_resource_runs": 60,
                   "verifier_resource_runs": 40}
# Summary figures recomputed from raw samples; the stored summary must agree with them exactly.
COMPARED = (("samples",), ("duration_s",), ("interval_s",), ("gaps_s", "max"),
            ("host", "mem_total_bytes"), ("host", "swap_total_bytes"),
            ("host", "sampled_peak_used_excluding_reclaimable", "bytes"),
            ("host", "sampled_peak_used_including_cache", "bytes"),
            ("host", "sampled_min_mem_available", "bytes"), ("host", "sampled_peak_swap_used", "bytes"),
            ("oom",))


def _summarizer():
    spec = importlib.util.spec_from_file_location("summarize_host_memory", HERE / "summarize_host_memory.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _dig(value, path):
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def phases(review: Path) -> dict:
    rows = {}
    path = review / "phases.jsonl"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                rows[row["phase"]] = row
    return rows


def output(review: Path, name: str) -> str:
    path = review / "steps" / f"{name}.out"
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    return text.split("\n", 1)[1] if "\n" in text else ""


def docker_events(text: str) -> list[tuple[str, str]]:
    """(type, action) of each Docker event, for JSON lines and for the older text format.

    In the text format an event starts with a Unix timestamp; any other line continues a multi-line
    attribute (for example an exec command) and is not an event.
    """
    events = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                item = json.loads(line)
            except ValueError:
                continue
            events.append((str(item.get("Type", "")), str(item.get("Action", ""))))
            continue
        match = re.match(r"^\d{9,}(?:\.\d+)? (\w+) (\S+)", line)
        if match:
            events.append((match.group(1), match.group(2)))
    return events


def acceptance_statuses(report: dict) -> dict:
    """{group_id: status} from verify/report.json, tolerant of list or dict layouts."""
    groups = report.get("acceptance") or report.get("matrix") or []
    if isinstance(groups, dict):
        groups = [{"id": key, **value} if isinstance(value, dict) else {"id": key, "status": value}
                  for key, value in groups.items()]
    return {row.get("id"): row.get("status") for row in groups if isinstance(row, dict)}


def check_qualification(qualification, limit, failed, not_checked, facts):
    """Re-derive the profile from the recorded facts; a 'qualified' flag alone proves nothing."""
    if not isinstance(qualification, dict) or not isinstance(qualification.get("facts"), dict):
        not_checked.append("qualification.json or its facts missing")
        return None
    f = qualification["facts"]
    needed = ("machine", "mem_total_bytes", "swap_total_bytes", "affinity_cpus", "python", "cgroup2",
              "docker_cgroup", "docker_server")
    absent = [key for key in needed if f.get(key) is None]
    if absent:
        not_checked.append(f"qualification facts missing: {absent}")
        return None
    derived = {
        "x86_64": f["machine"] == "x86_64",
        "two_cpus": f["affinity_cpus"] == 2,
        "mem_total_le_limit": isinstance(f["mem_total_bytes"], int) and f["mem_total_bytes"] <= limit,
        "swap_disabled": f["swap_total_bytes"] == 0,
        "python_3_11": str(f["python"]).startswith("3.11."),
        "cgroup_v2": f["cgroup2"] is True and str(f["docker_cgroup"]) == "2",
        "docker_engine_reachable": bool(f["docker_server"]),
    }
    facts["qualification"] = derived
    violated = sorted(key for key, ok in derived.items() if not ok)
    if violated:
        if qualification.get("qualified") is True:
            failed.append(f"qualification.json claims qualified but its facts violate {violated}")
        else:
            not_checked.append(f"machine does not satisfy the profile: {violated}")
    elif qualification.get("qualified") is not True:
        failed.append("qualification.json facts satisfy the profile but it is not marked qualified")
    return f


def check_memory(review, steps, qual_facts, limit, failed, not_checked, facts):
    summarizer = _summarizer()
    try:
        meta, samples = summarizer.load_samples(review / "memory")
    except (OSError, ValueError):
        not_checked.append("raw memory samples (memory/meta.json, memory/samples.jsonl) missing")
        return
    if not samples:
        not_checked.append("no memory samples")
        return
    loaded = summarizer.load_phases(review / "phases.jsonl", meta)
    try:
        recomputed = summarizer.summarize(meta, samples, loaded, limit)
    except (KeyError, TypeError, ValueError) as error:
        failed.append(f"raw memory samples are malformed: {type(error).__name__}: {error}")
        return
    stored = read_json(review / "memory" / "summary.json")
    if stored is not None:
        differing = [".".join(path) for path in COMPARED if _dig(stored, path) != _dig(recomputed, path)]
        if differing:
            failed.append(f"memory/summary.json disagrees with the raw samples: {differing}")
    host = recomputed["host"]
    numbers = {"mem_total_bytes": host["mem_total_bytes"], "swap_total_bytes": host["swap_total_bytes"],
               "peak_used": (host["sampled_peak_used_excluding_reclaimable"] or {}).get("bytes"),
               "min_available": (host["sampled_min_mem_available"] or {}).get("bytes"),
               "vmstat_oom_kill_delta": recomputed["oom"].get("vmstat_oom_kill_delta")}
    facts["memory"] = {**numbers, "samples": recomputed["samples"], "duration_s": recomputed["duration_s"],
                       "interval_s": recomputed["interval_s"], "max_gap_s": recomputed["gaps_s"]["max"],
                       "oom": recomputed["oom"]}
    absent = [key for key, value in numbers.items() if value is None]
    if absent:
        not_checked.append(f"required memory figures unavailable: {absent}")
        return
    if numbers["mem_total_bytes"] > limit:
        failed.append(f"MemTotal {numbers['mem_total_bytes']} B exceeds the {limit} B profile")
    if numbers["peak_used"] > numbers["mem_total_bytes"]:
        failed.append("sampled peak exceeds MemTotal: inconsistent samples")
    if numbers["swap_total_bytes"] != 0:
        failed.append("swap was enabled during the run")
    if qual_facts is not None and qual_facts.get("mem_total_bytes") != numbers["mem_total_bytes"]:
        failed.append(f"MemTotal in samples ({numbers['mem_total_bytes']}) differs from qualification "
                      f"({qual_facts.get('mem_total_bytes')})")
    kills = {key: value for key, value in recomputed["oom"].items() if value}
    container_kills = {cid: row["oom_kill"] for cid, row in recomputed["containers"].items() if row["oom_kill"]}
    if kills or container_kills:
        failed.append(f"OOM kills recorded: {kills} {container_kills}")
    # Coverage: every step must be bracketed by samples with no gap above the allowed one.
    interval = recomputed["interval_s"] or 0.5
    allowed = max(2.0, 4 * interval)
    times = [sample["t"] for sample in samples]
    windows = {row["phase"]: (row["start_t"], row["end_t"]) for row in loaded}
    uncovered = []
    for name in EXPECTED_STEP_CODES:
        if name not in steps:
            continue  # reported as a missing step elsewhere
        if name not in windows:
            uncovered.append(f"{name}: no monotonic bounds")
            continue
        start, end = windows[name]
        before = [t for t in times if t <= start]
        after = [t for t in times if t >= end]
        if not before or not after:
            uncovered.append(f"{name}: samples do not cover the step")
            continue
        points = [before[-1]] + [t for t in times if start < t < end] + [after[0]]
        gap = max((b - a for a, b in zip(points, points[1:])), default=0.0)
        if gap > allowed:
            uncovered.append(f"{name}: gap {gap:.3f} s > {allowed} s")
    facts["memory"]["allowed_gap_s"] = allowed
    if uncovered:
        not_checked.append(f"memory sampling does not cover every step: {uncovered}")


def check_acceptance(review, source_sha, failed, not_checked, facts):
    results = sorted((review / "bundle" / "results").glob("verify-*"))
    report = read_json(results[-1] / "verify" / "report.json") if results else None
    if report is None:
        not_checked.append("rehearsal verify/report.json missing")
        return
    if source_sha and report.get("commit") != source_sha:
        failed.append(f"rehearsal report commit {report.get('commit')} is not {source_sha}")
    statuses = acceptance_statuses(report)
    facts["acceptance"] = statuses
    missing = sorted(set(ACCEPTANCE_GROUPS) - set(statuses))
    unknown = sorted(set(statuses) - set(ACCEPTANCE_GROUPS))
    bad = sorted(k for k, v in statuses.items() if v == "failed")
    open_ = {k for k, v in statuses.items() if v == "not_checked"}
    other = sorted(k for k, v in statuses.items() if v not in ("passed", "failed", "not_checked"))
    if bad:
        failed.append(f"acceptance groups failed: {bad}")
    if unknown:
        failed.append(f"unknown acceptance groups: {unknown}")
    if other:
        failed.append(f"acceptance groups with an unknown status: {other}")
    if open_ - EXPECTED_NOT_CHECKED:
        failed.append(f"unexpected not-checked groups: {sorted(open_ - EXPECTED_NOT_CHECKED)}")
    if missing:
        not_checked.append(f"acceptance groups missing from report.json: {missing}")
    if not missing and not bad and not unknown and not other and open_ == EXPECTED_NOT_CHECKED:
        passed = sum(1 for v in statuses.values() if v == "passed")
        if passed != len(ACCEPTANCE_GROUPS) - len(EXPECTED_NOT_CHECKED):
            failed.append(f"{passed} groups passed, expected {len(ACCEPTANCE_GROUPS) - len(EXPECTED_NOT_CHECKED)}")


def check_evidence(review, source_sha, failed, not_checked, facts):
    evidence = read_json(review / "evidence-summary.json")
    if not isinstance(evidence, dict):
        not_checked.append("evidence-summary.json missing")
        return
    facts["evidence_status"] = evidence.get("status")
    required = ("status", "exit_code", "failures", "missing", "source_revision", *EVIDENCE_COUNTS)
    absent = [key for key in required if key not in evidence]
    if evidence.get("failures"):
        failed.append(f"evidence checker failures: {evidence['failures']}")
    if source_sha and evidence.get("source_revision") not in (None, source_sha):
        failed.append(f"evidence source {evidence.get('source_revision')} is not {source_sha}")
    if absent:
        not_checked.append(f"evidence summary fields missing: {absent}")
        return
    if evidence["status"] != "internal_rehearsal_passed" or evidence["exit_code"] != 0 or evidence["missing"]:
        not_checked.append(f"evidence checker did not confirm the rehearsal: status={evidence['status']} "
                           f"exit_code={evidence['exit_code']} missing={evidence['missing']}")
    counts = {key: evidence[key] for key in EVIDENCE_COUNTS}
    if counts != EVIDENCE_COUNTS:
        not_checked.append(f"evidence counts {counts}, expected {EVIDENCE_COUNTS}")


def judge(review: Path, source_sha: str | None, limit: int = LIMIT_BYTES) -> dict:
    failed, not_checked, facts = [], [], {}
    qual_facts = check_qualification(read_json(review / "qualification.json"), limit, failed, not_checked, facts)
    preload = read_json(review / "preload-images.json")
    if preload is None:
        not_checked.append("pre-load image check missing")
    elif preload.get("present"):
        not_checked.append("project images existed before loading")
    steps = phases(review)
    facts["exit_codes"] = {name: row["exit_code"] for name, row in steps.items()}
    for name, code in EXPECTED_STEP_CODES.items():
        if name not in steps:
            not_checked.append(f"step {name} did not run")
        elif steps[name]["exit_code"] != code:
            failed.append(f"step {name}: exit {steps[name]['exit_code']}, expected {code}")
    if source_sha and "check" in steps and source_sha not in output(review, "check"):
        failed.append("offline.py check did not report the published source SHA")
    if "launch_cleanup" in steps and output(review, "launch_cleanup").strip():
        failed.append("containers of the launch remained after the run")
    if "demo_health" in steps and '"ok":true' not in output(review, "demo_health").replace(" ", ""):
        failed.append("demo health did not report ok")
    check_acceptance(review, source_sha, failed, not_checked, facts)
    check_evidence(review, source_sha, failed, not_checked, facts)
    events = review / "docker-events.log"
    if events.is_file():
        parsed = docker_events(events.read_text(errors="replace"))
        facts["docker_events"] = len(parsed)
        pulls = [event for event in parsed if event[0] == "image" and event[1].split(":")[0] == "pull"]
        if not parsed:
            not_checked.append("docker-events.log has no events")
        if pulls:
            failed.append(f"{len(pulls)} image pull event(s) during an offline run")
    else:
        not_checked.append("docker-events.log missing")
    check_memory(review, steps, qual_facts, limit, failed, not_checked, facts)
    verdict = "failed" if failed else ("not_checked" if not_checked else "confirmed")
    return {"verdict": verdict, "failed": failed, "not_checked": not_checked, "facts": facts,
            "statement_limits": [
                "confirmed means: on the machine described by qualification.json, the listed RC3 steps returned the "
                "documented codes without OOM kills, swap or image pulls, while the whole guest operating system "
                "was sampled across every step.",
                "The sampled host peak is not a continuous maximum; per-cgroup and container peaks are reported "
                "separately and are never summed into a host figure.",
                "For a virtual machine it measures the guest OS only, not the hypervisor host.",
                "It does not confirm OPS-02 (independent human acceptance) or the organisers' official adapter."]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("review", type=Path)
    parser.add_argument("--source-sha")
    parser.add_argument("--limit-bytes", type=int, default=LIMIT_BYTES)
    args = parser.parse_args(argv)
    result = judge(args.review, args.source_sha, args.limit_bytes)
    (args.review / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"verdict": result["verdict"], "failed": result["failed"], "not_checked": result["not_checked"]},
                     ensure_ascii=False, indent=2))
    return {"confirmed": 0, "failed": 1, "not_checked": 2}[result["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
