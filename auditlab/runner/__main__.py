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
    public = sub.add_parser("policy", help="run a public policy in an isolated local Docker image")
    public.add_argument("--snapshot", required=True, help="directory with public/ and private/")
    public.add_argument("--name", required=True, help="random | reconcile | flag_all")
    public.add_argument("--seed", type=int, default=0, help="public policy RNG seed (not the company seed)")
    public.add_argument("--mode", choices=("train", "closed"), default="closed")
    public.add_argument("--image", default="auditlab-policy:release", help="preloaded local image; never pulled")
    public.add_argument("--time-limit", type=float, default=60.0)
    public.add_argument("--unsafe-dev-subprocess", action="store_true",
                        help="train-only development mode: NO filesystem/network isolation")
    public.add_argument("--export-dir", help="new run directory for the open export")
    public.add_argument("--trusted-dir")
    public.add_argument("--run-id")
    args = parser.parse_args(argv)

    if args.command == "policy":
        if args.unsafe_dev_subprocess and args.mode != "train":
            parser.error("--unsafe-dev-subprocess is allowed only with --mode train")
        if args.mode == "closed" and args.export_dir:
            parser.error("closed runs use --trusted-dir, never --export-dir")
        if not args.time_limit > 0 or args.time_limit > 60:
            parser.error("--time-limit must be greater than 0 and at most 60 seconds")
        try:
            return _run_public_policy(args)
        except (OSError, RuntimeError) as error:
            print(json.dumps({"status": "infrastructure_error", "error": str(error)}), file=sys.stderr)
            return 2
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
    from auditlab.runner.isolation import DockerPolicy, SubprocessPolicy, child_env, policy_command
    env = make_env(args.mode, trusted_dir=args.trusted_dir)
    config = {"snapshot_dir": args.snapshot, "policy_name": args.name, "time_limit_s": args.time_limit}
    if args.export_dir:
        config["export_dir"] = args.export_dir
    if args.run_id:
        config["run_id"] = args.run_id
    if args.unsafe_dev_subprocess:
        print("UNSAFE DEVELOPMENT MODE: policy can read/write host files and use the network.", file=sys.stderr)
        policy = SubprocessPolicy(policy_command(args.name, args.seed), env=child_env())
    else:
        policy = DockerPolicy(args.image, args.name, args.seed)
    try:
        # The company is already fixed by snapshot; policy RNG seed is independent.
        run_episode(env, policy, 0, config)
    finally:
        env.close()
    graded = env.final["graded"]
    print(json.dumps({"status": env.final["status"], "reward": env.final["reward"],
                      "inspection_steps": env.final["inspection_steps"],
                      "score": graded["score"] if graded else None}))
    return 0 if env.final["status"] in ("submitted", "invalid_submission") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
