"""Open diverse set 1.3: build, lock and check the train/dev snapshots of profile 1.3.

    python -m datasets.build_13 build OUT_DIR       # materialize train/ and dev/ at a new path
    python -m datasets.build_13 check DIR           # rebuild into a temp dir and compare bytes with DIR

This set is separate from datasets/open (dataset 1.1, profile 1.0), whose recipe, builder and
files are unchanged. Seeds and configs are published: the set is open, so no secrecy is claimed
for it. The recipe pins every snapshot by episode_id and a tree SHA-256 of public/ and private/.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from auditlab.generator import GENERATOR_VERSIONS, SCHEMA_VERSION, generate_episode

RECIPE_PATH = Path(__file__).with_name("recipe-1.3.json")
DATASET_VERSION = "open-1.3"


def tree_sha256(path: Path) -> str:
    digest = sha256()
    for file in sorted(p for p in Path(path).rglob("*") if p.is_file()):
        digest.update(file.relative_to(path).as_posix().encode("utf-8") + b"\0" + file.read_bytes() + b"\0")
    return digest.hexdigest()


def load_recipe(path: Path = RECIPE_PATH) -> dict:
    recipe = json.loads(path.read_text(encoding="utf-8"))
    if recipe["dataset_version"] != DATASET_VERSION or recipe["config_version"] != "1.3":
        raise ValueError("unsupported_dataset_version")
    if recipe["generator_version"] != GENERATOR_VERSIONS["1.3"] or recipe["schema_version"] != SCHEMA_VERSION:
        raise ValueError("incompatible_dataset_recipe")
    return recipe


def _config(entry: dict, recipe: dict) -> dict:
    return {"config_version": recipe["config_version"], **entry["config"]}


def build(output_dir: Path, recipe: dict | None = None, *, verify_lock: bool = True) -> list[dict]:
    """Publish train/ and dev/ at a fresh path; with verify_lock, refuse any byte drift."""
    recipe = recipe or load_recipe()
    output = Path(output_dir).absolute()
    if os.path.lexists(output):
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    built = []
    with TemporaryDirectory(prefix=".auditlab-open13-", dir=output.parent) as staging:
        staged = Path(staging) / "datasets"
        for split, entries in recipe["splits"].items():
            (staged / split).mkdir(parents=True)
            for entry in entries:
                snapshot = Path(staging) / "episode"
                generate_episode(int(entry["seed_hex"], 16), _config(entry, recipe), snapshot)
                episode_id = json.loads((snapshot / "public" / "manifest.json").read_text(encoding="utf-8"))["episode_id"]
                digest = tree_sha256(snapshot)
                lock = entry.get("lock")
                if verify_lock and lock != {"episode_id": episode_id, "tree_sha256": digest}:
                    raise RuntimeError(f"recipe_lock_mismatch: {split}/{entry['family']}")
                snapshot.rename(staged / split / episode_id)
                built.append({"split": split, "family": entry["family"], "episode_id": episode_id, "tree_sha256": digest})
        staged.rename(output)
    return built


def check(directory: Path, recipe: dict | None = None) -> dict:
    """Rebuild in a temporary directory and compare every file with an existing copy."""
    recipe = recipe or load_recipe()
    with TemporaryDirectory() as tmp:
        built = build(Path(tmp) / "rebuilt", recipe)
        differing = []
        for item in built:
            existing = Path(directory) / item["split"] / item["episode_id"]
            if not existing.is_dir() or tree_sha256(existing) != item["tree_sha256"]:
                differing.append(f"{item['split']}/{item['episode_id']}")
        extra = sorted(f"{split.name}/{episode.name}" for split in Path(directory).iterdir() if split.is_dir()
                       for episode in split.iterdir()
                       if episode.name not in {i["episode_id"] for i in built if i["split"] == split.name})
    return {"status": "passed" if not differing and not extra else "failed", "snapshots": len(built),
            "differing": differing, "unexpected": extra}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build").add_argument("output_dir", type=Path)
    sub.add_parser("check").add_argument("directory", type=Path)
    lock = sub.add_parser("lock", help="maintainer only: print the lock of a new recipe draft")
    lock.add_argument("recipe", type=Path)
    args = parser.parse_args(argv)
    if args.command == "build":
        build(args.output_dir)
        print(f"Published open-1.3 train and dev snapshots to {args.output_dir}")
        return 0
    if args.command == "check":
        result = check(args.directory)
        print(json.dumps(result, indent=1))
        return 0 if result["status"] == "passed" else 1
    draft = json.loads(args.recipe.read_text(encoding="utf-8"))
    with TemporaryDirectory() as tmp:
        built = build(Path(tmp) / "draft", draft, verify_lock=False)
    for split, entries in draft["splits"].items():
        for entry in entries:
            item = next(i for i in built if i["split"] == split and i["family"] == entry["family"])
            entry["lock"] = {"episode_id": item["episode_id"], "tree_sha256": item["tree_sha256"]}
    sys.stdout.write(json.dumps(draft, indent=1, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
