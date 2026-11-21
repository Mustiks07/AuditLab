"""Experiment-root fixtures shaped like auditlab/research/agents/execute.py output. All content is synthetic.

    ROOT/attempts/<attempt_id>/attempt.json, prompt.json, transcript.jsonl, steps.jsonl, provider_calls.jsonl
    ROOT/trusted/<attempt_id>/evaluation.json
"""
from __future__ import annotations

import json
from pathlib import Path

TOOLS = [
    {"name": "query", "description": "Read rows from a public table.",
     "input_schema": {"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"]}},
    {"name": "submit", "description": "Submit findings.",
     "input_schema": {"type": "object", "properties": {"findings": {"type": "array"}}, "required": ["findings"]}},
]
HIDDEN_REASONING = "HIDDEN-THINKING-MUST-NOT-LEAK"
ORACLE = {"kind": "oracle_demonstration", "test_double": False}


def api_agent(provider: str, model: str, *, test_double: bool = False) -> dict:
    return {"variant": "notes", "prompt_hash": "ph-1", "provider": {"provider": provider, "model": model},
            "test_double": test_double}


def transcript(n: int) -> list[dict]:
    return [
        {"role": "user", "text": f"Public observation for episode {n}."},
        {"role": "assistant", "text": "",
         "native": [{"type": "thinking", "thinking": HIDDEN_REASONING, "signature": "sig"},
                    {"type": "tool_use", "id": f"toolu_{n}_1", "name": "query", "input": {"table": "ledger"}}],
         "tool_calls": [{"id": f"toolu_{n}_1", "name": "query", "input": {"table": "ledger"}}]},
        {"role": "tool_results", "results": [{"id": f"toolu_{n}_1", "content": "row 1: 100.00", "is_error": False}],
         "text": None},
        {"role": "assistant", "text": "Submitting the confirmed finding.",
         "native": [{"type": "text", "text": "Submitting the confirmed finding."}],
         "tool_calls": [{"id": f"toolu_{n}_2", "name": "submit", "input": {"findings": [{"row": 1}]}}]},
        {"role": "tool_results", "results": [{"id": f"toolu_{n}_2", "content": "episode finished", "is_error": False}],
         "text": None},
    ]


def make_attempt(root: Path, n: int, *, split: str = "train", status: str = "completed", test_double: bool = False,
                 ret: float | None = 1.0, family: str = "fam-a", agent: dict | None = None) -> Path:
    attempt_id = f"att-{n:03d}-{split}"
    directory = Path(root) / "attempts" / attempt_id
    directory.mkdir(parents=True, exist_ok=True)
    trusted = Path(root) / "trusted" / attempt_id
    trusted.mkdir(parents=True, exist_ok=True)
    agent = dict(agent or ORACLE)
    if test_double:
        agent["test_double"] = True
    attempt = {"schema_version": "1.0", "artifact_id": attempt_id, "attempt_id": attempt_id, "kind": "attempt",
               "protocol_id": "auditlab-research-600s", "status": status, "test_double": test_double,
               "episode": {"episode_ref": f"ep-{n:03d}-{split}", "episode_sha256": f"{n:064x}", "family_id": family,
                           "split": split, "label": "synthetic"},
               "agent": agent, "final_answer": [{"row": 1}], "inspection_steps": 1,
               "evaluation_ref": f"trusted/{attempt_id}/evaluation.json"}
    (directory / "attempt.json").write_text(json.dumps(attempt), encoding="utf-8")
    (directory / "prompt.json").write_text(json.dumps({"system": "You are an auditor.", "tools": TOOLS,
                                                       "prompt_hash": "ph-1"}), encoding="utf-8")
    (directory / "transcript.jsonl").write_text("\n".join(json.dumps(e) for e in transcript(n)) + "\n", encoding="utf-8")
    (directory / "steps.jsonl").write_text(json.dumps({"index": 1, "action": {"name": "submit"}}) + "\n", encoding="utf-8")
    (directory / "provider_calls.jsonl").write_text("", encoding="utf-8")
    official = None if ret is None else {"tp": 1, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0, "f1": 1.0,
                                         "return": ret}
    (trusted / "evaluation.json").write_text(json.dumps({"attempt_id": attempt_id, "official": official,
                                                         "official_by_rule": official, "status": status}),
                                             encoding="utf-8")
    return directory


def make_curriculum(path: Path, episode_refs, *, split: str = "train", kind: str = "random") -> Path:
    data = {"schema_version": "1.0", "artifact_id": f"cur-{kind}", "kind": kind, "families": ["fam-a"],
            "episodes": [{"episode_ref": ref, "family_id": "fam-a", "split": split} for ref in episode_refs]}
    Path(path).write_text(json.dumps(data), encoding="utf-8")
    return Path(path)
