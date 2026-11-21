"""Bind a CI test receipt to a clean checkout and the reviewed PR head/base.

This validates provenance and executed tests, not Issue acceptance or permission to merge.
Reviewers must obtain current expected SHAs from GitHub, independently of the artifact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SUITES = {"foundation", "integration", "simulator", "evaluator", "runner", "verifier", "metamorphic", "demo"}


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("expected a full 40-character Git SHA")
    return value


def capture(repo, event, event_name):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()
    if git("status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("review checkout must be clean")
    checkout = sha(git("rev-parse", "HEAD"))
    # cat-file retains parent headers even in actions/checkout's shallow clone.
    headers = git("cat-file", "commit", checkout).split("\n\n", 1)[0].splitlines()
    parents = [line[7:] for line in headers if line.startswith("parent ")]
    if event_name == "pull_request":
        pr = event["pull_request"]
        head, base = sha(pr["head"]["sha"]), sha(pr["base"]["sha"])
        if parents != [base, head]:
            raise ValueError("checkout is not the merge of the event's exact base and head")
        kind = "pull_request_merge"
    elif event_name in {"push", "workflow_dispatch"}:
        head = sha(event.get("after", checkout))
        if checkout != head:
            raise ValueError("checkout differs from the event revision")
        base, kind = None, "revision"
    else:
        raise ValueError("unsupported review event")
    return {"format_version": "1.0", "kind": kind, "head_sha": head, "base_sha": base,
            "checkout_sha": checkout, "tree_sha": sha(git("rev-parse", "HEAD^{tree}")),
            "parents": parents, "dirty": False}


def validate(context, report, expected_head, expected_base=None, include_research=False):
    failures = []
    def require(condition, reason):
        if not condition:
            failures.append(reason)
    sha(expected_head)
    if expected_base is not None:
        sha(expected_base)
    require(context.get("format_version") == "1.0", "unsupported context format")
    require(context.get("head_sha") == expected_head, "stale or unrelated head")
    require(context.get("dirty") is False and report.get("dirty") is False, "checkout was not clean")
    sha(context.get("checkout_sha"))
    sha(context.get("tree_sha"))
    if context.get("kind") == "pull_request_merge":
        require(expected_base is not None and context.get("base_sha") == expected_base,
                "current PR base is required and must match")
        require(context.get("parents") == [expected_base, expected_head], "merge parents differ")
    else:
        require(context.get("kind") == "revision" and expected_base is None and
                context.get("base_sha") is None and context.get("checkout_sha") == expected_head,
                "invalid revision context")
    require(report.get("commit") == context.get("checkout_sha"), "report belongs to another checkout")
    require(report.get("suite") == "quick" and report.get("exit_code") == 0 and
            report.get("status") in {"passed", "incomplete"}, "quick verification did not finish successfully")
    rows = report.get("checks", [])
    require(isinstance(rows, list), "invalid suite list")
    if not isinstance(rows, list):
        rows = []
    expected_suites = SUITES | ({"genesis_acceptance", "research_agents", "research_training", "research_acceptance", "curriculum", "research_integration"} if include_research else set())
    require(len(rows) == len(expected_suites) and all(isinstance(row, dict) for row in rows) and
            {row.get("id") for row in rows if isinstance(row, dict)} == expected_suites,
            "functional suite coverage differs")
    count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        records, n = row.get("records"), row.get("tests_run")
        valid = isinstance(records, list) and type(n) is int and n > 0 and len(records) == n
        require(row.get("status") == "passed" and valid, f"{row.get('id')}: incomplete executed tests")
        if not valid:
            continue
        ids = [r.get("id") if isinstance(r, dict) else None for r in records]
        require(all(isinstance(i, str) and i for i in ids) and len(set(ids)) == n,
                f"{row.get('id')}: missing or duplicate test identities")
        require(all(isinstance(r, dict) and r.get("status") == "passed" for r in records),
                f"{row.get('id')}: failed or skipped test")
        count += n
    return {"status": "failed" if failures else "tests_verified", "exit_code": int(bool(failures)),
            "head_sha": expected_head, "base_sha": expected_base, "checkout_sha": context.get("checkout_sha"),
            "tests": count, "failures": failures,
            "scope": "CI tests for these revisions only; Issue acceptance and merge decision require review"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("capture")
    make.add_argument("--repo", type=Path, default=ROOT)
    make.add_argument("--event", type=Path, required=True)
    make.add_argument("--event-name", required=True)
    make.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("check")
    verify.add_argument("--context", type=Path, required=True)
    verify.add_argument("--report", type=Path, required=True)
    verify.add_argument("--expected-head", required=True)
    verify.add_argument("--expected-base")
    verify.add_argument("--include-research", action="store_true")
    verify.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "capture":
            result = capture(args.repo, json.loads(args.event.read_text()), args.event_name)
        else:
            result = validate(json.loads(args.context.read_text()), json.loads(args.report.read_text()),
                              args.expected_head, args.expected_base or None, include_research=args.include_research)
            result["report_sha256"] = hashlib.sha256(args.report.read_bytes()).hexdigest()
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as error:
        result = {"status": "failed", "exit_code": 1, "failures": [str(error)]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))
    return result.get("exit_code", 0)


if __name__ == "__main__":
    sys.exit(main())
