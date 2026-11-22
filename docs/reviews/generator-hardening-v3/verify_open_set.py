"""Independent checks of the open-1.3 set and byte compatibility of the published dataset 1.1.

Run from the repository root (pinned tzdata, e.g. in the runtime image):
    python docs/reviews/generator-hardening-v3/verify_open_set.py --work /tmp/v3 --out verification.json

Per snapshot:
* review.agent_generalization.validator.validate_candidate (Claude Pro 2): own answer from public/
  before truth, clean source book, explained mutation, evidence available through the judge adapter,
  listing witness and independent public solver within 100 actions, regeneration from provenance;
* tools/check_source_books.py: SQL verifier on standalone copies of the source and final books.
Set level: rebuild open-1.3 and datasets/open (1.1) from their recipes and compare every byte.
Organizer scenarios: the integrator's D01-D20 settings (review/genesis_acceptance/data_checks.py,
explicit 1.1 configs) regenerated as profile 1.3 with a per-episode test key, then the same checks.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import judge_adapter  # noqa: E402
from check_source_books import check_source_book  # noqa: E402
from datasets import build as build_11  # noqa: E402
from datasets import build_13  # noqa: E402
from review.agent_generalization.validator import validate_candidate  # noqa: E402
from review.genesis_acceptance.data_checks import SCENARIOS  # noqa: E402

OPEN_13 = ROOT / "datasets" / "open-1.3"
OPEN_11 = ROOT / "datasets" / "open"


def describe(episode: Path) -> dict:
    public = episode / "public"
    manifest = json.loads((public / "manifest.json").read_text(encoding="utf-8"))
    with (public / "ledger.csv").open(encoding="utf-8") as stream:
        ledger = list(csv.DictReader(stream))
    invoices = (public / "invoices.jsonl").read_text(encoding="utf-8").splitlines()
    payments = (public / "payments.jsonl").read_text(encoding="utf-8").splitlines()
    truth = json.loads((episode / "private" / "truth.json").read_text(encoding="utf-8"))
    provenance = json.loads((episode / "private" / "provenance.json").read_text(encoding="utf-8"))
    return {"ledger_rows": len(ledger), "invoices": len(invoices), "payments": len(payments),
            "pages_at_50": {"ledger": -(-len(ledger) // 50), "invoices": -(-len(invoices) // 50),
                            "payments": -(-len(payments) // 50)},
            "timezone": manifest["timezone"], "period": manifest["period_start"][:7],
            "currencies": sorted(manifest["currency_exponents"]),
            "distinct_amounts": len({row["debit_minor"] + "/" + row["credit_minor"] for row in ledger}),
            "event_kinds": dict(sorted(Counter(e["kind"] for e in provenance["events"]).items())),
            "truth": dict(sorted(Counter(i["type"] for i in truth["issues"]).items())),
            "randomness": provenance.get("randomness", {}).get("mode")}


def tree_equal(a: Path, b: Path) -> bool:
    return build_13.tree_sha256(a) == build_13.tree_sha256(b)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    args.work.mkdir(parents=True, exist_ok=True)
    recipe = build_13.load_recipe()
    names = {entry["lock"]["episode_id"]: (split, entry["name"], entry["category"])
             for split, entries in recipe["splits"].items() for entry in entries}
    snapshots = []
    for split in ("train", "dev"):
        for episode in sorted((OPEN_13 / split).iterdir()):
            _, name, category = names[episode.name]
            validation = validate_candidate(episode, adapter=judge_adapter, work=args.work / episode.name)
            books = check_source_book(episode)
            checks = {key: value["status"] for key, value in validation["checks"].items()}
            solver = validation["checks"].get("independent_solver", {})
            witness = validation["checks"].get("witness_trajectory", {})
            snapshots.append({"split": split, "episode_id": episode.name, "name": name, "category": category,
                              **describe(episode), "validator": validation["status"], "validator_checks": checks,
                              "witness_actions": witness.get("submit_action"),
                              "solver": {key: solver.get(key) for key in ("status", "actions", "score", "elapsed_s")
                                         if key in solver},
                              "source_books": books["status"]})
            print(json.dumps({k: snapshots[-1][k] for k in ("split", "name", "validator", "source_books",
                                                              "ledger_rows", "witness_actions")}))
    scenarios = []
    for did, (seed, config, meaning) in SCENARIOS.items():
        key = hashlib.sha256(f"test key {did}".encode()).hexdigest()[:32]  # self-made, not a jury key
        episode = args.work / "scenarios-1.3" / did
        if not episode.exists():
            judge_adapter.generate_episode(seed, {**config, "config_version": "1.3", "episode_key": key}, str(episode))
        validation = validate_candidate(episode, adapter=judge_adapter, work=args.work / f"{did}.validation")
        scenarios.append({"id": did, "meaning": meaning, **describe(episode), "validator": validation["status"],
                          "validator_checks": {k: v["status"] for k, v in validation["checks"].items()},
                          "source_books": check_source_book(episode)["status"]})
        print(json.dumps({k: scenarios[-1][k] for k in ("id", "validator", "source_books", "ledger_rows")}))
    with tempfile.TemporaryDirectory() as tmp:
        rebuilt_13 = build_13.check(OPEN_13)
        old = Path(tmp) / "open-1.1"
        build_11.build_datasets(old)
        old_names = sorted(f"{s}/{e.name}" for s in ("train", "dev") for e in (OPEN_11 / s).iterdir())
        old_equal = [name for name in old_names if tree_equal(OPEN_11 / name, old / name)]
    report = {
        "open_1_3": {"snapshots": len(snapshots),
                     "validator_passed": sum(s["validator"] == "passed" for s in snapshots),
                     "source_books_passed": sum(s["source_books"] == "passed" for s in snapshots),
                     "rebuild": rebuilt_13},
        "open_1_1_rebuild": {"snapshots": len(old_names), "byte_identical": len(old_equal)},
        "organizer_scenarios_1_3": {"snapshots": len(scenarios),
                                    "validator_passed": sum(s["validator"] == "passed" for s in scenarios),
                                    "source_books_passed": sum(s["source_books"] == "passed" for s in scenarios)},
        "snapshots": snapshots,
        "scenarios": scenarios,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({k: v for k, v in report.items() if k not in ("snapshots", "scenarios")}, indent=1))
    return 0 if (report["open_1_3"]["validator_passed"] == report["open_1_3"]["source_books_passed"] == len(snapshots)
                 and rebuilt_13["status"] == "passed" and len(old_equal) == len(old_names)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
