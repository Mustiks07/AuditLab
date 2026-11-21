"""Oracle demonstrations: zero-cost teacher trajectories for weight training, built on the TRUSTED side.

For a training-split episode the trusted controller reads truth and scripts a short, valid trajectory
through the official adapter: read every evidence record of each issue's smallest admissible set with
get_record, then submit the correct findings (a clean episode reads the payment and invoice pages and
submits nothing). The trajectory runs through the same LLMAuditor ("tools" variant), executor and
transcript format as a real agent, so the SFT examples match the agent's prompt and tools exactly.

Why this teacher: it needs no third-party model (no terms question, no mixing of a model change with
the effect of task selection) and it costs nothing, so the random and adaptive curriculum arms differ
only in WHICH episodes are demonstrated. It is a demonstration, never an agent result: attempts carry
agent.kind = "oracle_demonstration" and are refused outside train/curriculum/search splits.
"""
from __future__ import annotations

import json
from pathlib import Path

from auditlab.research.agents.execute import InProcessBridge, run_attempt
from auditlab.research.agents.providers import ScriptedProvider, scripted_turn

TEACHER = "oracle_demonstration"
MODEL = "oracle-demonstration"
ALLOWED_SPLITS = ("train", "curriculum", "search")
TABLE_OF = {"ledger": "ledger", "invoices": "invoices", "payments": "payments", "fx": "fx", "policy": "policy"}


class DemonstrationProvider(ScriptedProvider):
    """Replays the trusted script. Not a test double and not a model: priced at zero, labelled as teacher."""

    name = TEACHER
    is_test_double = False

    def describe(self) -> dict:
        return {"provider": TEACHER, "model": MODEL, "teacher": True}


def _read(key: str):
    table, _, ident = key.partition("/")
    if table not in TABLE_OF or not ident:
        raise ValueError(f"evidence key {key!r} has no readable table")
    return scripted_turn("get_record", {"table": table, "id": ident}, model=MODEL)


def demonstration_script(episode_dir: Path) -> list:
    truth = json.loads((Path(episode_dir) / "private" / "truth.json").read_text(encoding="utf-8"))
    manifest = json.loads((Path(episode_dir) / "public" / "manifest.json").read_text(encoding="utf-8"))
    script, findings, seen = [], [], set()
    for issue in sorted(truth["issues"], key=lambda item: item["issue_id"]):
        evidence = min(issue["evidence_sets"], key=lambda group: (len(group), sorted(group)))
        for key in sorted(evidence):
            if key not in seen:
                seen.add(key)
                script.append(_read(key))
        findings.append({"type": issue["type"], "affected_ids": list(issue["affected_ids"]),
                         "claimed_amount_minor": int(issue["expected_amount_minor"]), "currency": issue["currency"],
                         "evidence_ids": sorted(evidence),
                         "explanation": f"{issue['type']} on {', '.join(issue['affected_ids'])}: the cited records show a "
                                        f"discrepancy of {issue['expected_amount_minor']} minor units of {issue['currency']}."})
    if not findings:
        script += [scripted_turn("list_records", {"table": table, "cursor": None, "limit": 50}, model=MODEL)
                   for table in ("payments", "invoices")]
    script.append(scripted_turn("submit", {"episode_id": manifest["episode_id"], "findings": findings}, model=MODEL))
    return script


def run_demonstration(episode: dict, output_root: str | Path, attempt_id: str | None = None) -> dict:
    """episode: {episode_ref, family_id, split, ...}. Writes a normal attempt record (agent.kind = teacher)."""
    if episode.get("split") not in ALLOWED_SPLITS:
        raise ValueError(f"demonstrations are built only for {ALLOWED_SPLITS}, not {episode.get('split')!r}")
    episode_dir = Path(episode["episode_ref"])
    provider = DemonstrationProvider(demonstration_script(episode_dir), model=MODEL)
    spec = {"attempt_id": attempt_id or f"demo-{episode_dir.name}", "episode_ref": str(episode_dir),
            "episode": {k: v for k, v in episode.items() if k != "episode_ref"}, "variant": TEACHER,
            "agent": {"variant": "tools", "max_prechecks": 0}, "teacher_source": TEACHER,
            "provider": {"provider": TEACHER, "model": MODEL, "prices": {MODEL: {"input": 0, "output": 0}}},
            "protocol_id": "auditlab-research-600s", "budget_usd": 0.0}
    return run_attempt(spec, output_root, lambda: InProcessBridge(provider))
