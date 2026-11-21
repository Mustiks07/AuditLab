"""Closed-mode leakage and the 60-second boundary, through AuditLab's internal runner (local analogs).

These do not replace C18-C20 through the jury adapter; they are labelled diagnostics.
"""
from __future__ import annotations

import copy, json, time
from pathlib import Path


def closed_mode_leakage(make_env, make_policy, episode: Path, seed_marker: int) -> dict:
    truth = json.loads((episode / "private" / "truth.json").read_text(encoding="utf-8"))
    secrets = {i["issue_id"] for i in truth["issues"]} | {str(episode / "private"), str(seed_marker), "expected_amount_minor", "evidence_sets", "provenance"}
    env = make_env("closed", evaluator="inprocess")
    obs, info = env.reset(seed_marker, {"snapshot_dir": str(episode), "policy_name": "reconcile"})
    channel = [obs, info]; rewards = []
    pol = make_policy("reconcile", 3)
    # also exercise error paths: unknown id, unknown action, path-like ids, a broken submit body
    probes = [{"action": "get_record", "args": {"table": "ledger", "id": "NO_SUCH_ROW"}},
              {"action": "get_document", "args": {"doc_id": "../private/truth.json"}},
              {"action": "no_such_action", "args": {}},
              {"action": "calculator", "args": {"expression": "__import__('os')"}}]
    for p in probes:
        o, r, term, trunc, inf = env.step(p); channel += [o, inf]; rewards.append(r)
    while True:
        a = pol.act(copy.deepcopy(obs))
        obs, r, term, trunc, inf = env.step(a); channel += [obs, inf]; rewards.append(r)
        if term or trunc:
            break
    text = json.dumps(channel)
    leaks = sorted(s for s in secrets if s and s in text)
    terminal_zero = all(type(x) in (int, float) and x == 0 for x in rewards)
    trusted = getattr(env, "final", None)
    return {"status": "passed" if not leaks and terminal_zero else "failed", "leaks": leaks, "rewards_all_zero": terminal_zero,
            "steps": len(rewards), "trusted_result_kept": trusted is not None,
            "scope": "internal AuditEnvironment closed mode; not the jury adapter (C19/C20 stay not_checked)"}


def deadline_origin(make_env, episode: Path) -> dict:
    """Controlled clock: snapshot loading inside reset takes 61 s; the published rule counts from the start of reset."""
    from auditlab.environment import open_session
    now = [1000.0]
    clock = lambda: now[0]

    def slow_opener(public_dir):
        now[0] += 61.0           # simulated time spent inside reset before the episode becomes steppable
        return open_session(public_dir)
    env = make_env("closed", evaluator="inprocess", clock=clock, session_opener=slow_opener)
    entry = now[0]
    env.reset(1, {"snapshot_dir": str(episode), "policy_name": "probe", "time_limit_s": 60})
    obs, r, term, trunc, info = env.step({"action": "get_record", "args": {"table": "ledger", "id": "NO_SUCH_ROW"}})
    elapsed = now[0] - entry
    return {"status": "failed" if not trunc else "passed",
            "stimulus": "fake clock; session_opener inside reset advances it by 61 s; then one step",
            "expected": "truncated=true (61 s since the start of reset > 60 s)", "actual": {"truncated": trunc, "terminated": term, "elapsed_since_reset_entry_s": elapsed,
                                                                                         "remaining_steps": obs.get("remaining_steps")},
            "cause": "AuditEnvironment.reset sets _started after generation/loading/validate_truth/session opening (auditlab/runner/env.py)"}


def reset_wall_time(make_env, episode: Path) -> dict:
    env = make_env("closed", evaluator="inprocess")
    t0 = time.perf_counter(); env.reset(1, {"snapshot_dir": str(episode), "policy_name": "probe"}); dt = time.perf_counter() - t0
    rows = json.loads((episode / "public" / "manifest.json").read_text())["ledger_rows"]
    return {"ledger_rows": rows, "reset_wall_s": round(dt, 4), "note": "time currently excluded from the deadline; measured on this machine only"}
