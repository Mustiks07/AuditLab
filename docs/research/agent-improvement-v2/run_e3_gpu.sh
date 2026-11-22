#!/usr/bin/env bash
# E3 weight training on a CUDA host (NOT runnable on the AuditLab offline machine). Run from the repository root.
# Compares the base model (T0), LoRA on the uniform curriculum (T_random) and LoRA on the adaptive curriculum
# (T_adaptive) with identical data size, settings and compute. Few-shot, memory or prompts are NOT this.
set -eu
BASE_MODEL=${BASE_MODEL:?set the open model id or local path, e.g. Qwen/Qwen2.5-7B-Instruct}
OUT=${OUT:-artifacts/e3}

# 0. The machine must actually be able to train: stops here with concrete blockers otherwise.
python -m auditlab.research.training probe-hardware | tee "$OUT-probe.json"
python - "$OUT-probe.json" <<'EOF'
import json, sys; d = json.load(open(sys.argv[1])); sys.exit(0 if d["weight_training_possible"] else 3)
EOF

# 1. Two curricula of TRAIN episodes with the same number of examples, from GPT Pro 1's frozen search:
#    UNIFORM_CURRICULUM (uniform candidate selection) and ADAPTIVE_CURRICULUM (adaptive selection), each a
#    curriculum.json written by auditlab.research.curriculum's export (see docs/research/financial-curriculum).
UNIFORM_CURRICULUM=${UNIFORM_CURRICULUM:?path to the uniform arm curriculum.json}
ADAPTIVE_CURRICULUM=${ADAPTIVE_CURRICULUM:?path to the adaptive arm curriculum.json}

# 2. Oracle demonstrations of each curriculum's train episodes -> SFT datasets -> equal budgets.
for ARM in uniform adaptive; do
  CUR=$([ "$ARM" = uniform ] && echo "$UNIFORM_CURRICULUM" || echo "$ADAPTIVE_CURRICULUM")
  python -m auditlab.research.training demonstrate --curriculum "$CUR" --out "$OUT/demo-$ARM"
  python -m auditlab.research.training build-sft --roots "$OUT/demo-$ARM" --curriculum "$CUR" --splits train       --out "$OUT/sft-$ARM-raw"
done
python -m auditlab.research.training equalize --datasets "$OUT/sft-uniform-raw" "$OUT/sft-adaptive-raw"     --outs "$OUT/sft-uniform" "$OUT/sft-adaptive" --seed 20261122

# 3. Identical LoRA settings; only the dataset differs. Each job is idempotent (resume never duplicates).
for ARM in uniform adaptive; do
  cat > "$OUT/lora-$ARM.json" <<EOF
{"schema_version": "1.0", "protocol_id": "auditlab-research-600s", "provider": "local_lora", "base_model": "$BASE_MODEL",
 "dataset": "sft-$ARM", "hyperparameters": {"n_epochs": 2, "learning_rate": 0.0001, "batch_size": 1},
 "lora": {"r": 16, "alpha": 32}, "seed": 20261122, "lora_module_name": "auditlab-lora-$ARM", "min_examples": 10}
EOF
  CUR=$([ "$ARM" = uniform ] && echo "$UNIFORM_CURRICULUM" || echo "$ADAPTIVE_CURRICULUM")
  python -m auditlab.research.training train --config "$OUT/lora-$ARM.json" --curriculum "$CUR" \
      --out "$OUT/train-$ARM" --max-wait 86400
done

# 4. Serve base + both adapters (vLLM, OpenAI-compatible, tool calling) and evaluate with the same agent.
#    vllm serve "$BASE_MODEL" --served-model-name base --enable-lora \
#      --lora-modules auditlab-lora-uniform=<adapter dir> auditlab-lora-adaptive=<adapter dir> \
#      --enable-auto-tool-choice --tool-call-parser hermes --port 8000
#    Fill the episodes of docs/research/agent-improvement-v2/e3-training.plan.template.json, then:
#    python -m auditlab.research.agents run --plan e3.plan.json --out "$OUT/eval" --confirm-paid
echo "trained; serve and evaluate as described in step 4"
