"""Reproduce three research checks on the pinned AuditLab code, with synthetic inputs only."""

import argparse
import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path


EVIDENCE = Path(__file__).resolve().parent
ROOT = EVIDENCE.parents[3]
sys.path.insert(0, str(ROOT))

from auditlab.generator import generate_episode
from auditlab.policies import make_policy
from auditlab.runner import make_env
from auditlab.runner.episode import run_episode


FX_CONFIG = {"config_version": "1.1", "base_currency": "JPY", "currencies": ["JPY", "EUR"],
             "clean_distractors": [], "interaction": "allocation_fx_same_payment"}
FIXTURES = ROOT / "fixtures" / "foundation"


def prepare(destination):
    destination.mkdir(parents=True)
    generate_episode(0, FX_CONFIG, destination / "fx-clean")
    public = destination / "auditor"
    shutil.copytree(destination / "fx-clean" / "public", public / "FX")
    for name in ("S01", "S02"):
        shutil.copytree(FIXTURES / name / "public", public / name)
    print(f"Public-only directory ready: {public}")


def guarded_seal():
    expected = (EVIDENCE / "root-public.sha256").read_text(encoding="ascii").split()[0]
    actual = hashlib.sha256((EVIDENCE / "root-public.md").read_bytes()).hexdigest()
    assert expected == actual, "Manual public calculation does not match its seal"
    return actual


def fx_check(destination):
    truth = json.loads((destination / "fx-clean" / "private" / "truth.json").read_bytes())
    assert truth["issues"] == []
    faulty = destination / "fx-requested"
    try:
        generate_episode(0, {**FX_CONFIG, "issue_counts": {"fx_conversion": 1}}, faulty)
    except RuntimeError as error:
        assert str(error) == "unintended_issue_counts" and not faulty.exists()
        return {"seed": 0, "clean_truth_count": 0, "failure": str(error), "partial_snapshot": False}
    raise AssertionError("Baseline counterexample did not reproduce")


def run_policy(label, replace_evidence=False):
    base = make_policy("reconcile", 31)

    class Policy:
        def act(self, observation):
            action = base.act(observation)
            if replace_evidence and action["action"] == "submit":
                action = copy.deepcopy(action)
                finding = action["args"]["findings"][0]
                refs = finding["evidence_ids"]
                refs[refs.index("ledger/R5")] = "ledger/R6"
            return action

    env = make_env("train")
    try:
        _, reward, terminated, truncated, info = run_episode(
            env, Policy(), 0, {"snapshot_dir": str(FIXTURES / label), "policy_name": "reconcile"})
        assert terminated and not truncated and info["status"] == "submitted"
        score = info["score"]
        return {"reward": reward, "findings": env.final["submission"]["findings"],
                "tp_fp_fn": [score[key] for key in ("tp", "fp", "fn")],
                "inspections": env.final["inspection_steps"],
                "actions": [line["action"]["action"] for line in env._trajectory]}
    finally:
        env.close()


def timeline_check():
    before, after, wrong = run_policy("S01"), run_policy("S02"), run_policy("S02", replace_evidence=True)
    assert before["findings"] == [] and before["tp_fp_fn"] == [0, 0, 0]
    finding, = after["findings"]
    assert (finding["claimed_amount_minor"], finding["currency"], after["tp_fp_fn"]) == (2_000_000, "KZT", [1, 0, 0])
    assert after["reward"] == 0.99 and wrong["reward"] == -1
    assert wrong["tp_fp_fn"] == [0, 1, 1]
    return {"before": before, "after": after, "wrong_evidence": wrong}


def atomic_finish_check():
    # The reviewed #23 regression uses a valid JSON envelope with an invalid findings field.
    # Keep it on the local synthetic fixture and collect only trusted state diagnostics.
    results = {}
    for mode in ("train", "closed"):
        env = make_env(mode)
        try:
            observation, _ = env.reset(0, {"snapshot_dir": str(FIXTURES / "S02")})
            episode = observation["manifest"]["episode_id"]
            _, shallow_reward, done, _, info = env.step({"action": "submit", "args": {"episode_id": episode, "findings": {}}})
            assert done and info["status"] == "invalid_submission"
            assert shallow_reward == (-1 if mode == "train" else 0)

            env.reset(0, {"snapshot_dir": str(FIXTURES / "S02")})
            findings = 0
            for _ in range(600):
                findings = [findings]
            action = json.loads(json.dumps({"action": "submit", "args": {"episode_id": episode, "findings": findings}}))
            try:
                env.step(action)
            except RecursionError as error:
                assert env._phase == "done" and env.final is None and not env._trajectory
                results[mode] = {"shallow_reward": shallow_reward, "raised": type(error).__name__,
                                 "phase": env._phase, "final": env.final, "trajectory_lines": len(env._trajectory)}
            else:
                raise AssertionError("Known baseline #23 did not reproduce")
        finally:
            env.close()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "check"))
    parser.add_argument("directory", type=Path)
    options = parser.parse_args()
    if options.phase == "prepare":
        prepare(options.directory.resolve())
    else:
        seal = guarded_seal()
        print(json.dumps({"python": sys.version.split()[0], "public_seal_sha256": seal,
                          "runner_issue_23": atomic_finish_check(), "fx_quantization": fx_check(options.directory),
                          "settlement_and_evidence": timeline_check()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
