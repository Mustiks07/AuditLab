"""Evaluator as a separate process (SPEC §3). One JSON request line on stdin, one JSON reply on stdout.

Request:  {"private_dir": str, "submission": object, "inspection_steps": int}
The closed truth is read here, from the closed volume, and never travels back to the caller;
the reply carries only the score and trusted diagnostics.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from auditlab.evaluator.matcher import evaluate


def handle(request: dict) -> dict:
    truth = json.loads((Path(request["private_dir"]) / "truth.json").read_text(encoding="utf-8"))
    result = evaluate(request["submission"], truth, request["inspection_steps"])
    return {"score": result.score, "status": result.status, "reason": result.reason,
            "matches": result.matches, "unmatched": {str(k): v for k, v in result.unmatched.items()}}


def main() -> int:
    try:
        reply = {"ok": True, "result": handle(json.loads(sys.stdin.readline()))}
    except Exception as error:  # reported to the trusted parent only
        reply = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()
    return 0 if reply["ok"] else 1
