"""Frozen dev replication set for the live Gemini miss (Issue #42, agent-improvement v2). Trusted side.

    python -m auditlab.research.agents.replication build --out DIR      # needs the pinned tzdata
    python -m auditlab.research.agents.replication check --out DIR      # re-hash, no regeneration

The pilot (docs/research/gemini) saw ONE completed attempt submit no findings on a partial allocation
error of 3 024 000 minor KZT (dev family 922101). Before calling that a weakness we repeat it on four
frozen conditions of the same economic family:

    original_error       the generator snapshot the pilot used (seed 922101, partial allocation)
    corrected_clean      the same history without the defect (Claude Pro 2's matched clean variant:
                         the snapshot's source book when size-matched), empty truth
    transformed_error    original_error with documents renamed and rows reordered (auditlab.release.transforms)
    transformed_clean    corrected_clean with the same transformation

Each condition is validated by Claude Pro 2's independent validator (own answer from public files
before truth). The set manifest freezes configs, seeds and sha256 of every file; a changed byte
changes the manifest hash. Truth stays in each condition's private/ and never reaches an agent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

SET_VERSION = "replication-922101-1.0"
FAMILY = "dev-922101"
SEED = 922101
CONFIG = {"config_version": "1.1", "base_currency": "KZT", "currencies": ["KZT"], "allocation_mode": "partial",
          "issue_counts": {"payment_allocation": 1}}
TRANSFORM_SEED = 4207
CONDITIONS = ("original_error", "corrected_clean", "transformed_error", "transformed_clean")
ROOT = Path(__file__).resolve().parents[3]


def tree_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for file in sorted(p for p in Path(path).rglob("*") if p.is_file()):
        digest.update(file.relative_to(path).as_posix().encode() + b"\0" + file.read_bytes() + b"\0")
    return digest.hexdigest()


def _write_json(path: Path, value) -> None:
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _transform(source: Path, target: Path) -> dict:
    from auditlab.release.transforms import transform_public, transform_truth
    mapping = transform_public(source / "public", target / "public", TRANSFORM_SEED)
    (target / "private").mkdir(parents=True)
    truth = json.loads((source / "private" / "truth.json").read_text(encoding="utf-8"))
    _write_json(target / "private" / "truth.json", transform_truth(truth, mapping))
    return {"version": mapping["version"], "seed": TRANSFORM_SEED, "renamed_ids": sum(len(v) for v in mapping["raw_ids"].values()),
            "new_episode_id": mapping["new_episode_id"]}


def build(out: str | Path) -> dict:
    """Generate the four conditions into out/<condition>/ and write out/replication-set.json."""
    import judge_adapter
    sys.path.insert(0, str(ROOT))
    from review.agent_generalization.families import matched_clean
    from review.agent_generalization.validator import validate_candidate

    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} is not empty; a frozen set is built once into a new directory")
    out.mkdir(parents=True, exist_ok=True)
    error = out / "original_error"
    judge_adapter.generate_episode(SEED, {"team_config": CONFIG}, str(error))
    clean = out / "corrected_clean"
    clean_kind = matched_clean(judge_adapter, error, SEED, CONFIG, clean)
    if clean_kind.get("kind") is None:
        raise RuntimeError(f"no matched clean variant: {clean_kind}")
    transforms = {"transformed_error": _transform(error, out / "transformed_error"),
                  "transformed_clean": _transform(clean, out / "transformed_clean")}
    conditions = {}
    for name in CONDITIONS:
        episode = out / name
        truth = json.loads((episode / "private" / "truth.json").read_text(encoding="utf-8"))
        validation = validate_candidate(episode, adapter=judge_adapter, work=out / ".validation" / name)
        conditions[name] = {
            "episode_ref": name, "family_id": FAMILY, "split": "dev",
            "variant": "clean" if not truth["issues"] else "error",
            "public_sha256": tree_sha256(episode / "public"), "episode_sha256": tree_sha256(episode),
            "truth_issues": [{"type": i["type"], "expected_amount_minor": i["expected_amount_minor"],
                              "currency": i["currency"]} for i in truth["issues"]],
            "validation": {"status": validation.get("status"),
                           "checks": {k: v.get("status") for k, v in sorted((validation.get("checks") or {}).items())
                                      if isinstance(v, dict)},
                           "notes": [str(n)[:300] for n in (validation.get("notes") or [])][:10]},
            "derived_from": {"transformed_error": "original_error", "transformed_clean": "corrected_clean"}.get(name),
            "transform": transforms.get(name)}
    shutil.rmtree(out / ".validation", ignore_errors=True)
    manifest = {"schema_version": "1.0", "artifact_id": SET_VERSION, "split": "dev", "family_id": FAMILY,
                "purpose": "replication of one observed live miss; dev only, never a holdout",
                "generator": {"seed": SEED, "config": CONFIG}, "clean_variant": clean_kind,
                "transform": {"module": "auditlab.release.transforms", "seed": TRANSFORM_SEED},
                "validator": "review.agent_generalization.validator.validate_candidate (Claude Pro 2)",
                "conditions": conditions}
    manifest["set_sha256"] = hashlib.sha256(json.dumps(conditions, sort_keys=True).encode()).hexdigest()
    _write_json(out / "replication-set.json", manifest)
    return manifest


def check(out: str | Path) -> dict:
    """Re-hash a built set against its manifest (detects any changed byte)."""
    out = Path(out)
    manifest = json.loads((out / "replication-set.json").read_text(encoding="utf-8"))
    problems = [name for name, row in manifest["conditions"].items()
                if tree_sha256(out / row["episode_ref"]) != row["episode_sha256"]]
    return {"artifact_id": manifest["artifact_id"], "set_sha256": manifest["set_sha256"], "changed": problems,
            "status": "passed" if not problems else "failed"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m auditlab.research.agents.replication")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "check"):
        sub.add_parser(name).add_argument("--out", required=True)
    args = parser.parse_args(argv)
    result = build(args.out) if args.command == "build" else check(args.out)
    print(json.dumps({k: result[k] for k in ("artifact_id", "set_sha256")} | (
        {"conditions": {n: {k: r[k] for k in ("variant", "validation")} for n, r in result["conditions"].items()}}
        if args.command == "build" else {"status": result["status"], "changed": result["changed"]}), indent=1))
    return 0 if args.command == "build" or result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
