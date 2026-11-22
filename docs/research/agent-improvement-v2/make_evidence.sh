#!/usr/bin/env bash
# Zero-cost evidence of agent-improvement v2, from the repository root (Python 3.11+, no network, no key):
#   bash docs/research/agent-improvement-v2/make_evidence.sh
# 1. local baseline policies on the frozen replication set (sanity of the set; not a model experiment),
#    independently re-scored by Claude Pro 2's compare and published as console envelopes;
# 2. dry-runs of every live plan, and the refused live run (no key here);
# 3. the hardware probe for weight training.
set -u
PY=${PYTHON:-python}
D=docs/research/agent-improvement-v2
E=$D/evidence
rm -rf "$E" && mkdir -p "$E"
$PY - "$D" <<'EOF'
import json, sys
from pathlib import Path
d = Path(sys.argv[1])
episodes = json.loads((d / "replication-official.plan.json").read_text(encoding="utf-8"))["episodes"]
plan = {"schema_version": "1.0", "artifact_id": "baselines-replication-922101", "protocol_id": "genesis-official-60s",
        "purpose": "selection", "transport": "in_process", "repetitions": 1, "episodes": episodes, "env_seed": 0,
        "arms": [{"name": n, "agent": {"kind": "baseline_policy", "policy": n}} for n in ("empty", "flag_all", "reconcile")],
        "analysis": {"pairs": [["reconcile", "empty"], ["reconcile", "flag_all"]], "bootstrap": 10000, "seed": 20261122},
        "note": "Local policies on the frozen replication set: checks the set, not a model."}
(d / "evidence" / "baselines.plan.json").write_bytes((json.dumps(plan, indent=2) + "\n").encode())
EOF
$PY -m auditlab.research.agents run --plan "$E/baselines.plan.json" --out "$E/baselines" > "$E/baselines.log"
$PY -m auditlab.research.agents export-review --plan "$E/baselines.plan.json" --out "$E/baselines" --target "$E/baselines-review" > /dev/null
$PY review/agent_generalization/run.py compare "$E/baselines-review" > "$E/baselines-independent-compare.json"
$PY -m auditlab.research publish --plan "$E/baselines.plan.json" --output "$E/baselines" --target "$E/console" \
    --comparison "$E/baselines-independent-compare.json" > "$E/console-publish.log" 2>&1; echo "exit=$?" >> "$E/console-publish.log"
for f in replication-official replication-research abc-dev; do
  $PY -m auditlab.research.agents dry-run --plan "$D/$f.plan.json" > "$E/$f.dry-run.json"
done
$PY -m auditlab.research.agents run --plan "$D/replication-official.plan.json" --out "$E/replication-live" --confirm-paid \
    2> "$E/replication-live.refused.json"; echo "exit=$?" >> "$E/replication-live.refused.json"
$PY -m auditlab.research.training probe-hardware > "$E/hardware-probe.json"
echo done
