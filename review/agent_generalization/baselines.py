"""Local baseline policies on the public dev families (real local runs, not LLM calls, live=false).

Policies run through AuditLab's internal runner (train mode, its documented snapshot_dir path);
each attempt's final submission is saved and later re-scored by compare.py, never trusted as is.
"""
from __future__ import annotations

import copy, json, time
from pathlib import Path


class Empty:
    def act(self, obs):
        return {"action": "submit", "args": {"episode_id": obs["manifest"]["episode_id"], "findings": []}}


def run(make_env, make_policy, built: dict, out: Path) -> Path:
    exp = {"protocol_id": "agent-generalization-1.1", "episodes": {}, "comparisons": [
        {"name": "reconcile_vs_empty", "a": "reconcile", "b": "empty"},
        {"name": "reconcile_vs_flag_all", "a": "reconcile", "b": "flag_all"},
        {"name": "random_vs_empty", "a": "random", "b": "empty"}]}
    (out / "attempts").mkdir(parents=True, exist_ok=True)
    arms = [("empty", None, [0]), ("flag_all", "flag_all", [0]), ("reconcile", "reconcile", [0]), ("random", "random", [1, 2, 3])]
    for fam in built["families"]:
        if "variants" not in fam:
            continue
        for v in ("error", "clean"):
            key = f"{fam['family_id']}:{v}"
            exp["episodes"][key] = {"family_id": fam["family_id"], "variant": v, "episode_dir": fam["variants"][v]["dir"]}
            for arm, name, seeds in arms:
                for rep, seed in enumerate(seeds):
                    run_dir = out / "runs" / f"{arm}-{key.replace(':', '-')}-{rep}"
                    env = make_env("train", evaluator="inprocess")
                    t0 = time.monotonic()
                    obs, info = env.reset(seed, {"snapshot_dir": fam["variants"][v]["dir"], "export_dir": str(run_dir), "policy_name": arm})
                    pol = Empty() if name is None else make_policy(name, seed)
                    steps, submitted, answer, trunc = 0, False, None, False
                    while True:
                        action = pol.act(copy.deepcopy(obs))
                        if action.get("action") == "submit":
                            answer = action.get("args")
                        obs, r, term, trunc, info = env.step(action)
                        if term or trunc:
                            submitted = term and not trunc
                            break
                        steps += 1
                    status = "completed" if submitted else ("timeout" if trunc else "invalid_response")
                    attempt = {"attempt_id": f"{arm}-{key}-{rep}", "arm": arm, "episode_key": key, "repetition": rep, "status": status,
                               "final_answer": answer if submitted else None, "inspection_steps": steps, "actions": steps + int(submitted),
                               "input_tokens": None, "output_tokens": None, "wall_time_s": round(time.monotonic() - t0, 4), "cost_usd": 0.0,
                               "live": False, "policy": f"local:{arm}", "seed": seed}
                    (out / "attempts" / f"{attempt['attempt_id'].replace(':', '-')}.json").write_text(json.dumps(attempt), encoding="utf-8")
    (out / "experiment.json").write_text(json.dumps(exp, indent=1), encoding="utf-8")
    return out
