"""Local Genesis acceptance (independent review, Claude Pro 2). NOT an official score.

    python review/genesis_acceptance/run.py --output artifacts/genesis-acceptance [--adapter judge_adapter.py] [--skip-slow]
    python review/genesis_acceptance/run.py --output DIR --phase answers   # only generate + freeze the independent answers

Statuses: passed / failed / not_checked. Exit code 0 when the run completed (failures are findings,
not crashes); 2 if the harness itself could not run.
"""
from __future__ import annotations

import argparse, importlib.util, json, platform, subprocess, sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def load_adapter(path: Path):
    if not path.is_file():
        return None, f"{path.name} not present"
    spec = importlib.util.spec_from_file_location("judge_adapter", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as error:  # noqa: BLE001
        return None, f"import failed: {type(error).__name__}: {error}"
    missing = [n for n in ("generate_episode", "make_env", "score_episode") if not hasattr(module, n)]
    return (None, f"missing entry points {missing}") if missing else (module, "importable")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--adapter", type=Path, default=ROOT / "judge_adapter.py")
    ap.add_argument("--phase", choices=("answers", "all"), default="all")
    ap.add_argument("--skip-slow", action="store_true")
    args = ap.parse_args(argv)
    out = args.output; out.mkdir(parents=True, exist_ok=True)
    from auditlab.generator import generate_episode, GENERATOR_VERSION
    from review.genesis_acceptance import data_checks
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--", "auditlab", "judge_adapter.py"], cwd=ROOT, capture_output=True, text=True).stdout.strip())
    meta = {"suite": "genesis-local-acceptance-1.0", "not_an_official_score": True, "commit": sha, "product_code_dirty": dirty,
            "generator_version": GENERATOR_VERSION, "python": platform.python_version(), "platform": platform.platform(),
            "definitions": {"url": "https://genesis-exe.com/docs/fintech-checks.txt",
                            "sha256": "2a597ebfb94daec700ba9b1b09d62c032366e788c6e992486d444e063be8d870", "retrieved_utc": "2026-11-21T03:52:46Z"},
            "started_utc": datetime.utcnow().isoformat() + "Z"}
    answers_path = out / "answers_before_truth.json"
    if answers_path.exists():
        meta["answers"] = {"path": str(answers_path), "sha256": data_checks.sha(answers_path), "reused": True}
    else:
        meta["answers"] = data_checks.phase_answers(generate_episode, out)   # written before any truth is opened
    if args.phase == "answers":
        (out / "answers.meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        print(json.dumps(meta["answers"], indent=1)); return 0

    from auditlab.environment import open_session
    from auditlab.evaluator.matcher import evaluate, _max_matching
    from auditlab.policies import make_policy
    from auditlab.runner import make_env
    from auditlab.verifier import verify_report
    from review.genesis_acceptance import contract_checks, matcher_checks, security_checks
    adapter, adapter_note = load_adapter(args.adapter)
    meta["adapter"] = {"path": str(args.adapter), "state": adapter_note}
    policy = json.loads((out / "episodes" / "D01" / "public" / "policy.json").read_text(encoding="utf-8"))
    items = []
    items += matcher_checks.run(out / "matcher", policy, evaluate, _max_matching, adapter) if not (out / "matcher").exists() else \
        matcher_checks.run(_fresh(out / "matcher"), policy, evaluate, _max_matching, adapter)
    team = {"C": data_checks.SCENARIOS["D01"][1], "E": data_checks.SCENARIOS["D12"][1],
            "L": data_checks.SCENARIOS["D02"][1]}
    items += contract_checks.run(adapter, _fresh(out / "contract"), team, args.skip_slow)
    data_items = data_checks.phase_compare(out, open_session, verify_report, evaluate)
    from review.genesis_acceptance.adapter_transport import data_transport
    transport = data_transport(adapter, out)
    for it in data_items:
        if it["id"] in transport:
            it["adapter_transport"] = "generate_episode via adapter byte-identical: " + transport[it["id"]]
            if transport[it["id"]] != "passed":
                it["status"] = "failed"
    items += data_items
    items += data_checks.phase_pairs(out, make_env, make_policy, open_session, evaluate, adapter)
    diagnostics = {
        "integrator_probe_reproduced": "see probe-results-reproduced.json in the review package",
        "closed_mode_leakage_internal": security_checks.closed_mode_leakage(make_env, make_policy, out / "episodes" / "D16", 987654321),
        "deadline_origin_internal": security_checks.deadline_origin(make_env, out / "episodes" / "D01"),
        "deadline_origin_adapter": security_checks.deadline_origin_adapter(adapter, out / "episodes" / "D01"),
        "reset_wall_time_max_size": security_checks.reset_wall_time(make_env, out / "episodes" / "D08"),
    }
    summary = {}
    for it in items:
        block = it["block"]; summary.setdefault(block, {"passed": 0, "failed": 0, "not_checked": 0})
        summary[block][it["status"]] += 1
    core = {}
    for it in items:
        if "core" in it:
            b = it["block"]; core.setdefault(b, {"passed": 0, "failed": 0}); core[b][it["core"]["status"]] += 1
    meta["finished_utc"] = datetime.utcnow().isoformat() + "Z"
    report = {"meta": meta, "summary_official_layer": summary, "summary_core_evaluator": core, "items": items, "diagnostics": diagnostics}
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"summary_official_layer": summary, "summary_core_evaluator": core, "adapter": adapter_note}, indent=1))
    return 0


def _fresh(path: Path) -> Path:
    import shutil
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


if __name__ == "__main__":
    raise SystemExit(main())
