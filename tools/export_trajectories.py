"""Publish/replay the open submission trajectories through the official adapter.

Export uses an isolated Docker policy, never a truth-reading oracle. Replay re-executes
the recorded public actions against the real environment and compares all seven fields.
The reset observation is step 0; reset is not one of the 100 step calls.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import judge_adapter
from auditlab.runner.isolation import DockerPolicy

FIELDS = {"episode_id", "step", "action", "observation", "reward", "terminated", "truncated"}
RESET = {"name": "reset", "args": {"mode": "train"}}
FORBIDDEN = {"truth", "provenance", "seed", "episode_key", "expected_amount_minor", "evidence_sets", "issue_id"}


def no_private_fields(value):
    if isinstance(value, dict):
        if FORBIDDEN.intersection(value):
            raise ValueError("private field in public observation")
        for child in value.values():
            no_private_fields(child)
    elif isinstance(value, list):
        for child in value:
            no_private_fields(child)


def episodes(dataset):
    result = {}
    for split in ("train", "dev"):
        snapshots = sorted((Path(dataset) / split).glob("*/public/manifest.json"))
        if len(snapshots) != 10:
            raise ValueError(f"expected 10 {split} episodes, got {len(snapshots)}")
        for manifest in snapshots:
            eid = json.loads(manifest.read_text(encoding="utf-8"))["episode_id"]
            if eid in result:
                raise ValueError("duplicate episode_id across train/dev")
            result[eid] = (split, manifest.parent.parent)
    return result


def row(eid, step, action, observation, reward=0.0, terminated=False, truncated=False):
    no_private_fields(observation)
    return {"episode_id": eid, "step": step, "action": action, "observation": observation,
            "reward": reward, "terminated": terminated, "truncated": truncated}


def policy_observation(observation):
    # Only a wire-format translation of the public observation, no trusted info.
    if "manifest" in observation:
        return observation
    return {"ok": observation["error"] is None, "remaining_steps": observation["remaining_steps"],
            **({"result": observation["result"]} if observation["error"] is None
               else {"error": observation["error"]})}


def official_action(action):
    name = action["action"]
    return {"name": name, "args": {"findings": action["args"]} if name == "submit" else action["args"]}


def encode(record):
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"


def export(dataset, output, image):
    output = Path(output)
    if output.exists():
        raise FileExistsError("output must be a new directory")
    catalog = episodes(dataset)
    output.parent.mkdir(parents=True, exist_ok=True)
    receipt = {"format_version": "1.0", "dataset": "open-1.3", "mode": "train",
               "policy": "reconcile", "policy_seed": 0, "transport": "DockerPolicy",
               "step_zero": "reset observation; excluded from the action budget",
               "exporter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "versions": judge_adapter.make_env().versions(), "episodes": []}
    with tempfile.TemporaryDirectory(prefix=".trajectories-", dir=output.parent) as tmp:
        staged = Path(tmp) / "published"
        staged.mkdir()
        with (staged / "trajectories.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
            for eid, (split, snapshot) in catalog.items():
                env = judge_adapter.make_env()
                policy = None
                try:
                    # Container preparation precedes reset; each episode keeps its real 60 s deadline.
                    policy = DockerPolicy(image, "reconcile", 0, memory="1536m")
                    observation, _ = env.reset(0, {"mode": "train", "episode_dir": str(snapshot)})
                    stream.write(encode(row(eid, 0, RESET, observation)))
                    for step in range(1, 101):
                        if env.seconds_left() <= 0:
                            raise RuntimeError("episode timed out; no complete publication")
                        policy.set_timeout(env.seconds_left())
                        action = official_action(policy.act(policy_observation(observation)))
                        observation, reward, terminated, truncated, _ = env.step(action)
                        stream.write(encode(row(eid, step, action, observation, reward, terminated, truncated)))
                        if terminated or truncated:
                            break
                    else:
                        raise RuntimeError("episode did not terminate")
                    final = env.trusted_result()
                    if not terminated or truncated or final["status"] != "submitted":
                        raise RuntimeError(f"episode did not complete: {eid}")
                    public = hashlib.sha256()
                    for file in sorted((snapshot / "public").iterdir()):
                        public.update(file.name.encode() + b"\0" + file.read_bytes() + b"\0")
                    receipt["episodes"].append({"episode_id": eid, "split": split, "actions": step,
                                                "public_sha256": public.hexdigest(), "score": final["score"],
                                                "policy_image_id": policy.measurements["image_id"]})
                finally:
                    try:
                        if policy is not None:
                            policy.close()
                    finally:
                        env.close()
        path = staged / "trajectories.jsonl"
        receipt["trajectories_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        (staged / "trajectories.manifest.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        staged.rename(output)
    return receipt


def replay(dataset, trajectory):
    catalog = episodes(dataset)
    seen, current, env, last = set(), None, None, None
    lines = 0
    try:
        with Path(trajectory).open(encoding="utf-8") as stream:
            for line in stream:
                record = json.loads(line)
                if not isinstance(record, dict) or set(record) != FIELDS:
                    raise ValueError("trajectory row must contain exactly seven fields")
                if type(record["step"]) is not int or type(record["reward"]) not in (int, float) \
                        or type(record["terminated"]) is not bool or type(record["truncated"]) is not bool:
                    raise ValueError("invalid trajectory field types")
                no_private_fields(record["observation"])
                eid = record["episode_id"]
                if eid != current:
                    if eid not in catalog or eid in seen or (last and not (last["terminated"] or last["truncated"])):
                        raise ValueError("unknown, repeated or unfinished episode")
                    if env:
                        env.close()
                    seen.add(eid)
                    current = eid
                    env = judge_adapter.make_env()
                    observation, _ = env.reset(0, {"mode": "train", "episode_dir": str(catalog[eid][1])})
                    expected = row(eid, 0, RESET, observation)
                else:
                    if last["terminated"] or last["truncated"]:
                        raise ValueError("action after terminal row")
                    observation, reward, terminated, truncated, _ = env.step(record["action"])
                    expected = row(eid, last["step"] + 1, record["action"], observation, reward, terminated, truncated)
                if record != expected:
                    raise ValueError(f"replay mismatch at episode {eid}, step {record['step']}")
                lines += 1
                last = record
        if seen != set(catalog) or last is None or not (last["terminated"] or last["truncated"]):
            raise ValueError("trajectory must finish all 10 train and 10 dev episodes")
    finally:
        if env:
            env.close()
    return {"status": "passed", "episodes": len(seen), "train": 10, "dev": 10, "rows": lines}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("export", "check"):
        item = sub.add_parser(name)
        item.add_argument("--dataset", type=Path, default=ROOT / "datasets/open-1.3")
        if name == "export":
            item.add_argument("--output", type=Path, required=True)
            item.add_argument("--policy-image", default=os.environ.get("AUDITLAB_POLICY_IMAGE"), required=False)
        else:
            item.add_argument("--file", type=Path, default=ROOT / "trajectories.jsonl")
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            if not args.policy_image:
                parser.error("export requires --policy-image or AUDITLAB_POLICY_IMAGE")
            result = export(args.dataset, args.output, args.policy_image)
            print(json.dumps({"status": "published", "episodes": len(result["episodes"]),
                              "trajectories_sha256": result["trajectories_sha256"]}))
        else:
            print(json.dumps(replay(args.dataset, args.file)))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Trajectory check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
