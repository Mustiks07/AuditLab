"""python -m auditlab.runner oracle --snapshot DIR [--export-dir NEW_RUN_DIR] [--inspections N]

Runs the trusted oracle through the real reset/step facade. `--export-dir` is the run directory
itself (INTERFACES 1.1): it must not exist yet. Without auditlab.environment, `--stub-session`
uses the test double from auditlab.runner.testing.
"""
from __future__ import annotations

import argparse
import json
import sys

from auditlab.runner import make_env
from auditlab.runner.episode import OraclePolicy, run_episode


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="auditlab.runner")
    sub = parser.add_subparsers(dest="command", required=True)
    oracle = sub.add_parser("oracle", help="run the trusted oracle on a snapshot directory")
    oracle.add_argument("--snapshot", required=True, help="directory with public/ and private/")
    oracle.add_argument("--mode", choices=("train", "closed"), default="train")
    oracle.add_argument("--export-dir", help="new run directory for the open export (train only)")
    oracle.add_argument("--trusted-dir")
    oracle.add_argument("--inspections", type=int, default=0)
    oracle.add_argument("--run-id")
    oracle.add_argument("--stub-session", action="store_true", help="use the PublicSession test double")
    args = parser.parse_args(argv)

    options = {"trusted_dir": args.trusted_dir}
    if args.stub_session:
        from auditlab.runner.testing import FixturePublicSession
        options["session_opener"] = FixturePublicSession
    env = make_env(args.mode, **options)
    config = {"snapshot_dir": args.snapshot, "policy_name": "oracle"}
    if args.export_dir:
        config["export_dir"] = args.export_dir
    if args.run_id:
        config["run_id"] = args.run_id
    result = run_episode(env, OraclePolicy(args.snapshot, args.inspections), 0, config)
    env.close()
    print(json.dumps({"status": env.final["status"], "reward": env.final["reward"],
                      "score": env.final["graded"]["score"] if env.final["graded"] else None}))
    return 0 if result[2] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
