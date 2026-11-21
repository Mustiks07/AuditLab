"""Prepare offline Claude 1 SFT input from a selected, checked training curriculum."""
from __future__ import annotations

import argparse
from hashlib import sha256
import os
from pathlib import Path
import subprocess
import sys

from auditlab.release.transforms import read_json
from .agent_bridge import CLAUDE_RUNNER_SHA
from .export import snapshot_sha256


_PREPARE = """
from hashlib import sha256
import json
from pathlib import Path
import sys
from auditlab.research.training.dataset import build_sft, load_curriculum
from auditlab.research.training.demonstrations import run_demonstration

manifest, output = map(Path, sys.argv[1:])
content = manifest.read_bytes()
source = json.loads(content)
# The judge's evaluator subprocess starts from the runner checkout.
for episode in source["episodes"]:
    episode["episode_ref"] = (manifest.parent / episode["episode_ref"]).resolve().as_posix()
bound = output / "training-curriculum.json"
bound.write_text(json.dumps({"schema_version": source["schema_version"], "kind": source["kind"],
                             "artifact_id": source["artifact_id"], "episodes": source["episodes"],
                             "parent_curriculum_sha256": sha256(content).hexdigest()}, sort_keys=True) + "\\n",
                 encoding="utf-8")
registry = load_curriculum(bound)
for index, (episode_ref, metadata) in enumerate(registry["episodes"].items(), start=1):
    run_demonstration({"episode_ref": episode_ref, **metadata}, output / "demos", f"teacher-{index:04d}")
report = build_sft([output / "demos"], output / "sft", curriculum_ref=bound,
                   allowed_sources=("oracle_demonstration",), allowed_splits=("train",))
if report["counts"]["examples"] != len(registry["episodes"]):
    raise SystemExit("Some verified training snapshots did not produce accepted SFT examples")
print(json.dumps(report["counts"]))
"""


def _check_teacher(snapshot, curriculum, selected):
    teacher = selected["teacher"]
    path = curriculum.parent / teacher["path"]
    if sha256(path.read_bytes()).hexdigest() != teacher["sha256"]:
        raise ValueError("Teacher submission changed after export")
    payload = read_json(path)
    submission = payload.get("submission", payload)
    truth = read_json(snapshot / "private/truth.json")

    def signature(item, amount):
        return (item["type"], tuple(sorted(item["affected_ids"])), item[amount], item["currency"])

    findings = {signature(item, "claimed_amount_minor"): set(item["evidence_ids"]) for item in submission["findings"]}
    issues = {signature(item, "expected_amount_minor"): item["evidence_sets"] for item in truth["issues"]}
    if (submission["episode_id"] != truth["episode_id"] or findings.keys() != issues.keys()
            or len(findings) != len(submission["findings"]) or len(issues) != len(truth["issues"])
            or not all(any(set(group) <= findings[key] for group in groups) for key, groups in issues.items())):
        raise ValueError("Training truth differs from the validated teacher submission")


def prepare_teacher(checkout: Path, curriculum: Path, output: Path) -> None:
    checkout, curriculum, output = checkout.resolve(), curriculum.resolve(), output.resolve()
    git = ["git", "-C", str(checkout)]
    observed = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
    changes = subprocess.check_output([*git, "status", "--porcelain", "--", "auditlab/research"], text=True)
    if observed != CLAUDE_RUNNER_SHA or changes:
        raise ValueError("Teacher preparation requires the pinned Claude 1 checkout")
    manifest = read_json(curriculum)
    examples = {row["candidate_id"]: row for row in manifest["examples"]}
    entries = manifest["episodes"]
    if not entries:
        raise ValueError("No selected training episodes")
    for entry in entries:
        selected = examples[entry["candidate_id"]]
        snapshot = curriculum.parent / entry["episode_ref"]
        if (entry["split"] != "train" or selected["split"] != "train" or selected["status"] != "included"
                or selected["validation"]["status"] != "passed" or selected["teacher"] is None
                or entry["snapshot_sha256"] != selected["snapshot_sha256"]
                or snapshot_sha256(snapshot / "public") != entry["snapshot_sha256"]):
            raise ValueError("Teacher preparation requires a checked, unchanged train snapshot")
        _check_teacher(snapshot, curriculum, selected)
    output.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(checkout), env.get("PYTHONPATH"))))
    subprocess.run([sys.executable, "-c", _PREPARE, str(curriculum), str(output)],
                   cwd=curriculum.parent, env=env, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", type=Path, required=True)
    parser.add_argument("--curriculum", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare_teacher(args.runner, args.curriculum, args.output)


if __name__ == "__main__":
    main()
