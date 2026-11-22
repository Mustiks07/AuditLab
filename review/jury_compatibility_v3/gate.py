"""Strict CI gate over the manual Genesis acceptance and the external ID check.

    python review/genesis_acceptance/run.py --output artifacts/genesis-acceptance
    python -m review.jury_compatibility_v3.check_ids --generate --output artifacts/jury-v3/ids
    python -m review.jury_compatibility_v3.gate --genesis artifacts/genesis-acceptance/report.json \
        --ids artifacts/jury-v3/ids/id-transform.json --expected-commit "$(git rev-parse HEAD)"

Exit 0 only if every official-layer item of the Genesis harness passed (no failed, no not_checked),
the reports belong to the expected commit, and the ID profiles that the published rules require to be
compatible (reorder and value-based renaming) passed on every episode. The two profiles that model
undefined jury behaviour (field-list renaming, opaque truth references) are reported, never gated.
Exit 1 — a requirement failed; 2 — a report is missing, unreadable or from another commit.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

GATED_ID_PROFILES = ("reorder", "discover-grammar", "discover-grammar+reorder", "discover-neutral+reorder",
                     "discover-numeric+reorder")
INFORMATIVE_ID_PROFILES = ("literal+reorder", "literal-opaque-truth")


def check(genesis: dict, ids: dict | None, expected: str | None) -> tuple[int, dict]:
    result = {"genesis": {}, "ids": {}, "problems": []}
    if expected and genesis.get("meta", {}).get("commit") != expected:
        result["problems"].append("genesis report is from another commit")
        return 2, result
    if genesis.get("meta", {}).get("product_code_dirty"):
        result["problems"].append("genesis report was produced from a dirty product tree")
        return 2, result
    official = genesis.get("summary_official_layer") or {}
    if not official:
        result["problems"].append("genesis report has no official layer")
        return 2, result
    for block, counts in official.items():
        result["genesis"][block] = counts
        if counts.get("failed") or counts.get("not_checked") or not counts.get("passed"):
            result["problems"].append(f"genesis {block}: {counts}")
    if ids is not None:
        if expected and ids.get("commit") != expected:
            result["problems"].append("id report is from another commit")
            return 2, result
        summary = ids.get("summary") or {}
        for name in GATED_ID_PROFILES:
            row = summary.get(name)
            result["ids"][name] = row
            if not row or row.get("total", 0) == 0 or row.get("passed") != row.get("total"):
                result["problems"].append(f"id profile {name}: {row}")
        replay = ids.get("replay_identical") or {}
        if not replay.get("total") or replay.get("passed") != replay.get("total"):
            result["problems"].append(f"replay: {replay}")
        result["ids_informative"] = {name: summary.get(name) for name in INFORMATIVE_ID_PROFILES}
    return (1 if result["problems"] else 0), result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--genesis", type=Path, required=True)
    parser.add_argument("--ids", type=Path)
    parser.add_argument("--expected-commit")
    args = parser.parse_args(argv)
    try:
        genesis = json.loads(args.genesis.read_text(encoding="utf-8"))
        ids = json.loads(args.ids.read_text(encoding="utf-8")) if args.ids else None
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "not_checked", "error": f"{type(error).__name__}: {error}"}))
        return 2
    code, result = check(genesis, ids, args.expected_commit)
    result["status"] = {0: "passed", 1: "failed", 2: "not_checked"}[code]
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return code


if __name__ == "__main__":
    sys.exit(main())
