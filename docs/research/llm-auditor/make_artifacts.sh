#!/usr/bin/env bash
# Regenerates the committed research artifacts from the repository root. No network, no paid call.
#   bash docs/research/llm-auditor/make_artifacts.sh [PATH_TO_CLAUDE2_REVIEW_CHECKOUT]
# The optional argument is a checkout of origin/review/agent-generalization; its compare_experiment
# re-scores the baseline run independently. Python 3.11+.
set -u
PY=${PYTHON:-python}
R=docs/research/llm-auditor/evidence
REVIEW=${1:-}
mkdir -p "$R"

# 1. Baseline policies on the published dev episodes under the official limits (real runs, zero cost)
B=$R/2026-11-21-baselines-open-dev
mkdir -p "$B"
cat > "$B/arms.json" <<'EOF'
{"arms": [
  {"name": "empty", "agent": {"kind": "baseline_policy", "policy": "empty"}},
  {"name": "flag_all", "agent": {"kind": "baseline_policy", "policy": "flag_all"}},
  {"name": "random", "agent": {"kind": "baseline_policy", "policy": "random", "seed": 1}},
  {"name": "reconcile", "agent": {"kind": "baseline_policy", "policy": "reconcile"}}
 ],
 "transport": "in_process",
 "analysis": {"pairs": [["reconcile", "empty"], ["reconcile", "flag_all"], ["random", "empty"]], "bootstrap": 10000, "seed": 20261121}}
EOF
$PY -m auditlab.research.agents plan --episodes datasets/open/dev --split dev --arms "$B/arms.json" \
    --protocol genesis-official-60s --repetitions 1 --purpose selection --artifact-id baselines-open-dev-2026-11-21 --out "$B/plan.json"
$PY -m auditlab.research.agents run --plan "$B/plan.json" --out "$B/out" > "$B/run.log"
$PY -m auditlab.research.agents export-review --plan "$B/plan.json" --out "$B/out" --target "$B/review-export"
if [ -n "$REVIEW" ]; then
  ( cd "$REVIEW" && $PY review/agent_generalization/run.py compare "$OLDPWD/$B/review-export" ) > "$B/independent-compare.json"
fi

# 2. E1 (memory vs no memory) plan: dry-run and the refused paid run (no key, no limit)
E=$R/2026-11-21-e1-dry-run
mkdir -p "$E"
$PY -m auditlab.research.agents plan --episodes datasets/open/dev --split dev --arms docs/research/llm-auditor/configs/e1-memory-arms.json \
    --protocol genesis-official-60s --repetitions 3 --purpose selection --artifact-id e1-memory-open-dev-dry-run --out "$E/plan.json"
$PY -m auditlab.research.agents dry-run --plan "$E/plan.json" > "$E/dry-run.json"
$PY -m auditlab.research.agents run --plan "$E/plan.json" --out "$E/out" --confirm-paid 2> "$E/run-refused.json"; echo "exit=$?" >> "$E/run-refused.json"
$PY -m auditlab.research.agents preflight --provider docs/research/llm-auditor/configs/claude-opus-5-5.json > "$E/preflight.json"
$PY -m auditlab.research.agents smoke --provider docs/research/llm-auditor/configs/claude-opus-5-5.json --budget-usd 0.5 --confirm-paid \
    --out "$E/smoke" > "$E/smoke.json"; echo "exit=$?" >> "$E/smoke.json"

# 3. Weight-training path (config paths are relative to the config file; jobs default to <out>/jobs): oracle demonstrations on the published TRAIN split -> SFT dataset -> LoRA job
T=$R/2026-11-21-training-dry-run
mkdir -p "$T"
$PY - "$T" <<'EOF'
import json, sys
from pathlib import Path
from auditlab.research.training.demonstrations import run_demonstration
out = Path(sys.argv[1])
episodes = [{"episode_ref": d.as_posix(), "family_id": d.name, "split": "train"}
            for d in sorted(Path("datasets/open/train").iterdir()) if (d / "public" / "manifest.json").is_file()]
for episode in episodes:
    run_demonstration(episode, out / "demonstrations")
(out / "curriculum.json").write_text(json.dumps({"schema_version": "1.0", "artifact_id": "published-train-2026-11-21",
    "kind": "published_train", "note": "all 10 published train episodes; NOT a random or adaptive curriculum arm",
    "episodes": episodes}, indent=1), encoding="utf-8")
EOF
$PY -m auditlab.research.training build-sft --roots "$T/demonstrations" --out "$T/dataset" --splits train \
    --curriculum "$T/curriculum.json" > "$T/build-sft.json"
cat > "$T/lora-config.json" <<'EOF'
{"schema_version": "1.0", "protocol_id": "auditlab-research-600s", "provider": "local_lora",
 "base_model": "Qwen/Qwen2.5-7B-Instruct", "dataset": "dataset",
 "hyperparameters": {"n_epochs": 2, "learning_rate": 0.0001, "batch_size": 1}, "lora": {"r": 16, "alpha": 32},
 "seed": 20261121, "lora_module_name": "auditlab-lora-published-train", "min_examples": 10, "poll_interval_s": 60,
 "note": "base model is a placeholder until the integrator fixes the trainable model and GPU host"}
EOF
$PY -m auditlab.research.training train --config "$T/lora-config.json" --curriculum "$T/curriculum.json" --out "$T/dry-run" --dry-run > "$T/train-dry-run.json"
$PY -m auditlab.research.training train --config "$T/lora-config.json" --curriculum "$T/curriculum.json" --out "$T/attempt" > "$T/train-attempt.json"
echo "exit=$?" >> "$T/train-attempt.json"
echo done
