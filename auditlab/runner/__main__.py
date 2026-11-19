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
    public = sub.add_parser("policy", help="run a public policy (auditlab.policies) in its own process")
    public.add_argument("--snapshot", required=True, help="directory with public/ and private/")
    public.add_argument("--name", required=True, help="random | reconcile | flag_all")
    public.add_argument("--seed", type=int, default=0)
    public.add_argument("--export-dir", help="new run directory for the open export")
    public.add_argument("--trusted-dir")
    public.add_argument("--run-id")
    args = parser.parse_args(argv)

    if args.command == "policy":
        return _run_public_policy(args)
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


def _run_public_policy(args) -> int:
    from auditlab.runner.isolation import SubprocessPolicy, child_env, policy_command
    env = make_env("train", trusted_dir=args.trusted_dir)
    config = {"snapshot_dir": args.snapshot, "policy_name": args.name}
    if args.export_dir:
        config["export_dir"] = args.export_dir
    if args.run_id:
        config["run_id"] = args.run_id
    policy = SubprocessPolicy(policy_command(args.name, args.seed), env=child_env())
    run_episode(env, policy, args.seed, config)
    env.close()
    graded = env.final["graded"]
    print(json.dumps({"status": env.final["status"], "reward": env.final["reward"],
                      "inspection_steps": env.final["inspection_steps"],
                      "score": graded["score"] if graded else None}))
    return 0 if env.final["status"] in ("submitted", "invalid_submission") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
