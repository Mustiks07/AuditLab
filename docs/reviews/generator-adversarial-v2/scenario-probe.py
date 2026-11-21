"""Measure a public posting-date shortcut against financial reconciliation for period cutoff."""

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from auditlab.environment import open_session
from auditlab.evaluator.matcher import score_submission
from auditlab.generator import generate_episode
from auditlab.policies import make_policy
from auditlab.runner import make_env
from auditlab.runner.episode import run_episode
from auditlab.verifier.core import verify_report


SEEDS = range(915231, 915239)
THRESHOLD_SEEDS = range(915261, 915277)
PROFILES = (
    ("Asia/Almaty", "2028-02", 200, 20),
    ("America/New_York", "2028-11", 480, 76),
    ("Pacific/Auckland", "2028-09", 2_000, 200),
    ("Europe/Berlin", "2028-03", 480, 76),
)
STATES = (
    ("standalone-clean", None, 0, 0),
    ("standalone-cutoff", None, 1, 0),
    ("combined-00", "cutoff_fx_same_invoice", 0, 0),
    ("combined-10", "cutoff_fx_same_invoice", 1, 0),
    ("combined-01", "cutoff_fx_same_invoice", 0, 1),
    ("combined-11", "cutoff_fx_same_invoice", 1, 1),
)


def dump(path, payload):
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")


def load(path):
    return json.loads(path.read_bytes())


def records(session, table):
    cursor = None
    while True:
        reply = session.inspect({"action": "list_records", "args": {
            "table": table, "cursor": cursor, "limit": 50}})
        assert reply["ok"], reply
        result = reply["result"]
        yield from result["records"]
        cursor = result["next_cursor"]
        if cursor is None:
            return


def shortcut(public):
    """Reads posting date, never invoice issue time, and does not know a private path."""
    session = open_session(public)
    manifest = load(public / "manifest.json")
    end = datetime.fromisoformat(manifest["period_end"])
    invoices = list(records(session, "invoices"))
    documents = {doc["invoice_id"]: doc["doc_id"] for doc in invoices}
    findings = []
    for row in records(session, "ledger"):
        key = row["invoice_id"]
        if (row["account"] == "AR" and row["payment_id"] is None and row["debit_minor"] > 0
                and key in documents and datetime.fromisoformat(row["timestamp"]) >= end):
            findings.append({
                "type": "period_cutoff",
                "affected_ids": [f"invoice/{key}"],
                "claimed_amount_minor": row["debit_minor"],
                "currency": row["currency"],
                "evidence_ids": [f"invoices/{documents[key]}", f"ledger/{row['row_id']}", "policy/period"],
                "explanation": "Posting after the target period.",
            })
    inspections = max(1, (manifest["ledger_rows"] + 49) // 50) + max(1, (len(invoices) + 49) // 50)
    return {"episode_id": manifest["episode_id"], "findings": findings}, inspections


def make_neighbor_control(output, baseline):
    control = output / "neighbor-control"
    public = control / "public"
    shutil.copytree(baseline, public)
    manifest = load(public / "manifest.json")
    session = open_session(public)
    end = datetime.fromisoformat(manifest["period_end"])
    invoices = list(records(session, "invoices"))
    by_invoice = {doc["invoice_id"]: doc for doc in invoices}
    row, = (row for row in records(session, "ledger")
            if row["account"] == "AR" and row["payment_id"] is None and row["debit_minor"]
            and row["invoice_id"] in by_invoice
            and datetime.fromisoformat(by_invoice[row["invoice_id"]]["issued_at"]) < end
            <= datetime.fromisoformat(row["timestamp"]))
    invoice = by_invoice[row["invoice_id"]]
    before = invoice["issued_at"]
    invoice["issued_at"] = row["timestamp"]
    invoice["due_at"] = (datetime.fromisoformat(row["timestamp"]) + timedelta(days=14)).isoformat()
    with (public / "invoices.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
        for doc in invoices:
            stream.write(json.dumps(doc, ensure_ascii=False) + "\n")
    manifest["episode_id"] = "SCENARIO-NEIGHBOR"
    dump(public / "manifest.json", manifest)
    outcome = {
        "boundary": end.isoformat(),
        "affected_invoice": f"invoice/{invoice['invoice_id']}",
        "invoice_ref": f"invoices/{invoice['doc_id']}",
        "recognition_ref": f"ledger/{row['row_id']}",
        "issue_time_faulty": before,
        "issue_time_clean": invoice["issued_at"],
        "recognition_time_both": row["timestamp"],
        "nominal_minor": row["debit_minor"],
        "currency": row["currency"],
        "expected_cutoff_count_faulty": 1,
        "expected_cutoff_count_clean": 0,
    }
    return control, outcome


def build(output):
    output.mkdir(parents=True)
    matrix = {}
    for index, seed in enumerate(SEEDS):
        zone, period, rows, docs = PROFILES[index % len(PROFILES)]
        for name, interaction, cutoff, fx in STATES:
            key = f"{seed}-{name}"
            config = {
                "config_version": "1.1",
                "company_size": {"ledger_rows_target": rows, "documents_target": docs},
                "base_currency": "KZT",
                "currencies": ["KZT", "USD", "KWD"],
                "timezone": zone,
                "target_period": period,
                "issue_counts": {"period_cutoff": cutoff, "fx_conversion": fx},
                "snapshot_nonce": f"cutoff-probe-{key}",
            }
            if interaction:
                config["interaction"] = interaction
            matrix[key] = (seed, config, cutoff)
    dump(output / "matrix.json", matrix)

    public_results = {}
    for key, (seed, config, _cutoff) in matrix.items():
        episode = output / key
        generate_episode(seed, config, episode)
        public_results[key] = shortcut(episode / "public")
    key = f"{SEEDS[0]}-standalone-cutoff"
    control, arithmetic = make_neighbor_control(output, output / key / "public")
    control_submission, steps = shortcut(control / "public")
    public_results[control.name] = control_submission, steps
    dump(output / "control-answer.json", arithmetic)
    dump(output / "predictions.json", public_results)
    print(f"Predictions SHA-256: {hashlib.sha256((output / 'predictions.json').read_bytes()).hexdigest()}")
    print(f"Control SHA-256: {hashlib.sha256((output / 'control-answer.json').read_bytes()).hexdigest()}")


def score_counts(score):
    return {key: score[key] for key in ("tp", "fp", "fn")}


def grade(output):
    matrix = load(output / "matrix.json")
    predictions = load(output / "predictions.json")
    counts = {"tp": 0, "fp": 0, "fn": 0}
    by_state = {}
    for key, (_, _, expected_count) in matrix.items():
        truth = load(output / key / "private" / "truth.json")
        truth["issues"] = [issue for issue in truth["issues"] if issue["type"] == "period_cutoff"]
        assert len(truth["issues"]) == expected_count, key
        submission, steps = predictions[key]
        score = score_counts(score_submission(submission, truth, steps))
        bucket = by_state.setdefault(key.split("-", 1)[1], dict.fromkeys(counts, 0))
        for field, value in score.items():
            counts[field] += value
            bucket[field] += value

    control = output / "neighbor-control"
    public = control / "public"
    sql = verify_report(public, engine="sqlite")
    assert sql["findings"] == [] and sql["notes"] == [], sql
    submission, steps = predictions[control.name]
    # This trusted assertion is the hand-established clean control label, not an input to a policy.
    truth = {"episode_id": submission["episode_id"], "issues": []}
    control_score = score_counts(score_submission(submission, truth, steps))
    assert control_score["fp"] >= 1
    (control / "private").mkdir(exist_ok=True)
    dump(control / "private" / "truth.json", truth)
    env = make_env("train")
    try:
        _, reward, terminated, truncated, info = run_episode(
            env, make_policy("reconcile", 41), SEEDS[0], {"snapshot_dir": str(control)})
        assert (terminated, truncated, info["status"]) == (True, False, "submitted")
        assert score_counts(info["score"]) == {"tp": 0, "fp": 0, "fn": 0}
        assert env.final["submission"]["findings"] == []
    finally:
        env.close()

    result = {"episodes": len(matrix), "shortcut": counts, "by_state": by_state,
              "control": {"posting_shortcut": control_score, "reconcile": score_counts(info["score"]),
                          "reconcile_reward": reward, "sqlite_findings": len(sql["findings"]),
                          "sqlite_notes": len(sql["notes"])}}
    dump(output / "results.json", result)
    print(json.dumps(result, indent=2))


def threshold(output):
    output.mkdir(parents=True)
    selections = [(seed, faults) for seed in THRESHOLD_SEEDS for faults in (0, 1)]
    for seed, faults in selections:
        snapshot = output / f"{seed}-{faults}"
        generate_episode(seed, {
            "config_version": "1.1", "timezone": "UTC", "target_period": "2028-03",
            "company_size": {"ledger_rows_target": 240, "documents_target": 76},
            "interaction": "cutoff_fx_same_invoice",
            "issue_counts": {"period_cutoff": faults, "fx_conversion": faults},
        }, snapshot)

    public_counts = {}
    issue_time_candidates = {}
    for seed, faults in selections:
        snapshot = output / f"{seed}-{faults}"
        public = snapshot / "public"
        manifest = load(public / "manifest.json")
        end = datetime.fromisoformat(manifest["period_end"])
        session = open_session(public)
        issued = {doc["invoice_id"]: datetime.fromisoformat(doc["issued_at"])
                  for doc in records(session, "invoices")}
        recognitions = [row for row in records(session, "ledger")
                        if row["account"] == "AR" and row["debit_minor"] > 0 and row["payment_id"] is None]
        public_counts[snapshot.name] = sum(
            row["currency"] != manifest["base_currency"] and datetime.fromisoformat(row["timestamp"]) >= end
            for row in recognitions)
        issue_time_candidates[snapshot.name] = sorted({
            f"invoice/{row['invoice_id']}" for row in recognitions
            if datetime.fromisoformat(row["timestamp"]) != issued[row["invoice_id"]]})
    dump(output / "public-counts.json", public_counts)
    dump(output / "public-time-inequality.json", issue_time_candidates)
    print(f"Public count SHA-256: {hashlib.sha256((output / 'public-counts.json').read_bytes()).hexdigest()}")

    distribution = {"clean": {}, "faulty": {}}
    totals = dict.fromkeys(("tp", "fp", "fn", "tn"), 0)
    inequality = dict.fromkeys(("tp", "fp", "fn"), 0)
    for key, count in public_counts.items():
        truth = load(output / key / "private" / "truth.json")
        expected = {issue["affected_ids"][0] for issue in truth["issues"] if issue["type"] == "period_cutoff"}
        proposed = set(issue_time_candidates[key])
        inequality["tp"] += len(proposed & expected)
        inequality["fp"] += len(proposed - expected)
        inequality["fn"] += len(expected - proposed)
        cutoff = bool(expected)
        by_outcome = distribution["faulty" if cutoff else "clean"]
        by_outcome[count] = by_outcome.get(count, 0) + 1
        result = ("tp" if count >= 2 else "fn") if cutoff else ("fp" if count >= 2 else "tn")
        totals[result] += 1
    overlapping = sorted(set(distribution["clean"]) & set(distribution["faulty"]))
    result = {"episodes": len(selections), "threshold": totals, "counts_by_outcome": distribution,
              "overlapping_counts": overlapping, "exact_time_inequality_by_affected": inequality}
    dump(output / "results.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    phases = {"build": build, "grade": grade, "threshold": threshold}
    parser.add_argument("phase", choices=tuple(phases))
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    phases[args.phase](args.output)
