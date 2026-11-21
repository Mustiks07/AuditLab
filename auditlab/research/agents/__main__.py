"""python -m auditlab.research.agents COMMAND

    preflight --provider CFG               what a live run still needs (no network, no secret values)
    smoke --provider CFG --budget-usd X --confirm-paid --out DIR
                                           one minimal live provider request (paid); writes smoke.json
    plan --episodes DIR[,DIR] --split S --arms ARMS.json --protocol ID --out PLAN.json [--repetitions N]
    dry-run --plan PLAN                    attempts, worst-case spend and missing conditions; no network
    run --plan PLAN --out DIR [--confirm-paid]
    summarize --plan PLAN --out DIR
    export --out DIR --target NEWDIR       public attempt records (AuditLab layout)
    export-review --plan PLAN --out DIR --target NEWDIR
                                           experiment.json + attempts/*.json for Claude Pro 2's compare_experiment

Configs never contain keys: a provider config names the environment variable that holds the key.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

from auditlab.research.agents import experiment
from auditlab.research.agents.agent import ENV_TOOL_SPECS
from auditlab.research.agents.pricing import PRICE_SOURCE, price_for
from auditlab.research.agents.protocol import PROTOCOLS
from auditlab.research.agents.providers import ProviderError, ProviderRequest, make_provider
from auditlab.research.agents.reliability import ModelCaller, RetryPolicy, SpendGuard


def _json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def preflight(config: dict) -> dict:
    kind = config.get("provider")
    missing = []
    if kind == "anthropic" and importlib.util.find_spec("anthropic") is None:
        missing.append("the 'anthropic' SDK is not installed in the bridge environment")
    key_env = config.get("api_key_env", "ANTHROPIC_API_KEY" if kind == "anthropic" else "OPENAI_API_KEY")
    if kind == "anthropic" and not key_env:
        missing.append("the Anthropic provider needs api_key_env")
    elif key_env and not os.environ.get(key_env):
        missing.append(f"secret environment variable {key_env} is not set (value never printed)")
    if price_for(config.get("model", ""), config.get("prices")) is None:
        missing.append(f"no price for model {config.get('model')!r}; add 'prices' to the provider config")
    return {"provider": kind, "model": config.get("model"), "price_source": PRICE_SOURCE,
            "ready_for_paid_calls": not missing, "missing_conditions": missing, "network_calls": 0}


def smoke(config: dict, budget_usd: float, confirm_paid: bool, out: Path) -> dict:
    """One real request: the model must answer with a well-formed calculator tool call."""
    check = preflight(config)
    if check["missing_conditions"] or not confirm_paid or not budget_usd > 0:
        reasons = check["missing_conditions"] + ([] if confirm_paid else ["--confirm-paid not given"]) + (
            [] if budget_usd > 0 else ["--budget-usd must be positive"])
        return {"status": "not_checked", "missing_conditions": reasons, "network_calls": 0}
    provider = make_provider(config)
    caller = ModelCaller(provider, RetryPolicy(max_tries=2), SpendGuard(budget_usd, config.get("prices")))
    request = ProviderRequest("You are testing a tool connection. Call the calculator tool with the expression 1+1. "
                              "Do not answer in text.", [{"role": "user", "text": "Start."}],
                              [spec for spec in ENV_TOOL_SPECS if spec.name == "calculator"], 1024, 30.0)
    started = time.monotonic()
    try:
        response = caller.call(request, time.monotonic() + 60)
        calls = [{"name": c.name, "input": c.input} for c in response.tool_calls]
        ok = calls == [{"name": "calculator", "input": {"expression": "1+1"}}] or (
            len(calls) == 1 and calls[0]["name"] == "calculator")
        result = {"status": "passed" if ok else "failed", "tool_calls": calls, "stop_reason": response.stop_reason,
                  "model_served": response.model_served, "served_by_fallback": response.served_by_fallback}
    except ProviderError as error:
        result = {"status": "failed", "error": error.to_dict()}
    result.update(provider=provider.describe(), tries=caller.records, spent_usd=caller.guard.spent_usd,
                  wall_seconds=round(time.monotonic() - started, 3), network_calls=len(caller.records))
    out.mkdir(parents=True, exist_ok=True)
    (out / "smoke.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def make_plan(args) -> dict:
    episodes = []
    for directory in args.episodes.split(","):
        for manifest in sorted(Path(directory).glob("*/public/manifest.json")):
            episode_dir = manifest.parent.parent
            episodes.append({"episode_ref": episode_dir.as_posix(), "family_id": episode_dir.name, "split": args.split})
    arms = _json(args.arms)
    return {"schema_version": "1.0", "artifact_id": args.artifact_id or f"experiment-{int(time.time())}",
            "protocol_id": args.protocol, "purpose": args.purpose, "episodes": episodes,
            "arms": arms["arms"], "repetitions": args.repetitions, "transport": arms.get("transport", "in_process"),
            "budget": arms.get("budget", {}), "analysis": arms.get("analysis", {}), "env_seed": 0,
            "note": "family_id defaults to the episode id; the independent protocol supplies real economic families."}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m auditlab.research.agents")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("preflight"); p.add_argument("--provider", required=True)
    p = sub.add_parser("smoke"); p.add_argument("--provider", required=True); p.add_argument("--budget-usd", type=float, default=0.0)
    p.add_argument("--confirm-paid", action="store_true"); p.add_argument("--out", required=True)
    p = sub.add_parser("plan"); p.add_argument("--episodes", required=True); p.add_argument("--split", required=True)
    p.add_argument("--arms", required=True); p.add_argument("--protocol", required=True, choices=sorted(PROTOCOLS))
    p.add_argument("--repetitions", type=int, default=3); p.add_argument("--purpose", default="selection",
                                                                        choices=("selection", "final"))
    p.add_argument("--artifact-id"); p.add_argument("--out", required=True)
    p = sub.add_parser("dry-run"); p.add_argument("--plan", required=True)
    p = sub.add_parser("run"); p.add_argument("--plan", required=True); p.add_argument("--out", required=True)
    p.add_argument("--confirm-paid", action="store_true")
    p = sub.add_parser("summarize"); p.add_argument("--plan", required=True); p.add_argument("--out", required=True)
    p = sub.add_parser("export"); p.add_argument("--out", required=True); p.add_argument("--target", required=True)
    p = sub.add_parser("export-review"); p.add_argument("--plan", required=True); p.add_argument("--out", required=True)
    p.add_argument("--target", required=True)
    args = parser.parse_args(argv)

    if args.command == "preflight":
        print(json.dumps(preflight(_json(args.provider)), indent=2))
        return 0
    if args.command == "smoke":
        result = smoke(_json(args.provider), args.budget_usd, args.confirm_paid, Path(args.out))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["status"] == "passed" else 2 if result["status"] == "not_checked" else 1
    if args.command == "plan":
        plan = make_plan(args)
        experiment.validate_plan(plan)
        Path(args.out).write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"{args.out}: {len(plan['episodes'])} episodes x {len(plan['arms'])} arms x {plan['repetitions']}")
        return 0
    if args.command == "export":
        print(experiment.export_attempts(args.out, args.target))
        return 0
    plan, sha = experiment.load_plan(args.plan)
    if args.command == "dry-run":
        report = experiment.dry_run(plan, sha)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    if args.command == "export-review":
        print(json.dumps(experiment.export_for_review(args.out, args.target, plan)))
        return 0
    if args.command == "summarize":
        summary = experiment.summarize(args.out, plan, sha)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0
    try:
        summary = experiment.run_experiment(plan, sha, args.out, confirm_paid=args.confirm_paid)
    except experiment.PlanError as error:
        print(json.dumps({"status": "refused", "reason": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"attempts": summary["attempts_total"], "is_experiment": summary["is_experiment"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
