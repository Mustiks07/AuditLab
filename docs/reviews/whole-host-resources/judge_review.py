#!/usr/bin/env python3
"""Verdict for one run_review.sh directory: confirmed / failed / not_checked (standard library only).

    python3.11 judge_review.py REVIEW_DIR [--source-sha SHA]

Reads only the files the review saved. "confirmed" needs a qualifying machine, every HANDOFF step with
its documented exit code, no image pull, no OOM kill and complete memory records. A strict-acceptance
exit 2 of the rehearsal is expected only if OPS-01/OPS-02 are its sole not-checked groups.
Exit code: 0 confirmed, 1 failed, 2 not_checked.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

EXPECTED_NOT_CHECKED = {"OPS-01", "OPS-02"}


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


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


def judge(review: Path, source_sha: str | None) -> dict:
    failed, not_checked, facts = [], [], {}
    qualification = read_json(review / "qualification.json")
    if not qualification:
        not_checked.append("qualification.json missing")
    elif not qualification.get("qualified"):
        not_checked.append("machine does not satisfy the profile: " +
                           ", ".join(k for k, v in qualification["checks"].items() if not v))
    preload = read_json(review / "preload-images.json")
    if preload is None:
        not_checked.append("pre-load image check missing")
    elif preload.get("present"):
        not_checked.append("project images existed before loading")
    steps = phases(review)
    facts["exit_codes"] = {name: row["exit_code"] for name, row in steps.items()}
    expected = {"check": 0, "rehearsal": 2, "image_runtime": 0, "evidence_checker": 0, "launch_cleanup": 0,
                "demo_start": 0, "demo_health": 0, "demo_runs": 0, "crosscheck_live": 0, "replay_check": 0}
    for name, code in expected.items():
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
    results = sorted((review / "bundle" / "results").glob("verify-*"))
    report = read_json(results[-1] / "verify" / "report.json") if results else None
    if report is None:
        not_checked.append("rehearsal verify/report.json missing")
    else:
        statuses = acceptance_statuses(report)
        facts["acceptance"] = statuses
        bad = sorted(k for k, v in statuses.items() if v in ("failed", "провалено"))
        open_ = {k for k, v in statuses.items() if v in ("not_checked", "не проверено")}
        if bad:
            failed.append(f"acceptance groups failed: {bad}")
        if open_ - EXPECTED_NOT_CHECKED:
            failed.append(f"unexpected not-checked groups: {sorted(open_ - EXPECTED_NOT_CHECKED)}")
        if not statuses:
            not_checked.append("acceptance matrix not found in report.json")
    evidence = read_json(review / "evidence-summary.json")
    facts["evidence_status"] = evidence.get("status") if evidence else None
    if evidence is None:
        not_checked.append("evidence-summary.json missing")
    elif evidence.get("failures") or evidence.get("missing"):
        failed.append(f"evidence checker: failures={evidence.get('failures')} missing={evidence.get('missing')}")
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
    memory = read_json(review / "memory" / "summary.json")
    if memory is None:
        not_checked.append("memory summary missing")
    else:
        facts["memory"] = {"mem_total_bytes": memory["host"]["mem_total_bytes"],
                           "swap_total_bytes": memory["host"]["swap_total_bytes"],
                           "sampled_peak_used_excluding_reclaimable": memory["host"]["sampled_peak_used_excluding_reclaimable"],
                           "sampled_min_mem_available": memory["host"]["sampled_min_mem_available"],
                           "interval_s": memory["interval_s"], "duration_s": memory["duration_s"],
                           "max_gap_s": memory["gaps_s"]["max"], "oom": memory["oom"]}
        kills = {k: v for k, v in memory["oom"].items() if v}
        container_kills = {cid: row["oom_kill"] for cid, row in memory["containers"].items() if row["oom_kill"]}
        if kills or container_kills:
            failed.append(f"OOM kills recorded: {kills} {container_kills}")
        if memory["host"]["swap_total_bytes"] not in (0, None):
            not_checked.append("swap was enabled during the run")
        if memory["gaps_s"]["max"] is not None and memory["gaps_s"]["max"] > 10 * (memory["interval_s"] or 1):
            not_checked.append(f"sampling gap {memory['gaps_s']['max']} s exceeds 10 intervals")
    verdict = "failed" if failed else ("not_checked" if not_checked else "confirmed")
    return {"verdict": verdict, "failed": failed, "not_checked": not_checked, "facts": facts,
            "statement_limits": [
                "confirmed means: this machine (see qualification.json) ran the listed RC3 steps with the documented "
                "codes, without OOM kills, swap or image pulls, while the whole guest OS was sampled.",
                "The sampled host peak is not a continuous maximum; container peaks are reported separately and are "
                "not summed into a host figure.",
                "It does not confirm OPS-02 (independent human acceptance) or the organisers' official adapter."]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("review", type=Path)
    parser.add_argument("--source-sha")
    args = parser.parse_args(argv)
    result = judge(args.review, args.source_sha)
    (args.review / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"verdict": result["verdict"], "failed": result["failed"], "not_checked": result["not_checked"]},
                     ensure_ascii=False, indent=2))
    return {"confirmed": 0, "failed": 1, "not_checked": 2}[result["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
