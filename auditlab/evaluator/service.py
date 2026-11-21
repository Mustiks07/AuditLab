"""Evaluator as a separate process (SPEC §3). One JSON request line on stdin, one JSON reply on stdout.

Request:  {"private_dir": str, "submission": object, "inspection_steps": int,
           "public_dir": str (optional, default: <private_dir>/../public),
           "rules": "auditlab-1.1" | "genesis-2026-11-20" (optional, default auditlab-1.1)}
The closed truth is read here, from the closed volume, and never travels back to the caller;
the reply carries only the score and trusted diagnostics. Evidence availability is checked
against the episode's public files (evidence.PublicEvidence) whenever they are present; the
Genesis rules require them. The reply format of the default rules is unchanged.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from auditlab.evaluator.evidence import PublicEvidence
from auditlab.evaluator.matcher import evaluate
from auditlab.evaluator.schema import RULES, RULES_GENESIS, RULES_INTERNAL

TOLERANCE_KEY = "matching_tolerance_minor"


def _is_tolerance(value: object) -> bool:
    return type(value) is int and value >= 0


def public_tolerance(public_dir: Path) -> int | dict:
    """Genesis rules: the published per-type amount tolerance, 0 for every type by default.

    A fixture may declare it in public policy.json as `matching_tolerance_minor`: one nonnegative
    integer for all types, or {issue type: nonnegative integer}. It is public and fixed before the
    run and never derived from the submission. Any other value is a fixture error.
    """
    policy_file = public_dir / "policy.json"
    policy = json.loads(policy_file.read_text(encoding="utf-8")) if policy_file.is_file() else {}
    value = policy.get(TOLERANCE_KEY, 0) if isinstance(policy, dict) else 0
    if _is_tolerance(value):
        return value
    if isinstance(value, dict) and all(isinstance(key, str) and _is_tolerance(item) for key, item in value.items()):
        return dict(value)
    raise ValueError(f"policy.{TOLERANCE_KEY} must be a nonnegative integer or an object of them")


def handle(request: dict) -> dict:
    rules = request.get("rules", RULES_INTERNAL)
    if rules not in RULES:
        raise ValueError(f"unknown rules: {rules!r}")
    private_dir = Path(request["private_dir"])
    public_dir = Path(request["public_dir"]) if request.get("public_dir") else private_dir.parent / "public"
    truth = json.loads((private_dir / "truth.json").read_text(encoding="utf-8"))
    has_public = (public_dir / "manifest.json").is_file()
    if rules == RULES_GENESIS and not has_public:
        raise ValueError("the Genesis rules need the episode's public files for evidence availability")
    available = PublicEvidence(public_dir) if has_public else None
    tolerance = public_tolerance(public_dir) if rules == RULES_GENESIS else 0
    result = evaluate(request["submission"], truth, request["inspection_steps"], tolerance,
                      rules=rules, available_evidence=available)
    reply = {"score": result.score, "status": result.status, "reason": result.reason,
             "matches": result.matches, "unmatched": {str(k): v for k, v in result.unmatched.items()}}
    if rules != RULES_INTERNAL:
        reply["rules"] = rules
    return reply


def main() -> int:
    try:
        reply = {"ok": True, "result": handle(json.loads(sys.stdin.readline()))}
    except Exception as error:  # reported to the trusted parent only
        reply = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()
    return 0 if reply["ok"] else 1
