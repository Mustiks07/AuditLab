"""Publish aggregate study results and check public allocation motifs."""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime
import json
from pathlib import Path


def read(path):
    return json.loads(path.read_bytes())


def allocation_shapes(episode, answer):
    public = episode / "public"
    cutoff = datetime.fromisoformat(read(public / "manifest.json")["period_end"])
    payments = {payment["payment_id"]: payment for payment in map(
        json.loads, (public / "payments.jsonl").read_text(encoding="utf-8").splitlines())}
    moves = defaultdict(list)
    with (public / "ledger.csv").open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            if (row["account"] == "UNAPPLIED" and int(row["debit_minor"]) > 0
                    and datetime.fromisoformat(row["timestamp"]) < cutoff):
                moves[row["payment_id"]].append(int(row["debit_minor"]))
    for finding in answer["findings"]:
        if finding["type"] != "payment_allocation":
            continue
        [key] = [value.removeprefix("payment/") for value in finding["affected_ids"] if value.startswith("payment/")]
        payment = payments[key]
        yield (payment["currency"], payment["amount_minor"], sum(moves[key]), len(moves[key]))


def summarize(output):
    study = read(output / "study.json")
    summaries = []
    audited = set()
    shapes = Counter()
    orders = {}
    for goal in study["goals"]:
        name = goal["objective"]
        search = read(output / name / "search/search.json")
        orders[name] = {}
        arms = {}
        for arm, result in goal["arms"].items():
            rows = search["arms"][arm]["candidates"]
            orders[name][arm] = [(row["proposal"]["coordinate"], row["status"], row["reason"]) for row in rows]
            checks = Counter(row["validation"]["status"] for row in rows)
            controls = result["controls"]
            arm_stats = {key: value for key, value in result.items() if key != "controls"}
            arm_stats.update(validation=dict(checks), validation_api_cost_micros=0,
                             independently_confirmed_pairs=sum(pair["status"] == "passed" for pair in controls),
                             paired_matches=sum(pair.get("repeated") is True for pair in controls),
                             paired_submitted=sum(pair.get("repeated") is not None for pair in controls),
                             paired_elapsed_seconds=sum((pair.get("attempt") or {}).get("elapsed_seconds", 0)
                                                        for pair in controls))
            arms[arm] = arm_stats
            for row in rows:
                candidate = row["candidate_id"]
                if candidate in audited or row["validation"]["status"] != "passed":
                    continue
                audited.add(candidate)
                path = Path(row["validation"]["artifact_ref"])
                shapes.update(allocation_shapes(output / "cases" / candidate / "s", read(path.parent / "answer_before_truth.json")))
        reduction = goal["reduction"]
        if reduction:
            measured = read(output / name / "reduction/reduction.json")
            selected = next((row for row in measured["proposals"] if row["snapshot"] == measured["selected_snapshot"]),
                            measured["baseline"])
            reduction = {**reduction, "target": reduction["target"]["reason"],
                         "baseline_sizes": measured["baseline"]["sizes"],
                         "baseline_frequency": measured["baseline"]["frequency"],
                         "selected_frequency": selected["frequency"]}
        summaries.append({"objective": name, "agent": goal["agent"], "arms": arms, "reduction": reduction,
                          "matched_train_examples_relating_to_goal": goal["matched_examples_per_arm"],
                          "wall_seconds": goal["elapsed_seconds"],
                          "admissible_coordinates": search["plan"]["proposal_space"]["coordinate_count"]})
    pairs = study["pair_validation"]
    return {"schema_version": "2.0", "artifact_id": "financial-families-v2-summary",
            "source_sha": study["source_sha"], "protocol_id": study["protocol_id"],
            "plan_sha256": study["plan_sha256"], "partition_sha256": study["partition_sha256"],
            "status": study["status"], "agent_kind": study["runner"]["kind"], "goals": summaries,
            "proposal_orders": orders, "exports": study["exports"],
            "financial_pairs": {"status": dict(Counter(row["status"] for row in pairs)),
                                "kinds": dict(Counter(row.get("kind") for row in pairs if row["status"] == "passed")),
                                "solver_actions": sorted({row["solver_actions"] for row in pairs if row["status"] == "passed"})},
            "public_allocation_shapes": {
                "independently_accepted_candidates": len(audited),
                "distinct_outstanding_shapes": len(shapes), "outstanding_examples": sum(shapes.values()),
                "fixed_50000_30000_kzt": shapes["KZT", 5_000_000, 3_000_000, 1],
                "exact_3_over_5": sum(count for (_, amount, applied, _), count in shapes.items() if applied * 5 == amount * 3),
                "currencies": sorted({shape[0] for shape in shapes}),
                "applied_tranche_counts": sorted({shape[3] for shape in shapes}),
            }}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.output.resolve()), indent=2, sort_keys=True))
