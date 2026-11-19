"""Entry point inside the policy sandbox.

    python -m auditlab.runner.policy_host NAME SEED                 # auditlab.policies.make_policy(NAME, SEED)
    python -m auditlab.runner.policy_host --factory module:fn SEED  # fn(SEED), for external adapters

Reads one public observation per line from stdin, writes one action per line to stdout.
Imports nothing from the trusted side (evaluator, runner.env) and opens no episode files.
"""
from __future__ import annotations

import importlib
import json
import sys


def load_policy(argv: list[str]):
    if argv[0] == "--factory":
        module_name, _, attribute = argv[1].partition(":")
        return getattr(importlib.import_module(module_name), attribute)(int(argv[2]))
    from auditlab.policies import make_policy
    return make_policy(argv[0], int(argv[1]))


def main(argv: list[str]) -> int:
    policy = load_policy(argv)
    for line in sys.stdin:
        sys.stdout.write(json.dumps(policy.act(json.loads(line))) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
