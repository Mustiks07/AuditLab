"""Extract reproducible measurements from trusted local experiment directories."""
import argparse
from collections import Counter
import csv
from datetime import datetime
import json
from pathlib import Path


def read(path):
    return json.loads(path.read_bytes())


def guess_boundary(snapshot):
    """Public-only shortcut: hardcode the receipt and first tranche of the boundary fixture."""
    public = snapshot / "public"
    manifest = read(public / "manifest.json")
    cutoff = datetime.fromisoformat(manifest["period_end"])
    scale = 10 ** manifest["currency_exponents"][manifest["base_currency"]]
    with (public / "ledger.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    motif = {row["payment_id"] for row in rows if row["account"] == "UNAPPLIED"
             and int(row["credit_minor"]) == 50_000 * scale}
    first_tranche = {row["payment_id"] for row in rows if row["account"] == "UNAPPLIED"
                     and int(row["debit_minor"]) == 30_000 * scale
                     and datetime.fromisoformat(row["timestamp"]) < cutoff}
    return bool(motif & first_tranche)


def summary(output):
    experiment = read(output / "experiment.json")
    search = read(output / experiment["search"])
    curriculum = read(output / experiment["curriculum"])
    pairs = experiment["paired"]
    reduction = None
    if experiment["reduction"]:
        measured = read(output / experiment["reduction"])
        selected = next((row for row in measured["proposals"] if row["snapshot"] == measured["selected_snapshot"]),
                        measured["baseline"])
        reduction = {"status": measured["status"], "candidate_id": measured["candidate_id"],
                     "target": measured["target"], "runs_used": measured["runs_used"], "cost": measured["cost"],
                     "baseline": {key: measured["baseline"][key] for key in ("sizes", "snapshot_sha256", "frequency")},
                     "selected": {key: selected[key] for key in ("sizes", "snapshot_sha256", "frequency")}}
    shortcut = {"true_positive": 0, "false_positive": 0, "true_negative": 0, "false_negative": 0}
    for pair in (pair for pair in pairs if pair["status"] == "passed"):
        snapshot = output / "pairs" / pair["parent"] / "snapshots" / pair["kind"]
        predicted = guess_boundary(snapshot)
        expected = bool(pair["expected"])
        shortcut[("true" if predicted == expected else "false") + ("_positive" if predicted else "_negative")] += 1
    return {
        "schema_version": "1.0", "artifact_id": f"experiment-summary-{experiment['agent']['kind']}",
        "source_sha": experiment["source_sha"], "protocol_id": experiment["protocol_id"],
        "plan_sha256": experiment["plan_sha256"], "partition_sha256": curriculum["partition_sha256"],
        "agent": experiment["agent"], "reason": experiment["reason"],
        "arms": {name: {key: arm[key] for key in ("offered", "accepted", "agent_runs", "cost_micros",
                                                  "weak_attempts", "scored_attempts", "stop_reason")}
                 for name, arm in search["arms"].items()},
        "paired": {"passed": sum(pair["status"] == "passed" for pair in pairs), "total": len(pairs),
                   "size_pairs": sorted({tuple(pair["public_size"]) for pair in pairs}),
                   "public_motif_guesser": shortcut},
        "exported": dict(Counter(f"{row['split']}/{row['status']}" for row in curriculum["examples"])),
        "partial_overlap": curriculum["partial_overlap"], "reduction": reduction,
    }


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("output", nargs="+", type=Path)
    print(json.dumps([summary(folder) for folder in cli.parse_args().output], indent=2, sort_keys=True))
