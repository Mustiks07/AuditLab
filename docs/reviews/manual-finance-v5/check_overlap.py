"""Public-only overlap of v5 companies with train/dev/internal: datasets.audit_subhistories.check_subhistories.

usage: python check_overlap.py REPO WORKDIR OUT_JSON
"""
import json, shutil, sys
from pathlib import Path

repo, work, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), Path(sys.argv[3])
sys.path.insert(0, str(repo))
from auditlab.generator import generate_episode  # noqa: E402
from auditlab.release.rehearsal import selection_manifest  # noqa: E402
from datasets.audit_subhistories import check_subhistories  # noqa: E402

internal = work / "internal"
if not internal.exists():
    for row in selection_manifest()["episodes"]:
        tmp = work / "tmp-internal" / row["id"]
        generate_episode(row["seed"], row["config"], tmp)
        (internal / row["id"]).mkdir(parents=True)
        shutil.move(str(tmp / "public"), str(internal / row["id"] / "public"))   # private discarded unread
        shutil.rmtree(tmp)
splits = {
    "train": {p.parent.name: p for p in sorted((repo / "datasets" / "open" / "train").glob("*/public"))},
    "dev": {p.parent.name: p for p in sorted((repo / "datasets" / "open" / "dev").glob("*/public"))},
    "internal": {p.parent.name: p for p in sorted(internal.glob("*/public"))},
    "v5": {p.parent.name: p for p in sorted((work / "public").glob("*/public"))},
}
result = check_subhistories(splits)
out.write_text(json.dumps(result, indent=1, default=str) + "\n", encoding="utf-8", newline="\n")
print("counts", result["counts"])
for view, v in result["views"].items():
    pairs = {k: len(x) for k, x in v["episode_pairs"].items() if "v5" in k}
    print(view, "shared families", v["shared_families"], "| pairs involving v5:", pairs)
    for m in v["matches"]:
        if any(x["split"] == "v5" for x in m["members"]):
            print("   v5 match:", m["sha256"][:16], [(x["split"], x["episode"]) for x in m["members"]], json.dumps(m["canonical"])[:200])
