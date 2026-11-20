"""Read-only diagnostics for the 2026-11-20 organizer definitions, not a complete suite."""
import copy
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from auditlab.evaluator.matcher import evaluate, _max_matching
from auditlab.evaluator.oracle import oracle_submission
from auditlab.runner.env import AuditEnvironment


def main():
    snapshot = ROOT / "fixtures/foundation/S02"
    truth = json.loads((snapshot / "private/truth.json").read_text())
    original = oracle_submission(truth)
    cases = []
    for name in ("baseline", "extra_unavailable_evidence", "unknown_string_type"):
        submission = copy.deepcopy(original)
        if name == "extra_unavailable_evidence":
            submission["findings"][0]["evidence_ids"].append("payments/DOES_NOT_EXIST")
        elif name == "unknown_string_type":
            submission["findings"][0]["type"] = "not_a_known_type"
        result = evaluate(submission, truth, 10)
        cases.append({"case": name, "status": result.status, "score": result.score,
                      "matches": result.matches, "reason": result.reason})
    env = AuditEnvironment("closed", evaluator="inprocess")
    try:
        env.reset(0, {"snapshot_dir": str(snapshot)})
        response = env.step({"name": "calculator", "args": {"expression": "0.1+0.2"}})[0]
    finally:
        env.close()
    print(json.dumps({"scope": "Read-only compatibility probes; not official acceptance or a score",
                      "cases": cases, "m11_owners_A_B": _max_matching([[0, 1], [0, 1]], 2),
                      "judge_adapter_importable": importlib.util.find_spec("judge_adapter") is not None,
                      "official_envelope_directly_to_internal_env": response},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
