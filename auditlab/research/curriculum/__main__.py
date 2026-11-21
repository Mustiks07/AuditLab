"""Run a financial curriculum experiment with a built-in or configured Claude 1 policy."""
import argparse
from pathlib import Path
import subprocess

from auditlab.release.transforms import read_json
from .agent_bridge import make_claude_runner
from .laboratory import local_runner, run_experiment
from .validation import IndependentValidator


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--reviewer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    agent = parser.add_mutually_exclusive_group()
    agent.add_argument("--policy", choices=("random", "flag_all", "reconcile"))
    agent.add_argument("--claude-config", type=Path, help="trusted opt-in to provider calls using explicit plan budgets")
    args = parser.parse_args()
    plan = read_json(args.plan)
    if plan.get("schema_version") != "1.0":
        parser.error("expected a version 1.0 curriculum plan")
    if plan.get("episode_protocol_id") != "genesis-official-60s":
        parser.error("the experiment requires the 100-action, 60-second episode protocol")
    source_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    reviewer = IndependentValidator(args.reviewer)
    if args.claude_config:
        config = read_json(args.claude_config)
        if config.get("protocol_id") != plan["episode_protocol_id"]:
            parser.error("Claude runner protocol must match the plan's episode protocol")
        try:
            investigate = make_claude_runner(config, source_sha)
        except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
            parser.error(str(error))
        for phase in ("search", "reduction"):
            limit = plan[phase].get("max_attempt_cost_micros")
            total = plan[phase].get("cost_budget_micros")
            if type(limit) is not int or limit < investigate.max_cost or type(total) is not int or total < limit:
                parser.error(f"{phase} must explicitly reserve the Claude per-attempt cost in the phase budget")
        metadata = investigate.metadata()
    else:
        policy = args.policy or "random"
        investigate = local_runner(policy, source_sha, plan["protocol_id"])
        metadata = {"kind": "built_in_policy", "name": policy, "provider_calls": 0, "network": False}
    result = run_experiment(plan, source_sha, reviewer, investigate, args.output, agent_metadata=metadata)
    print(args.output.resolve() / "experiment.json")
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
