"""Reproducible financial stress matrix, separate from the 20-case release rehearsal.

Trusted development harness: the ordinary policy uses public observations, but runs
in process. This measures financial correctness, NOT sandbox/resource acceptance or
an official/held-out contest score. Only failed snapshots are retained; successful
inputs and answers are identified by hashes and reproducible seed/config pairs.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import itertools
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from auditlab.generator import DISTRACTORS, GENERATOR_VERSION, generate_episode
from auditlab.policies import make_policy
from auditlab.release.transforms import comparable, transform_public, transform_truth, write_json
from auditlab.runner import make_env
from auditlab.runner.episode import run_episode
from auditlab.verifier import verify_report

VERSION = "financial-stress-1.1"
TYPES = ("duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff")


def matrix(count=1000, first_seed=920001):
    if type(count) is not int or count < 1 or type(first_seed) is not int:
        raise ValueError("positive count and integer first_seed required")
    currencies = list(itertools.permutations(("KZT", "USD", "EUR", "JPY", "KWD"), 2))
    periods = (("Europe/Berlin", "2026-10"), ("America/New_York", "2026-03"),
               ("America/New_York", "2026-11"), ("Asia/Kathmandu", "2027-02"), ("UTC", "2028-02"))
    for index in range(count):
        base, foreign = currencies[index % len(currencies)]
        state = (index // len(currencies)) % 12
        zone, period = periods[(index // 7) % len(periods)]
        counts = dict.fromkeys(TYPES, 0)
        interaction = None
        if state < 8:
            interaction = "allocation_fx_same_payment" if state < 4 else "cutoff_fx_same_invoice"
            counts["fx_conversion"] = state % 2
            counts["payment_allocation" if state < 4 else "period_cutoff"] = (state % 4) // 2
        elif state < 11:
            counts = dict.fromkeys(TYPES, state - 7)
        # Include all three practical sizes without correlating size to error type.
        rows, docs = ((240, 76), (800, 140), (2000, 200))[(index // 11) % 3]
        yield {"id": f"G{index + 1:04d}", "seed": first_seed + index, "config": {
            "config_version": "1.1", "base_currency": base, "currencies": [base, foreign],
            "timezone": zone, "target_period": period, "issue_counts": counts,
            "clean_distractors": [*DISTRACTORS, *(["next_day_settlement"] if index % 2 else [])],
            "interaction": interaction, "company_size": {"ledger_rows_target": rows, "documents_target": docs}}}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def compare_reference(findings, truth):
    def key(f, amount):
        return f["type"], tuple(sorted(f["affected_ids"])), f[amount], f["currency"]
    expected = Counter(key(f, "expected_amount_minor") for f in truth["issues"])
    actual = Counter(key(f, "claimed_amount_minor") for f in findings)
    problems = []
    if expected != actual:
        problems.append("financial_findings_differ")
    for finding in findings:
        matches = [f for f in truth["issues"] if key(f, "expected_amount_minor") == key(finding, "claimed_amount_minor")]
        if not any(set(finding["evidence_ids"]) == set(group) for f in matches for group in f["evidence_sets"]):
            problems.append("evidence_differs")
    return sorted(set(problems))


def ordinary(snapshot, export):
    env = make_env("train", evaluator="inprocess")
    try:
        result = run_episode(env, make_policy("reconcile", 314159), 0, {
            "snapshot_dir": str(snapshot), "export_dir": str(export), "policy_name": "reconcile"})
        final = env.final
        if result[4].get("status") != "submitted" or not final or final.get("persistence"):
            raise ValueError("ordinary policy did not submit and persist successfully")
        answer = json.loads((export / "submission.json").read_text())
        score = json.loads((export / "score.json").read_text())
        return answer["findings"], score
    finally:
        env.close()


def check_case(case, directory, paired=False):
    snapshot = directory / "snapshot"
    generate_episode(case["seed"], case["config"], snapshot)
    # Compute/freeze the independent answer before opening the reference file.
    independent = verify_report(snapshot / "public", engine="sqlite")
    write_json(directory / "independent-before-reference.json", independent)
    truth = json.loads((snapshot / "private" / "truth.json").read_text())
    problems = compare_reference(independent["findings"], truth)
    if independent["notes"]:
        problems.append("independent_notes")
    findings, score = ordinary(snapshot, directory / "ordinary")
    problems += ["ordinary_" + reason for reason in compare_reference(findings, truth)]
    if (score["tp"], score["fp"], score["fn"]) != (len(truth["issues"]), 0, 0):
        problems.append("ordinary_score_differs")
    pair = None
    if paired:
        target = directory / "transformed"
        mapping = transform_public(snapshot / "public", target / "public", case["seed"] + 1000000)
        (target / "private").mkdir()
        write_json(target / "private" / "truth.json", transform_truth(truth, mapping))
        other, other_score = ordinary(target, directory / "ordinary-transformed")
        inverse = {new: old for old, new in mapping["references"].items()}
        pair = comparable(findings) == comparable(other, inverse) and all(
            score[k] == other_score[k] for k in ("tp", "fp", "fn"))
        if not pair:
            problems.append("ordinary_id_order_pair_differs")
    return {**case, "status": "failed" if problems else "passed", "problems": sorted(set(problems)),
            "input_sha256": independent["input_sha256"], "answer_sha256": digest(independent["findings"]),
            "issue_count": len(truth["issues"]), "score": score, "pair_passed": pair}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--first-seed", type=int, default=920001)
    parser.add_argument("--pair-every", type=int, default=25)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    cases = list(matrix(args.count, args.first_seed))
    if args.pair_every < 1:
        parser.error("pair-every must be positive")
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"format_version": VERSION, "generator_version": GENERATOR_VERSION,
              "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)),
              "scope": __doc__, "planned": len(cases), "selection_sha256": digest(cases), "cases": []}
    write_json(args.output / "selection.json", cases)
    started = time.monotonic()
    for index, case in enumerate(cases):
        with TemporaryDirectory(prefix="auditlab-stress-") as temporary:
            directory = Path(temporary)
            try:
                row = check_case(case, directory, paired=index % args.pair_every == 0)
            except Exception as error:
                row = {**case, "status": "failed", "problems": [f"{type(error).__name__}: {error}"]}
            if row["status"] != "passed":
                shutil.copytree(directory, args.output / "failures" / case["id"])
            report["cases"].append(row)
        if (index + 1) % 50 == 0:
            print(f"{index + 1}/{len(cases)} checked", flush=True)
        report["status"] = "in_progress"
        write_json(args.output / "report.json", report)
    report.update(status="passed" if all(r["status"] == "passed" for r in report["cases"]) else "failed",
                  elapsed_seconds=time.monotonic() - started,
                  passed=sum(r["status"] == "passed" for r in report["cases"]),
                  pairs_passed=sum(r.get("pair_passed") is True for r in report["cases"]))
    write_json(args.output / "report.json", report)
    print(json.dumps({k: report[k] for k in ("status", "planned", "passed", "pairs_passed", "elapsed_seconds")}))
    return int(report["status"] != "passed")


if __name__ == "__main__":
    sys.exit(main())
