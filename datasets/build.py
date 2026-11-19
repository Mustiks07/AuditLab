"""Materialize the versioned train and dev snapshots in a fresh directory."""

import argparse
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from auditlab.generator import GENERATOR_VERSION, SCHEMA_VERSION, generate_episode


RECIPE_PATH = Path(__file__).with_name("recipe.json")


def load_recipe():
    """Read the checked-in recipe for use by trusted tooling."""
    recipe = json.loads(RECIPE_PATH.read_text(encoding="utf-8"))
    if recipe["dataset_version"] != "1.1":
        raise ValueError("unsupported_dataset_version")
    if recipe["generator_version"] != GENERATOR_VERSION or recipe["schema_version"] != SCHEMA_VERSION:
        raise ValueError("incompatible_dataset_recipe")
    return recipe


def build_datasets(output_dir: Path) -> None:
    """Publish train and dev, including their trusted labels, at a fresh path."""
    output = Path(output_dir).absolute()
    if os.path.lexists(output):
        raise FileExistsError(output)
    recipe = load_recipe()
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".auditlab-datasets-", dir=output.parent) as staging:
        staged = Path(staging) / "datasets"
        snapshot = Path(staging) / "episode"
        for split, entries in recipe["splits"].items():
            destination = staged / split
            destination.mkdir(parents=True)
            for entry in entries:
                generate_episode(int(entry["seed_hex"], 16), entry["config"], snapshot)
                manifest = json.loads((snapshot / "public" / "manifest.json").read_text(encoding="utf-8"))
                if any(manifest[key] != value for key, value in recipe["public_dimensions"].items()):
                    raise RuntimeError("unexpected_dataset_snapshot_dimensions")
                snapshot.rename(destination / manifest["episode_id"])
        staged.rename(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path, help="new directory for train/ and dev/")
    args = parser.parse_args()
    build_datasets(args.output_dir)
    print(f"Published train and dev snapshots to {args.output_dir}")


if __name__ == "__main__":
    main()
