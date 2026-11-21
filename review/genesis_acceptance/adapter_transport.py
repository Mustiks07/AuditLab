"""D/P transport through the jury adapter: generate_episode, episode_dir loading, exact replay, score_episode."""
from __future__ import annotations

import hashlib, json, shutil
from pathlib import Path

from .data_checks import SCENARIOS, oracle


def _tree_hash(root: Path) -> dict:
    return {str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


def data_transport(adapter, work: Path) -> dict:
    """adapter.generate_episode must produce exactly the bytes the team generator produced for the same seed/config."""
    if adapter is None:
        return {}
    out = {}
    for did, (seed, config, _) in SCENARIOS.items():
        d = work / "adapter-gen" / did
        if d.exists(): shutil.rmtree(d)
        adapter.generate_episode(seed, {"team_config": config}, str(d))
        same = _tree_hash(d) == _tree_hash(work / "episodes" / did)
        out[did] = "passed" if same else "failed (adapter output differs from the team generator)"
    return out


def _actions(public: Path):
    ledger = (public / "ledger.csv").read_text(encoding="utf-8").splitlines()[1].split(",")[0]
    doc = json.loads((public / "invoices.jsonl").read_text(encoding="utf-8").splitlines()[0])["doc_id"]
    return [{"name": "list_records", "args": {"table": "ledger", "cursor": None, "limit": 50}},
            {"name": "get_record", "args": {"table": "ledger", "id": ledger}},
            {"name": "get_document", "args": {"doc_id": doc}},
            {"name": "get_record", "args": {"table": "ledger", "id": "NO_SUCH_ROW"}},
            {"name": "calculator", "args": {"expression": "1+2"}}]


def pair_transport(adapter, src: Path, dst: Path) -> dict:
    if adapter is None:
        return {"status": "not_checked", "reason": "judge_adapter.py is not importable at this commit"}

    def run(episode):
        env = adapter.make_env()
        trace = [env.reset(5, {"mode": "train", "episode_dir": str(episode), "team_config": {}})]
        for a in _actions(episode / "public"):
            trace.append(env.step(a))
        return json.loads(json.dumps(trace))
    replay = run(src) == run(src)
    loads = run(dst)[1][0].get("error") is None  # transformed copy loads as is and serves records
    t0 = json.loads((src / "private" / "truth.json").read_text(encoding="utf-8")); t1 = json.loads((dst / "private" / "truth.json").read_text(encoding="utf-8"))
    s0 = adapter.score_episode(str(src), {"episode_id": t0["episode_id"], "findings": oracle(t0)}, 12)
    s1 = adapter.score_episode(str(dst), {"episode_id": t1["episode_id"], "findings": oracle(t1)}, 12)
    ok = replay and loads and s0 == s1 and s0.get("fn") == 0 and s0.get("fp") == 0
    return {"status": "passed" if ok else "failed", "exact_replay": replay, "transformed_loads": loads, "score_original": s0, "score_transformed": s1}
