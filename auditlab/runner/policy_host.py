"""Entry point that runs inside the policy sandbox: `python -m auditlab.runner.policy_host module:callable SEED`.

Reads one public observation per line from stdin, writes one action per line to stdout. The factory
is called as factory(seed) and returns an object with act(public_observation) -> action.
It must not import anything from auditlab.evaluator or read any episode files.
"""
from __future__ import annotations

import importlib
import json
import sys


def main(argv: list[str]) -> int:
    module_name, _, attribute = argv[1].partition(":")
    policy = getattr(importlib.import_module(module_name), attribute)(int(argv[2]))
    for line in sys.stdin:
        action = policy.act(json.loads(line))
        sys.stdout.write(json.dumps(action) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
