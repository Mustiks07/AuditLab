"""compare_experiment(experiment_ref) -> comparison  (CONTRACTS.md, Claude Pro 2).

Every attempt is re-scored by the reviewer's reference scorer (official fintech-checks rules,
evidence availability against the episode's public files, tau from policy) from the saved final
answer and the trusted truth. Nothing is taken from the attempt's own score fields.

experiment_ref directory:
  experiment.json   {"protocol_id", "comparisons": [{"name", "a", "b"}], "episodes": {episode_key: {"family_id", "variant", "episode_dir"}}}
  attempts/*.json   {"attempt_id", "arm", "episode_key", "repetition", "status", "final_answer" (findings object or null),
                     "inspection_steps", "actions", "input_tokens", "output_tokens", "wall_time_s", "cost_usd", "live": bool}
status: completed | timeout | provider_error | invalid_response | budget_stop | infra_error_before_first_action
"""
from __future__ import annotations

import json, random, statistics
from collections import defaultdict
from fractions import Fraction
from pathlib import Path

from review.genesis_acceptance import reference as ref
from review.genesis_acceptance.fixtures import public_refs

OPERATIONAL = ("completed", "timeout", "provider_error", "invalid_response", "budget_stop", "infra_error_before_first_action")


def rescore(attempt: dict, episode: dict, cache: dict) -> dict:
    epdir = Path(episode["episode_dir"])
    if epdir not in cache:
        pol = json.loads((epdir / "public" / "policy.json").read_text(encoding="utf-8"))
        cache[epdir] = (json.loads((epdir / "private" / "truth.json").read_text(encoding="utf-8")), public_refs(epdir / "public"),
                        pol.get("matching_tolerance_minor", 0))
    truth, refs, tau = cache[epdir]
    steps = int(attempt.get("inspection_steps") or 0)
    status = attempt["status"]
    if status == "infra_error_before_first_action":
        return {"excluded": True}
    answer = attempt.get("final_answer")
    if status in ("timeout", "budget_stop") or answer is None and status != "invalid_response":
        findings = []                                   # official: truncated -> empty findings
    elif status == "invalid_response":
        return {"excluded": False, "valid": False, "tp": None, "fp": None, "fn": None, "return": Fraction(-1)}
    else:
        if not isinstance(answer, dict) or answer.get("episode_id") != truth["episode_id"] or set(answer) != {"episode_id", "findings"}:
            return {"excluded": False, "valid": False, "tp": None, "fp": None, "fn": None, "return": Fraction(-1)}
        findings = answer["findings"]
    if isinstance(tau, dict):
        tau = 0 if not tau else max(tau.values())       # per-type tau: conservative single value for the reference scorer
    s = ref.score(findings, truth["issues"], steps, refs, int(tau))
    return {"excluded": False, **s, "n_findings": len(findings) if isinstance(findings, list) else None}


def _summary(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"n": 0, "mean": None, "median": None, "p90": None}
    ys = sorted(xs)
    return {"n": len(xs), "mean": statistics.fmean(xs), "median": statistics.median(xs), "p90": ys[min(len(ys) - 1, int(0.9 * len(ys)))]}


def metrics(rows: list[dict]) -> dict:
    n = len(rows)
    scored = [r for r in rows if not r["score"]["excluded"]]
    valid = [r for r in scored if r["score"].get("valid")]
    tp = sum(r["score"]["tp"] for r in valid); fp = sum(r["score"]["fp"] for r in valid); fn = sum(r["score"]["fn"] for r in valid)
    fn += sum(r["truth_issues"] for r in scored if not r["score"].get("valid"))   # an invalid submit finds nothing
    clean = [r for r in scored if r["variant"] == "clean"]
    clean_accused = [r for r in clean if r["score"].get("valid") is False or (r["score"].get("n_findings") or 0) > 0]
    return {"attempts": n, "scored": len(scored), "excluded_infra": n - len(scored),
            "micro_f1": {"value": float(Fraction(2 * tp, 2 * tp + fp + fn)) if 2 * tp + fp + fn else 1.0, "tp": tp, "fp": fp, "fn": fn},
            "mean_reward": {"value": float(sum(r["score"]["return"] for r in scored) / len(scored)) if scored else None, "n": len(scored)},
            "fp_on_clean": {"attempts_with_findings": len(clean_accused), "clean_attempts": len(clean),
                            "fp_findings": sum(r["score"]["fp"] or 0 for r in clean if r["score"].get("valid"))},
            "status": {s: sum(r["status"] == s for r in rows) for s in OPERATIONAL},
            "completion": {"submitted": sum(r["status"] == "completed" for r in rows), "attempts": n},
            "actions": _summary([r.get("actions") for r in rows]), "input_tokens": _summary([r.get("input_tokens") for r in rows]),
            "output_tokens": _summary([r.get("output_tokens") for r in rows]), "wall_time_s": _summary([r.get("wall_time_s") for r in rows]),
            "cost_usd": _summary([r.get("cost_usd") for r in rows]), "live_attempts": sum(bool(r.get("live")) for r in rows)}


def family_values(rows: list[dict]) -> dict:
    """Family value = mean reward over its episodes and repetitions (repeats are not independent companies)."""
    by = defaultdict(list)
    for r in rows:
        if not r["score"]["excluded"]:
            by[r["family_id"]].append(float(r["score"]["return"]))
    return {f: statistics.fmean(v) for f, v in by.items()}


def paired(a: dict, b: dict, resamples=10000, seed=20261121) -> dict:
    fams = sorted(set(a) & set(b))
    if not fams:
        return {"families": 0, "difference": None, "ci95": None, "p_value": None}
    d = [a[f] - b[f] for f in fams]
    rng = random.Random(seed)
    boots = sorted(statistics.fmean(rng.choice(d) for _ in d) for _ in range(resamples))
    lo, hi = boots[int(0.025 * resamples)], boots[int(0.975 * resamples) - 1]
    obs = abs(statistics.fmean(d)); rng2 = random.Random(seed + 1)
    extreme = sum(abs(statistics.fmean(x * rng2.choice((1, -1)) for x in d)) >= obs - 1e-12 for _ in range(resamples))
    return {"families": len(fams), "difference": statistics.fmean(d), "ci95": [lo, hi], "p_value": (extreme + 1) / (resamples + 1),
            "missing_in_a": sorted(set(b) - set(a)), "missing_in_b": sorted(set(a) - set(b)),
            "shown": not (lo <= 0 <= hi)}


def compare_experiment(experiment_ref, resamples=10000) -> dict:
    exp_dir = Path(experiment_ref)
    exp = json.loads((exp_dir / "experiment.json").read_text(encoding="utf-8"))
    cache, rows = {}, []
    for p in sorted((exp_dir / "attempts").glob("*.json")):
        a = json.loads(p.read_text(encoding="utf-8"))
        ep = exp["episodes"][a["episode_key"]]
        truth_n = len(json.loads((Path(ep["episode_dir"]) / "private" / "truth.json").read_text(encoding="utf-8"))["issues"])
        rows.append({**a, "family_id": ep["family_id"], "variant": ep["variant"], "truth_issues": truth_n, "score": rescore(a, ep, cache)})
    arms = sorted({r["arm"] for r in rows})
    per_arm = {arm: metrics([r for r in rows if r["arm"] == arm]) for arm in arms}
    fam = {arm: family_values([r for r in rows if r["arm"] == arm]) for arm in arms}
    comps = {c["name"]: {"a": c["a"], "b": c["b"], **paired(fam.get(c["a"], {}), fam.get(c["b"], {}), resamples)} for c in exp.get("comparisons", [])}
    return {"schema_version": "1.0", "artifact_id": f"comparison-{exp_dir.name}", "protocol_id": exp.get("protocol_id"),
            "source_sha": exp.get("source_sha"), "arms": per_arm, "paired_by_family": comps,
            "families": len({r["family_id"] for r in rows}), "episodes": len({r["episode_key"] for r in rows}), "attempts": len(rows),
            "note": "Rescored by the reviewer's reference scorer; not an official score. live=false attempts are recordings or local policies, not live model calls."}
