"""Independent recomputation of Claude Pro 1's experiments (protocol independent-assurance-2.0, section 6).

    python review/agent_generalization/recompute.py OUT.json

Scores every saved attempt again with the reviewer's own reference scorer, resolves each snapshot to
an economic family with the reviewer's canonical histories (renamed/reordered copies collapse into
their family), and reports, per arm and separately: completion, service outcomes, quality on
completed attempts (with per-finding labels), clean-company accusations, the official-by-rule value of
every attempt, family-level values and paired differences with their limits. The publication's own
numbers are compared with the recomputed ones.
"""
from __future__ import annotations

import argparse, hashlib, json, statistics, subprocess, sys, tempfile
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from review.genesis_acceptance import reference  # noqa: E402
from review.agent_generalization.canonical import whole_history  # noqa: E402
from review.agent_generalization.validator import available_keys  # noqa: E402
from review.agent_generalization import compare  # noqa: E402

SERVICE = {"provider_error", "rate_limited", "quota_exhausted", "bridge_failure", "interrupted", "budget_stop",
           "infra_error_before_first_action"}


def public_hash(public: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(public.iterdir()):
        if f.is_file():
            h.update(f.name.encode()); h.update(f.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()


class Episodes:
    """Known episodes (public + private/truth.json) and their economic families."""

    def __init__(self, dirs: list[Path]):
        self.by_hash, self.info = {}, {}
        for d in dirs:
            if (d / "public" / "manifest.json").is_file() and (d / "private" / "truth.json").is_file():
                self.by_hash[public_hash(d / "public")] = d
                self.info[d] = {"public_history": whole_history(d / "public"),
                                "source_history": whole_history(d / "private" / "source_book") if (d / "private" / "source_book").is_dir() else None,
                                "truth": json.loads((d / "private" / "truth.json").read_text(encoding="utf-8"))}

    def family(self, d: Path) -> str:
        i = self.info[d]
        if i["source_history"]:
            return i["source_history"][:16]
        if not i["truth"]["issues"]:
            return i["public_history"][:16]
        # an error copy without its own source book: the family of a known episode with the same (ID-free) history
        for other, j in self.info.items():
            if j["public_history"] == i["public_history"] and j["source_history"]:
                return j["source_history"][:16]
        return "unresolved:" + i["public_history"][:12]

    def resolve(self, public: Path):
        d = self.by_hash.get(public_hash(public))
        if d is not None:
            return d
        hist = whole_history(public)  # a renamed copy: same history, different IDs -> truth would need the ID map
        same = [x for x, i in self.info.items() if i["public_history"] == hist]
        return ("history_only", same) if same else None


def commercial(public: Path) -> set[str]:
    from review.agent_generalization.canonical import chains
    return {c["digest"] for c in chains(public).values() if c["canon"]["invoices"] or c["canon"]["payments"]}


def effective_families(rows: list[dict], threshold: float = 0.5) -> dict:
    """Merge families whose snapshots share at least `threshold` of their commercial counterparty chains:
    one economic origin with different defects or IDs. Returns {family: effective family}."""
    reps = {}
    for r in rows:
        if r.get("resolved"):
            reps.setdefault(r["family"], r["public"])
    chain = {f: commercial(p) for f, p in reps.items()}
    parent = {f: f for f in chain}
    def find(x):
        while parent[x] != x: x = parent[x]
        return x
    fams = sorted(chain)
    merged = []
    for i, a in enumerate(fams):
        for b in fams[i + 1:]:
            shared = len(chain[a] & chain[b])
            if shared >= threshold * min(len(chain[a]), len(chain[b])):
                parent[find(b)] = find(a); merged.append([a, b, shared, len(chain[a]), len(chain[b])])
    return {f: find(f) for f in fams}, merged


def label_findings(findings, truth_issues, keys) -> list[str]:
    """Per-finding diagnosis (TP or the first field that is wrong)."""
    out = []
    for f in findings:
        if not truth_issues:
            out.append("accusation_on_clean"); continue
        if any(reference.edge(f, i, keys) for i in truth_issues):
            out.append("tp_candidate"); continue
        same = [i for i in truth_issues if i["type"] == f["type"] and set(i["affected_ids"]) == set(f["affected_ids"])]
        if not same:
            out.append("wrong_type_or_entities"); continue
        i = same[0]
        if f["currency"] != i["currency"]: out.append("wrong_currency")
        elif f["claimed_amount_minor"] != i["expected_amount_minor"]: out.append("wrong_amount")
        elif not set(f["evidence_ids"]) <= keys: out.append("unavailable_evidence")
        else: out.append("incomplete_evidence")
    return out


def row_from_console(path: Path, console_root: Path) -> dict:
    a = json.loads(path.read_text(encoding="utf-8"))
    steps = a.get("steps") or []
    inspections = sum(1 for s in steps if (s.get("action") or {}).get("action") != "submit")
    return {"attempt_id": a["artifact_id"], "arm": a["variant_id"], "status": a["status"], "public": console_root / a["snapshot_ref"],
            "submission": a.get("submission"), "inspection_steps": inspections, "published": a.get("evaluation"),
            "declared_family": (a.get("episode") or {}).get("family_id"), "elapsed_s": (a.get("usage") or {}).get("elapsed_s")}


def row_from_compare(path: Path, experiment: dict) -> dict:
    a = json.loads(path.read_text(encoding="utf-8"))
    ep = experiment["episodes"][a["episode_key"]]
    return {"attempt_id": a["attempt_id"], "arm": a["arm"], "status": a["status"], "public": ROOT / ep["episode_dir"] / "public",
            "submission": a.get("final_answer"), "inspection_steps": a.get("inspection_steps") or 0, "published": None,
            "declared_family": ep["family_id"], "elapsed_s": a.get("wall_time_s")}


def score_rows(rows: list[dict], eps: Episodes) -> list[dict]:
    out = []
    for r in rows:
        res = eps.resolve(r["public"])
        if not isinstance(res, Path):
            out.append({**r, "resolved": False, "reason": "snapshot not matched to a known episode" if res is None else "renamed copy without ID map"}); continue
        truth = eps.info[res]["truth"]; keys = available_keys(r["public"])
        variant = "clean" if not truth["issues"] else "error"
        completed = r["status"] == "completed" and r["submission"] is not None
        findings = (r["submission"] or {}).get("findings", []) if completed else []
        sc = reference.score(findings, truth["issues"], r["inspection_steps"], keys)
        out.append({**r, "resolved": True, "episode": res.name, "family": eps.family(res), "variant": variant, "completed": completed,
                    "service_outcome": r["status"] in SERVICE, "score": sc, "labels": label_findings(findings, truth["issues"], keys) if completed else [],
                    "by_rule_reward": sc["return"]})
    return out


def summarize(scored: list[dict]) -> dict:
    arms = {}
    for r in scored:
        arms.setdefault(r["arm"], []).append(r)
    out = {}
    for arm, rs in sorted(arms.items()):
        ok = [r for r in rs if r.get("resolved")]
        comp = [r for r in ok if r["completed"]]
        tp = sum(r["score"]["tp"] or 0 for r in comp); fp = sum(r["score"]["fp"] or 0 for r in comp); fn = sum(r["score"]["fn"] or 0 for r in comp)
        clean_comp = [r for r in comp if r["variant"] == "clean"]
        labels = {}
        for r in comp:
            for l in r["labels"]: labels[l] = labels.get(l, 0) + 1
        fams = {}
        for r in ok:
            fams.setdefault(r.get("family_effective", r["family"]), []).append(float(r["by_rule_reward"]))
        fam_completed = {}
        for r in comp:
            fam_completed.setdefault(r.get("family_effective", r["family"]), []).append(float(r["score"]["return"]))
        status = {}
        for r in rs: status[r["status"]] = status.get(r["status"], 0) + 1
        out[arm] = {"attempts": len(rs), "unresolved": len(rs) - len(ok), "status": status,
                    "completed": {"n": len(comp), "of": len(ok)},
                    "service_outcomes": sum(1 for r in ok if r["service_outcome"]),
                    "quality_on_completed": {"tp": tp, "fp": fp, "fn": fn, "micro_f1": float(Fraction(2 * tp, 2 * tp + fp + fn)) if 2 * tp + fp + fn else None,
                                             "mean_reward": statistics.fmean(float(r["score"]["return"]) for r in comp) if comp else None,
                                             "finding_labels": labels},
                    "clean_completed_with_accusation": {"n": sum(1 for r in clean_comp if r["score"]["fp"]), "of": len(clean_comp)},
                    "by_rule_all_attempts": {"mean_reward": statistics.fmean(float(r["by_rule_reward"]) for r in ok) if ok else None, "n": len(ok),
                                             "note": "service failures scored as empty submissions, as the official rule does"},
                    "families": {"n": len(fams), "values_by_rule": {f: statistics.fmean(v) for f, v in sorted(fams.items())},
                                 "values_completed_only": {f: statistics.fmean(v) for f, v in sorted(fam_completed.items())}}}
    return out


def pairs(summary: dict, comparisons: list[tuple[str, str]]) -> list[dict]:
    res = []
    for a, b in comparisons:
        if a not in summary or b not in summary:
            continue
        for kind in ("values_by_rule", "values_completed_only"):
            p = compare.paired(summary[a]["families"][kind], summary[b]["families"][kind], resamples=10000)
            fams = p["families"]
            claim = "not_shown" if fams < 2 else ("improvement_claim_allowed" if fams >= 10 and p["ci95"] and (p["ci95"][0] > 0 or p["ci95"][1] < 0) else "no_claim")
            res.append({"a": a, "b": b, "basis": kind, **{k: p.get(k) for k in ("families", "difference", "ci95", "p_value", "reason")}, "claim": claim})
    return res


def gemini(work: Path) -> dict:
    """Live Gemini pilot (docs/research/gemini): regenerate the pair from the published seed and config, match bytes."""
    from auditlab.generator import generate_episode
    root = ROOT / "docs/research/gemini/evidence/console"
    eps = []
    for name, count in (("allocation", 1), ("clean", 0)):
        d = work / "gemini-episodes" / name
        if not d.exists():
            generate_episode(922101, {"config_version": "1.1", "base_currency": "KZT", "currencies": ["KZT"], "allocation_mode": "partial",
                                      "issue_counts": {"payment_allocation": count}}, d)
        eps.append(d)
    rows = [row_from_console(p, root) for p in sorted((root / "attempts").glob("*.json"))]
    scored = score_rows(rows, Episodes(eps))
    published = json.loads((root / "comparison.json").read_text(encoding="utf-8"))
    return {"source": "docs/research/gemini/evidence (main 988a5db)", "rows": scored, "published_comparison": published}


def llm_baselines() -> dict:
    base = ROOT / "docs/research/llm-auditor/evidence/2026-11-21-baselines-open-dev/review-export"
    exp = json.loads((base / "experiment.json").read_text(encoding="utf-8"))
    rows = [row_from_compare(p, exp) for p in sorted((base / "attempts").glob("*.json"))]
    eps = Episodes(sorted({(ROOT / e["episode_dir"]) for e in exp["episodes"].values()}))
    return {"source": "docs/research/llm-auditor/evidence/2026-11-21-baselines-open-dev (main 988a5db)", "rows": score_rows(rows, eps),
            "comparisons": [(c["a"], c["b"]) for c in exp["comparisons"]]}


def pr52(work: Path, ref: str = "pr52") -> dict | None:
    """PR #52 replication-set baselines, read from the PR head with git (not merged into this branch)."""
    tree = work / "pr52"
    if not tree.exists():
        try:
            sha = subprocess.run(["git", "rev-parse", ref], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        except subprocess.CalledProcessError:
            return None
        tree.mkdir(parents=True)
        archive = subprocess.run(["git", "archive", "--format=tar", sha, "docs/research/agent-improvement-v2"], cwd=ROOT, capture_output=True, check=True).stdout
        import tarfile, io
        tarfile.open(fileobj=io.BytesIO(archive)).extractall(tree)
        (tree / "SHA").write_text(sha)
    base = tree / "docs/research/agent-improvement-v2"
    sets = base / "replication-922101"
    eps = Episodes(sorted(p for p in sets.iterdir() if p.is_dir()))
    root = base / "evidence/console"
    rows = [row_from_console(p, root) for p in sorted((root / "attempts").glob("*.json"))]
    return {"source": f"PR #52 head {(tree / 'SHA').read_text()}", "rows": score_rows(rows, eps),
            "families_of_conditions": {p.name: eps.family(p) for p in sorted(eps.info)},
            "published_comparison": json.loads((root / "comparison.json").read_text(encoding="utf-8"))}


def clean_rows(rows):
    keep = ("attempt_id", "arm", "status", "episode", "family", "family_effective", "variant", "completed", "service_outcome", "inspection_steps", "labels",
            "declared_family", "resolved", "reason", "published")
    out = []
    for r in rows:
        x = {k: r.get(k) for k in keep if k in r}
        if "score" in r:
            x["score"] = {k: (str(v) if isinstance(v, Fraction) else v) for k, v in r["score"].items() if k != "pairs"}
        out.append(x)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("out", type=Path); ap.add_argument("--work", type=Path, default=None)
    a = ap.parse_args(argv)
    work = (a.work or Path(tempfile.mkdtemp(prefix="recompute-"))).resolve()
    report = {"protocol_id": "independent-assurance-2.0", "sources": {}}
    for name, fn in (("gemini_live_pilot", lambda: gemini(work)), ("llm_auditor_baselines", llm_baselines), ("pr52_replication_baselines", lambda: pr52(work))):
        src = fn()
        if src is None:
            report["sources"][name] = {"status": "not_checked", "reason": "source not available"}; continue
        eff, merged = effective_families(src["rows"])
        for r in src["rows"]:
            if r.get("resolved"):
                r["family_effective"] = eff[r["family"]]
        declared = {r["declared_family"] for r in src["rows"] if r.get("declared_family")}
        summ = summarize(src["rows"])
        comps = src.get("comparisons") or [(x, y) for x in summ for y in summ if x < y]
        report["sources"][name] = {"source": src["source"], "summary": summ,
                                   "family_audit": {"declared_families": len(declared), "resolved_families": len({r["family"] for r in src["rows"] if r.get("resolved")}),
                                                    "effective_families": len(set(eff.values())), "merged": merged}, "paired": pairs(summ, comps), "attempts": clean_rows(src["rows"]),
                                   **{k: src[k] for k in ("families_of_conditions",) if k in src}}
        if "published_comparison" in src:
            report["sources"][name]["published_comparison_excerpt"] = {arm: {k: v for k, v in body.items() if k in ("micro_f1", "mean_reward", "status", "fp_on_clean", "completion")}
                                                                       for arm, body in src["published_comparison"].get("arms", {}).items()}
    a.out.write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    for name, s in report["sources"].items():
        print("==", name)
        for arm, v in s.get("summary", {}).items():
            print(f"  {arm}: attempts {v['attempts']} completed {v['completed']} service {v['service_outcomes']} quality {v['quality_on_completed']['tp']}/{v['quality_on_completed']['fp']}/{v['quality_on_completed']['fn']} "
                  f"clean_accused {v['clean_completed_with_accusation']} by_rule {v['by_rule_all_attempts']['mean_reward']} families {v['families']['n']}")
        for p in s.get("paired", []):
            print("  pair", p["a"], "-", p["b"], p["basis"], p["families"], p["difference"], p["ci95"], p["claim"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
