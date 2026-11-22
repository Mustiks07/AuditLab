"""Write the frozen plans and protocol of agent-improvement v2 (Issue #42). Run from the repository root:

    python docs/research/agent-improvement-v2/make_plans.py

Deterministic: the same code writes byte-identical files. Plans name no key, only its variable.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from auditlab.research.agents.prompts import prompt_set_record  # noqa: E402

HERE = Path(__file__).resolve().parent
REL = HERE.relative_to(ROOT).as_posix()
REPLICATION = f"{REL}/replication-922101"
GEMINI = json.loads((ROOT / "docs/research/gemini/provider.free-tier.json").read_text(encoding="utf-8"))
PROVIDER = {k: GEMINI[k] for k in ("provider", "model", "api_key_env", "base_url", "reasoning_effort", "strict_tools", "prices")}
# Same agent settings for every arm; only the prompt addition differs between A, B and C.
AGENT = {"kind": "llm", "variant": "tools", "max_tokens": 4096, "max_model_calls_per_action": 3,
         "retry": {"max_tries": 3, "min_interval_s": 4.0}}
SERVICE = {"max_slot_retries": 2, "cooldown_s": 60.0, "max_cooldown_s": 600.0, "max_consecutive_service_failures": 3}
DOCKER = {"image": "auditlab-llm-bridge:gemini", "allow": ["generativelanguage.googleapis.com:443"]}
BUDGET = {"max_usd_total": 0.25, "max_usd_per_attempt": 0.01}


def write(name: str, value: dict) -> str:
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    (HERE / name).write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def arm(name: str, prompt_variant: str) -> dict:
    return {"name": name, "agent": {**AGENT, "prompt_variant": prompt_variant}, "provider": PROVIDER}


def replication_episodes() -> list[dict]:
    manifest = json.loads((ROOT / REPLICATION / "replication-set.json").read_text(encoding="utf-8"))
    return [{"episode_ref": f"{REPLICATION}/{name}", "family_id": row["family_id"], "split": "dev",
             "variant": row["variant"], "condition": name} for name, row in manifest["conditions"].items()]


def open_dev() -> list[dict]:
    return [{"episode_ref": d.relative_to(ROOT).as_posix(), "family_id": d.name, "split": "dev"}
            for d in sorted((ROOT / "datasets/open/dev").iterdir()) if (d / "public" / "manifest.json").is_file()]


def plan(artifact_id: str, protocol: str, episodes: list[dict], arms: list[dict], repetitions: int, purpose: str,
         note: str, pairs=()) -> dict:
    return {"schema_version": "1.0", "artifact_id": artifact_id, "protocol_id": protocol, "purpose": purpose,
            "transport": "docker", "docker": DOCKER, "repetitions": repetitions, "episodes": episodes, "arms": arms,
            "budget": BUDGET, "service": SERVICE, "env_seed": 0,
            "analysis": {"pairs": [list(p) for p in pairs], "bootstrap": 10000, "seed": 20261122}, "note": note}


def main() -> None:
    shas = {}
    replication = replication_episodes()
    shas["replication-official.plan.json"] = write("replication-official.plan.json", plan(
        "replication-922101-official", "genesis-official-60s", replication, [arm("A-tools", "A")], 3, "selection",
        "Repeat the live pilot miss: same model/settings as the pilot's tools agent, 4 frozen conditions x 3 attempts, "
        "official 60 s limit (as in the pilot). Dev only."))
    shas["replication-research.plan.json"] = write("replication-research.plan.json", plan(
        "replication-922101-research", "auditlab-research-600s", replication, [arm("A-tools", "A")], 3, "selection",
        "Same as the official plan with 600 s, so client pacing and rate limits cannot turn into time pressure. "
        "Never reported as an official-protocol result."))
    shas["abc-dev.plan.json"] = write("abc-dev.plan.json", plan(
        "abc-dev-tuning", "genesis-official-60s", open_dev() + [e for e in replication if e["condition"] in
                                                                  ("original_error", "corrected_clean")],
        [arm("A", "A"), arm("B", "B"), arm("C", "C")], 3, "selection",
        "Dev comparison of A/B/C for prompt development only (published dev + the pilot family). "
        "Not a final test; results here may change prompts only before protocol freeze.",
        pairs=(("B", "A"), ("C", "A"), ("C", "B"))))
    final = plan("abc-final-holdout", "genesis-official-60s", [], [arm("A", "A"), arm("B", "B"), arm("C", "C")], 3,
                 "final", "TEMPLATE. The integrator fills episodes from Claude Pro 2's sealed holdout (both variants of "
                 "each family, split=holdout) and runs it once, after this protocol is frozen.",
                 pairs=(("B", "A"), ("C", "A"), ("C", "B")))
    shas["abc-final.plan.template.json"] = write("abc-final.plan.template.json", final)
    # E3: the same agent (A, tools) on three models served by one local vLLM: base, LoRA on the uniform
    # curriculum, LoRA on the adaptive curriculum. Identical settings; only the weights differ.
    local = {"provider": "openai_compatible", "base_url": "http://127.0.0.1:8000/v1", "api_key_env": None,
             "strict_tools": False, "prices": {}}
    e3_arms = []
    for name, model in (("T0", "base"), ("T_random", "auditlab-lora-uniform"), ("T_adaptive", "auditlab-lora-adaptive")):
        provider = {**local, "model": model, "prices": {model: {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}},
                    "pricing_basis": "local GPU; GPU hours are recorded separately by the training records"}
        e3_arms.append({"name": name, "agent": {**AGENT, "prompt_variant": "A"}, "provider": provider})
    e3 = plan("e3-weights-template", "auditlab-research-600s", [], e3_arms, 3, "final",
              "TEMPLATE for a CUDA host: fill episodes (dev for checks, then the sealed holdout once). Transport is "
              "subprocess on the GPU host because the model server is local; it is a research-protocol result.",
              pairs=(("T_random", "T0"), ("T_adaptive", "T0"), ("T_adaptive", "T_random")))
    e3["transport"] = "subprocess"
    e3.pop("docker")
    shas["e3-training.plan.template.json"] = write("e3-training.plan.template.json", e3)
    protocol = {
        "schema_version": "1.0", "artifact_id": "agent-improvement-v2-protocol-1.0", "issue": 42,
        "recorded_before_results": True,
        "question": "Does an improvement developed on AuditLab material help an API auditor solve NEW financial cases "
                    "without more false accusations? A positive result is not assumed.",
        "arms": {"A": "original auditor", "B": "A + general audit-method instruction",
                 "C": "A + AuditLab-derived designated-payment procedure and one worked train example"},
        "held_equal": {"provider": PROVIDER, "agent": AGENT, "tools": "five official actions (tools variant)",
                       "limits": "genesis-official-60s: 60 s from reset incl. API latency, 100 actions",
                       "context_budget": "B and C additions within 10% characters (test); token use measured per call"},
        "prompts": prompt_set_record(),
        "tuning": {"allowed_splits": ["train", "dev"], "dev_plan": "abc-dev.plan.json",
                   "status": "no live dev tuning was possible before freeze (no provider key in this environment); "
                             "prompts are first versions frozen here"},
        "final": {"episodes": "Claude Pro 2 sealed holdout (agent-generalization-1.1), via the integrator",
                  "plan_template": "abc-final.plan.template.json", "runs_once": True},
        "repetitions": 3,
        "metrics": {"primary": "mean official return per family (family = mean over its clean and error variants and "
                               "repetitions), paired differences B-A, C-A, C-B with Claude Pro 2's cluster bootstrap "
                               "(10000, seed 20261121) and sign-flip test",
                    "secondary": ["micro F1", "issues missed by type", "clean attempts with a false accusation",
                                  "finding labels: wrong amount / currency / entities / evidence", "completion",
                                  "actions", "wall time", "input/output tokens", "cost"],
                    "service": "rate_limited, quota_exhausted, provider_error, bridge_failure, interrupted, budget_stop "
                               "are reported with their counts and never counted as model misses or FPs; Claude Pro 2's "
                               "official-rule scoring of every attempt is reported separately"},
        "decision": {"improvement_shown_only_if": "the 95% interval of the primary difference excludes 0 AND the "
                                                   "clean-accusation rate does not increase beyond its interval",
                     "null_and_negative_results": "published"},
        "replication": {"set": f"{REPLICATION}/replication-set.json",
                        "plans": ["replication-official.plan.json", "replication-research.plan.json"],
                        "criterion": "the miss is called repeatable only if >= 2 of 3 COMPLETED attempts on each error "
                                     "condition miss it; denominators include unfinished attempts"},
        "plans_sha256": shas,
    }
    write("protocol.json", protocol)
    print(json.dumps(shas, indent=1))


if __name__ == "__main__":
    main()
