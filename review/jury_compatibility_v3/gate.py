"""Strict CI gate over the manual Genesis acceptance and the external ID check.

    python review/genesis_acceptance/run.py --output artifacts/genesis-acceptance
    python -m review.jury_compatibility_v3.check_ids --generate --output artifacts/jury-v3/ids
    python -m review.jury_compatibility_v3.gate --genesis artifacts/genesis-acceptance/report.json \
        --ids artifacts/jury-v3/ids/id-transform.json --expected-commit "$(git rev-parse HEAD)" --expected-episodes 24

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
from collections import defaultdict
from pathlib import Path

GENESIS_BLOCK_COUNTS = {"contract": 20, "data": 21, "pairs": 20, "reward": 20, "m-examples": 15, "extra-negative": 13}
GATED_ID_PROFILES = ("reorder", "discover-grammar", "discover-grammar+reorder", "discover-neutral+reorder",
                     "discover-numeric+reorder")
INFORMATIVE_ID_PROFILES = ("literal+reorder", "literal-opaque-truth")


def _groups(rows, field):
    grouped = defaultdict(list)
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get(field), str):
                grouped[row[field]].append(row)
    return grouped


def _passing_ids(rows, key, expected):
    identifiers = [row.get(key) for row in rows if row.get("status") == "passed"]
    if (len(rows) != expected or len(identifiers) != expected
            or any(not isinstance(value, str) or not value for value in identifiers)
            or len(set(identifiers)) != expected):
        return None
    return set(identifiers)


def check(genesis: dict, ids: dict | None, expected: str | None, expected_episodes: int | None = None) -> tuple[int, dict]:
    result = {"genesis": {}, "ids": {}, "problems": []}
    meta = genesis.get("meta") if isinstance(genesis, dict) else None
    if not isinstance(meta, dict) or meta.get("suite") != "genesis-local-acceptance-1.0":
        result["problems"].append("genesis report has missing or unsupported metadata")
        return 2, result
    if expected and meta.get("commit") != expected:
        result["problems"].append("genesis report is from another commit")
        return 2, result
    if meta.get("product_code_dirty") is not False:
        result["problems"].append("genesis report lacks evidence of a clean product tree")
        return 2, result
    official = genesis.get("summary_official_layer")
    if not isinstance(official, dict) or not official:
        result["problems"].append("genesis report has no official layer")
        return 2, result
    result["genesis"] = official
    items = _groups(genesis.get("items"), "block")
    if set(official) != set(GENESIS_BLOCK_COUNTS) or set(items) != set(GENESIS_BLOCK_COUNTS):
        result["problems"].append("Genesis report does not cover exactly the six required blocks")
    for block, count in GENESIS_BLOCK_COUNTS.items():
        counts = official.get(block)
        if (counts != {"passed": count, "failed": 0, "not_checked": 0}
                or _passing_ids(items[block], "id", count) is None):
            result["problems"].append(f"genesis {block}: expected {count} distinct passes, got {counts}")
    if ids is None and expected_episodes is not None:
        result["problems"].append("ID report is required when the expected episode count is specified")
        return 2, result
    if ids is not None:
        if not isinstance(ids, dict) or ids.get("suite") != "jury-compatibility-v3/id-transform":
            result["problems"].append("ID report has missing or unsupported metadata")
            return 2, result
        if expected and ids.get("commit") != expected:
            result["problems"].append("id report is from another commit")
            return 2, result
        count = ids.get("episodes") if expected_episodes is None else expected_episodes
        if type(count) is not int or count <= 0 or ids.get("episodes") != count:
            result["problems"].append(f"ID report episode count: expected {count}, got {ids.get('episodes')}")
            return 1, result
        summary = ids.get("summary")
        if not isinstance(summary, dict):
            result["problems"].append("ID report lacks profile summaries")
            return 2, result
        cases = _groups(ids.get("rows"), "profile")
        reference = None
        for name in GATED_ID_PROFILES:
            row = summary.get(name)
            result["ids"][name] = row
            episodes = _passing_ids(cases[name], "episode", count)
            if (not isinstance(row, dict) or row.get("total") != count or row.get("passed") != count
                    or episodes is None or reference is not None and reference != episodes):
                result["problems"].append(f"id profile {name}: expected {count} distinct passes on the same episodes, got {row}")
            if reference is None:
                reference = episodes
        replay = ids.get("replay_identical") or {}
        if replay != {"passed": count, "total": count}:
            result["problems"].append(f"replay: {replay}")
        result["ids_informative"] = {name: summary.get(name) for name in INFORMATIVE_ID_PROFILES}
    return (1 if result["problems"] else 0), result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--genesis", type=Path, required=True)
    parser.add_argument("--ids", type=Path)
    parser.add_argument("--expected-commit")
    parser.add_argument("--expected-episodes", type=int)
    args = parser.parse_args(argv)
    try:
        genesis = json.loads(args.genesis.read_text(encoding="utf-8"))
        ids = json.loads(args.ids.read_text(encoding="utf-8")) if args.ids else None
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "not_checked", "error": f"{type(error).__name__}: {error}"}))
        return 2
    code, result = check(genesis, ids, args.expected_commit, args.expected_episodes)
    result["status"] = {0: "passed", 1: "failed", 2: "not_checked"}[code]
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return code


if __name__ == "__main__":
    sys.exit(main())
