"""Run the profile 1.2 study with independent assurance and bounded investigations."""
import argparse
from pathlib import Path
import subprocess

from auditlab.release.transforms import read_json
from .agent_bridge import make_claude_runner
from .study import run_study
from .validation import AssuranceValidator


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--reviewer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reserved-registry", type=Path, help="trusted registry of excluded economic families")
    parser.add_argument("--provider-config", type=Path)
    parser.add_argument("--confirm-api", action="store_true")
    args = parser.parse_args()
    plan = read_json(args.plan)
    if (plan.get("schema_version") != "2.0" or plan.get("episode_protocol_id") != "genesis-official-60s"
            or plan.get("generator", {}).get("config_version") != "1.2"):
        parser.error("Expected the version 2.0 study plan with profile 1.2 and the 60-second Genesis protocol")
    if bool(args.provider_config) != args.confirm_api:
        parser.error("External calls require both --provider-config and --confirm-api")
    source_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    external = None
    if args.provider_config:
        config = read_json(args.provider_config)
        cap = config.get("max_attempt_cost_micros")
        if config.get("protocol_id") != plan["episode_protocol_id"] or type(cap) is not int or cap <= 0:
            parser.error("API runner must match the plan protocol and declare a positive per-attempt cost cap")
        if any(type(plan[phase].get("cost_budget_micros")) is not int or plan[phase]["cost_budget_micros"] < cap
               or type(plan[phase].get("max_attempt_cost_micros")) is not int
               or plan[phase]["max_attempt_cost_micros"] < cap for phase in ("search", "reduction")):
            parser.error("Each phase must explicitly reserve API costs")
        per_goal = (2 * plan["search"]["cost_budget_micros"] + plan["reduction"]["cost_budget_micros"]
                    + 2 * plan["search"]["candidate_budget"] * plan["search"]["max_attempt_cost_micros"])
        total = plan.get("total_cost_budget_micros")
        if type(total) is not int or total < len(plan["objectives"]) * per_goal or not args.reserved_registry:
            parser.error("API execution requires a total budget covering all phases and the trusted reserved registry")
        try:
            external = make_claude_runner(config, source_sha, use_installed=True)
        except (ValueError, KeyError, OSError) as error:
            parser.error(str(error))
    exclusions = read_json(args.reserved_registry)["families"] if args.reserved_registry else []
    if external and (not exclusions or any(not row.get("snapshot") for row in exclusions)):
        parser.error("The trusted registry must identify the holdout snapshots for economic collision checks")
    validation = AssuranceValidator(args.reviewer)
    result = run_study(plan, source_sha, validation, args.output, external=external, exclusions=exclusions)
    print(args.output.resolve() / "study.json")
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
