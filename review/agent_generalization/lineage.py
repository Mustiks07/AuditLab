"""Partition by economic origin (protocol independent-assurance-2.0, section 4).

The root of a snapshot is its economic origin: the canonical (ID- and order-free) history of its
clean source book, or of its own public files when it is clean and has no source book. Every
descendant (renamed, reduced, fixed or otherwise transformed variant) inherits the partition of its
root, through an explicit `parent` link or, without one, through an equal canonical history.

`check(items)` rejects a set in which a descendant sits in another partition than its root, or in
which one canonical history (or one family's defect-bearing history) appears in two partitions.
"""
from __future__ import annotations

from pathlib import Path

from review.agent_generalization.canonical import whole_history


def origin(episode: Path) -> str:
    sb = episode / "private" / "source_book"
    return whole_history(sb if sb.is_dir() else episode / "public")


def check(items: list[dict]) -> dict:
    """items: {"id", "split", "episode": Path, "parent": id or None}. Explicit parents win over histories."""
    by_id = {i["id"]: i for i in items}
    hist = {i["id"]: whole_history(i["episode"] / "public") for i in items}
    root_hist = {i["id"]: origin(i["episode"]) for i in items}

    def root(i):
        seen = set()
        while i.get("parent") and i["parent"] in by_id and i["id"] not in seen:
            seen.add(i["id"]); i = by_id[i["parent"]]
        return i
    violations = []
    for i in items:
        r = root(i)
        if r["split"] != i["split"]:
            violations.append({"id": i["id"], "split": i["split"], "root": r["id"], "root_split": r["split"], "via": "parent"})
    # without explicit links: one public history or one origin in two partitions is the same family
    for key, label in ((hist, "public_history"), (root_hist, "origin")):
        splits = {}
        for iid, h in key.items():
            splits.setdefault(h, set()).add(by_id[iid]["split"])
        for h, ss in splits.items():
            if len(ss) > 1:
                ids = sorted(iid for iid, x in key.items() if x == h)
                violations.append({"ids": ids, "splits": sorted(ss), "via": label})
    return {"status": "passed" if not violations else "failed", "items": len(items), "violations": violations}


def assign(items: list[dict], root_split: dict[str, str]) -> dict[str, str]:
    """Split for every item = split of its origin (root_split maps origin history -> split); unknown origins stay None."""
    return {i["id"]: root_split.get(origin(i["episode"])) for i in items}
