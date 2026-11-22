"""Mutation testing of the official evaluator in isolated copies (protocol independent-assurance-2.0, section 5).

    python review/agent_generalization/mutation_testing.py EPISODES_DIR WORK_DIR

EPISODES_DIR holds generator episodes (public/ + private/truth.json), e.g. the metamorphic study's
`episodes/`. For every mutant a fresh copy of auditlab/ (+ judge_adapter.py, fixtures, schemas and
tests/evaluator) is made under WORK_DIR, one deliberate defect is written into the COPY, and two
suites run in a subprocess whose import path is the copy only:

* battery: automatic combinations of wrong answers on real truths, scored by the copy's
  evaluator.service.handle under the Genesis rules;
* project: the project's own tests/evaluator.

A mutant is killed when any battery result (tp/fp/fn/reward/status/matches) differs from the
unmutated copy's result. The expected value of every case also comes from the reviewer's own
reference scorer, so the unmutated evaluator is checked too. The working tree is never modified.
"""
from __future__ import annotations

import argparse, copy, json, shutil, subprocess, sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from review.genesis_acceptance import reference  # noqa: E402
from review.agent_generalization.validator import available_keys  # noqa: E402

M = "auditlab/evaluator/matcher.py"
S = "auditlab/evaluator/schema.py"
MUTANTS = {
    "M1_currency": (M, '            and finding["currency"] == issue["currency"]\n', ''),
    "M2_amount": (M, '            and abs(finding["claimed_amount_minor"] - issue["expected_amount_minor"])\n            <= _tolerance(tolerance_minor, issue["type"]))',
                  '            and True)'),
    "M3_tolerance_plus_one": (M, '<= _tolerance(tolerance_minor, issue["type"]))', '<= _tolerance(tolerance_minor, issue["type"]) + 1)'),
    "M4_affected_ids": (M, '            and _canon_ids(finding["affected_ids"]) == _canon_ids(issue["affected_ids"])\n', ''),
    "M5_type": (M, '    return (finding["type"] == issue["type"]\n', '    return (True\n'),
    "M6_ids_order": (M, '    return tuple(sorted(set(ids)))', '    return tuple(ids)'),
    "M7_evidence_coverage": (M, '    return any(set(group) <= evidence for group in issue["evidence_sets"])', '    return True'),
    "M8_evidence_availability": (M, '        missing = available_evidence is not None and any(key not in available_evidence for key in evidence)',
                                 '        missing = False'),
    "M9_duplicates_as_tp": (M, '    tp = len(matches)\n', '    tp = sum(1 for edges in adjacency if edges)\n'),
    "M10_greedy_matching": (M, '    chosen = _lexmin_matching(candidates)  # {issue: finding}; issues are sorted by issue_id\n',
                            '    chosen, _used = {}, set()\n    for _j, _fs in enumerate(candidates):\n        for _f in sorted(_fs):\n'
                            '            if _f not in _used:\n                chosen[_j] = _f; _used.add(_f); break\n'),
    "M11_empty_clean_f1_zero": (M, '    return Fraction(1) if denominator == 0 else Fraction(2 * tp, denominator)',
                                '    return Fraction(0) if denominator == 0 else Fraction(2 * tp, denominator)'),
    "M12_no_inspection_cost": (M, '    raw = 2 * _f1(tp, fp, fn) - 1 - INSPECTION_COST * inspection_steps', '    raw = 2 * _f1(tp, fp, fn) - 1'),
    "M13_no_reward_clip": (M, '    return max(Fraction(-1), min(Fraction(1), raw))', '    return min(Fraction(1), raw)'),
    "M14_episode_id": (S, '    if episode_id is not None and submission["episode_id"] != episode_id:', '    if False:'),
    # controls added to exercise the equivalence handling (not part of the pre-registered M1-M14)
    "X1_legacy_helper_unused": (M, '    return [chosen.get(right) for right in range(right_count)]', '    return [chosen.get(right) for right in reversed(range(right_count))]'),
    "X2_same_constant": (M, 'INSPECTION_COST = Fraction(2, 1000)', 'INSPECTION_COST = Fraction(1, 500)'),
    # exploratory mutants added after the protocol, to find gaps of the battery (reported separately)
    "Y1_dict_tolerance_default": (M, '        return tolerance_minor.get(issue_type, 0)', '        return tolerance_minor.get(issue_type, 1)'),
    "Y2_unmatched_reason": (M, '            unmatched[index] = "duplicate"', '            unmatched[index] = "evidence_incomplete"'),
    "Y3_tie_break_largest_index": (M, '        for finding in sorted(findings):', '        for finding in sorted(findings, reverse=True):'),
    "Y4_evidence_limit_plus_one": (S, '"bad_evidence_ids")) > MAX_EVIDENCE:', '"bad_evidence_ids")) > MAX_EVIDENCE + 1:'),
    "Y5_explanation_limit_plus_one": (S, 'len(explanation) > MAX_EXPLANATION:', 'len(explanation) > MAX_EXPLANATION + 1:'),
    "Y6_findings_limit_plus_one": (S, '    if len(findings) > MAX_FINDINGS:', '    if len(findings) > MAX_FINDINGS + 1:'),
}
EQUIVALENCE_PROOFS = {
    "X1_legacy_helper_unused": "evaluate() and service.handle() never call _max_matching (only _lexmin_matching); the copy's call graph is checked with grep below",
    "X2_same_constant": "Fraction(1, 500) == Fraction(2, 1000) exactly; checked by evaluation",
}

RUNNER = r'''
import json, sys
from auditlab.evaluator.service import handle
cases = json.load(open(sys.argv[1], encoding="utf-8"))
out = []
for c in cases:
    try:
        r = handle({"private_dir": c["private_dir"], "public_dir": c["public_dir"], "submission": c["submission"],
                    "inspection_steps": c["steps"], "rules": "genesis-2026-11-20"})
        s = r["score"]
        out.append({"status": r["status"], "tp": s["tp"], "fp": s["fp"], "fn": s["fn"], "reward": round(s["reward"], 9),
                    "matches": [list(m) for m in r["matches"]]})
    except Exception as e:
        out.append({"status": "error", "error": type(e).__name__ + ": " + str(e)[:120]})
json.dump(out, open(sys.argv[2], "w", encoding="utf-8"))
'''


# ------------------------------------------------------------------ battery of wrong answers
def _tolerance(public: Path, issue_type: str) -> int:
    t = json.loads((public / "policy.json").read_text(encoding="utf-8")).get("matching_tolerance_minor", 0)
    return t.get(issue_type, 0) if isinstance(t, dict) else int(t)


def _oracle(issue: dict) -> dict:
    ev = min(issue["evidence_sets"], key=lambda g: (len(g), sorted(g)))
    return {"type": issue["type"], "affected_ids": list(issue["affected_ids"]), "claimed_amount_minor": issue["expected_amount_minor"],
            "currency": issue["currency"], "evidence_ids": sorted(ev), "explanation": "battery"}


def perturbations(f: dict, keys: set, tau: int, other_currency: str) -> dict:
    unrelated = sorted(k for k in keys if k.startswith("ledger/") and k not in f["evidence_ids"])[:1]
    p = {"exact": f,
         "currency": {**f, "currency": other_currency},
         "amount_plus_1": {**f, "claimed_amount_minor": f["claimed_amount_minor"] + 1},
         "amount_minus_1": {**f, "claimed_amount_minor": max(0, f["claimed_amount_minor"] - 1)},
         "amount_plus_tau": {**f, "claimed_amount_minor": f["claimed_amount_minor"] + tau},
         "amount_plus_tau_plus_1": {**f, "claimed_amount_minor": f["claimed_amount_minor"] + tau + 1},
         "evidence_drop_one": {**f, "evidence_ids": f["evidence_ids"][1:]},
         "evidence_unavailable_extra": {**f, "evidence_ids": f["evidence_ids"] + ["ledger/RNOSUCHROW000000000000000"]},
         "evidence_available_extra": {**f, "evidence_ids": f["evidence_ids"] + unrelated},
         "affected_reversed": {**f, "affected_ids": list(reversed(f["affected_ids"]))},
         "affected_extra": {**f, "affected_ids": f["affected_ids"] + ["invoice/INOSUCHINVOICE0000000000"]},
         "wrong_type": {**f, "type": {"payment_allocation": "duplicate_invoice"}.get(f["type"], "payment_allocation")}}
    if len(f["affected_ids"]) > 1:
        p["affected_drop_one"] = {**f, "affected_ids": f["affected_ids"][1:]}
    return p


def battery(episodes: list[Path]) -> list[dict]:
    cases = []
    for ep in episodes:
        public, private = ep / "public", ep / "private"
        truth = json.loads((private / "truth.json").read_text(encoding="utf-8"))
        keys = available_keys(public)
        cur = json.loads((public / "manifest.json").read_text(encoding="utf-8"))["currency_exponents"]
        eid = truth["episode_id"]
        base = dict(private_dir=str(private), public_dir=str(public), episode=ep.name)
        add = lambda name, findings, steps=10, episode_id=eid: cases.append({**base, "case": name, "steps": steps,
                                                                            "submission": {"episode_id": episode_id, "findings": findings}})
        oracle = [_oracle(i) for i in truth["issues"]]
        add("empty", [], steps=0); add("empty_10_steps", [], steps=10); add("oracle", oracle); add("oracle_100_steps", oracle, steps=100)
        add("wrong_episode_id", oracle, episode_id="E" + "0" * 32)
        add("oracle_duplicated", oracle + oracle[:1])
        fp = {"type": "period_cutoff", "affected_ids": ["invoice/INOSUCHINVOICE0000000000"], "claimed_amount_minor": 1, "currency": next(iter(cur)),
              "evidence_ids": ["policy/period"], "explanation": "battery"}
        add("oracle_plus_fabricated", oracle + [fp]); add("only_fabricated_100_steps", [fp], steps=100)
        per_issue = []
        for k, i in enumerate(truth["issues"]):
            other = next((c for c in sorted(cur) if c != i["currency"]), "XXX")
            ps = perturbations(_oracle(i), keys, _tolerance(public, i["type"]), other)
            per_issue.append(ps)
            for name, f in ps.items():
                add(f"issue{k}:{name}", [f if j == k else o for j, o in enumerate(oracle)])
        if len(per_issue) >= 2:  # pairs of perturbations on two different errors
            few = ("exact", "currency", "amount_plus_1", "evidence_drop_one", "affected_reversed")
            for a in few:
                for b in few:
                    add(f"pair:{a}+{b}", [per_issue[0][a], per_issue[1][b]] + oracle[2:])
        if truth["issues"]:  # synthetic ambiguity: two identical causes, nested evidence sets (greedy vs maximum)
            i0 = truth["issues"][0]
            ev = sorted(min(i0["evidence_sets"], key=len))
            extra = sorted(k for k in keys if k.startswith("ledger/") and k not in ev)[:1]
            if len(ev) >= 1 and extra:
                syn = copy.deepcopy(truth)
                syn["issues"] = [{**i0, "issue_id": "XA", "evidence_sets": [ev]}, {**i0, "issue_id": "XB", "evidence_sets": [ev + extra]}]
                sdir = ep.parent.parent / "synthetic" / ep.name / "private"
                sdir.mkdir(parents=True, exist_ok=True)
                (sdir / "truth.json").write_text(json.dumps(syn), encoding="utf-8")
                f_big, f_small = {**_oracle(i0), "evidence_ids": ev + extra}, {**_oracle(i0), "evidence_ids": ev}
                cases.append({**base, "private_dir": str(sdir), "case": "synthetic_nested_sets", "steps": 5,
                              "submission": {"episode_id": eid, "findings": [f_big, f_small]}})
    return cases


def expected(case: dict) -> dict:
    """The reviewer's own reference scorer for one case (independent of auditlab.evaluator)."""
    truth = json.loads((Path(case["private_dir"]) / "truth.json").read_text(encoding="utf-8"))
    if case["submission"]["episode_id"] != truth["episode_id"]:
        return {"status": "invalid_submission", "tp": 0, "fp": 0, "fn": len(truth["issues"]), "reward": -1.0}
    tau = None
    public = Path(case["public_dir"])
    taus = {i["type"]: _tolerance(public, i["type"]) for i in truth["issues"]}
    tau = max(taus.values()) if taus else 0  # per-type tolerances: the battery's truths use a single type per tau check
    r = reference.score(case["submission"]["findings"], truth["issues"], case["steps"], available_keys(public), tau)
    if not r["valid"]:
        return {"status": "invalid_submission", "tp": 0, "fp": 0, "fn": len(truth["issues"]), "reward": -1.0}
    return {"status": "scored", "tp": r["tp"], "fp": r["fp"], "fn": r["fn"], "reward": round(float(r["return"]), 9)}


# ------------------------------------------------------------------ isolated copies
def make_copy(dest: Path, mutant: str | None) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    ignore = shutil.ignore_patterns("__pycache__", "static", "*.pyc")
    shutil.copytree(ROOT / "auditlab", dest / "auditlab", ignore=ignore)
    for name in ("fixtures", "schemas"):
        if (ROOT / name).is_dir():
            shutil.copytree(ROOT / name, dest / name, ignore=ignore)
    shutil.copy2(ROOT / "judge_adapter.py", dest / "judge_adapter.py")
    (dest / "tests").mkdir()
    shutil.copytree(ROOT / "tests" / "evaluator", dest / "tests" / "evaluator", ignore=ignore)
    shutil.copytree(ROOT / "tests" / "runner" / "golden", dest / "tests" / "runner" / "golden", ignore=ignore)
    if mutant:
        rel, old, new = MUTANTS[mutant]
        target = dest / rel
        text = target.read_text(encoding="utf-8")
        if text.count(old) != 1:
            raise RuntimeError(f"{mutant}: anchor not found exactly once in {rel}")
        target.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
    return dest


def run_copy(copy_dir: Path, cases_path: Path, out_path: Path) -> dict:
    import os
    env = {**os.environ, "PYTHONPATH": str(copy_dir), "PYTHONHASHSEED": "0", "PYTHONIOENCODING": "utf-8"}
    (copy_dir / "battery_runner.py").write_text(RUNNER, encoding="utf-8")
    b = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(copy_dir)!r}); exec(open({str(copy_dir / 'battery_runner.py')!r}).read())",
                        str(cases_path), str(out_path)], cwd=copy_dir, capture_output=True, text=True, env=env, timeout=900)
    t = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests/evaluator"], cwd=copy_dir,
                       capture_output=True, text=True, env=env, timeout=900)
    tail = (t.stderr or "").strip().splitlines()
    return {"battery_rc": b.returncode, "battery_stderr": b.stderr[-400:], "project_tests_rc": t.returncode,
            "project_tests_summary": [l for l in tail if l.startswith(("Ran ", "OK", "FAILED"))]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("episodes", type=Path); ap.add_argument("work", type=Path)
    a = ap.parse_args(argv)
    a.episodes, a.work = a.episodes.resolve(), a.work.resolve()
    eps = sorted(p for p in a.episodes.iterdir() if (p / "private" / "truth.json").is_file())
    a.work.mkdir(parents=True, exist_ok=True)
    cases = battery(eps)
    cases_path = a.work / "cases.json"
    cases_path.write_text(json.dumps(cases), encoding="utf-8")
    exp = [expected(c) for c in cases]
    # unmutated copy = baseline
    base_dir = make_copy(a.work / "copies" / "original", None)
    info = {"original": run_copy(base_dir, cases_path, a.work / "original.json")}
    if info["original"]["battery_rc"] != 0:
        raise SystemExit("unmutated copy failed: " + info["original"]["battery_stderr"])
    base = json.loads((a.work / "original.json").read_text(encoding="utf-8"))
    agree = [k for k, (b, e) in enumerate(zip(base, exp)) if all(b.get(x) == e[x] for x in ("status", "tp", "fp", "fn", "reward"))]
    disagreements = [{"case": cases[k]["case"], "episode": cases[k]["episode"], "official": base[k], "reference": exp[k]}
                     for k in range(len(cases)) if k not in set(agree)]
    results = {}
    for name in MUTANTS:
        d = make_copy(a.work / "copies" / name, name)
        run = run_copy(d, cases_path, a.work / f"{name}.json")
        got = json.loads((a.work / f"{name}.json").read_text(encoding="utf-8")) if (a.work / f"{name}.json").is_file() else []
        killing = [cases[k]["case"] for k, (g, b) in enumerate(zip(got, base)) if g != b] if len(got) == len(base) else ["battery did not run"]
        project_killed = run["project_tests_rc"] != 0
        status = "killed" if killing else ("equivalent_with_proof" if name in EQUIVALENCE_PROOFS else "survived")
        results[name] = {"status": status, "battery_killing_cases": len(killing), "examples": sorted(set(c.split(":")[-1] for c in killing))[:6],
                         "project_tests_killed": project_killed, "project_tests": run["project_tests_summary"],
                         "proof": EQUIVALENCE_PROOFS.get(name)}
        shutil.rmtree(d, ignore_errors=True)
    calls = subprocess.run(["git", "grep", "-n", "_max_matching(", "--", "auditlab"], cwd=ROOT, capture_output=True, text=True).stdout
    report = {"protocol_id": "independent-assurance-2.0", "cases": len(cases), "episodes": len(eps),
              "original_vs_reference": {"agree": len(agree), "total": len(cases), "disagreements": disagreements[:20]},
              "original_project_tests": info["original"]["project_tests_summary"], "mutants": results,
              "x1_call_sites_of_max_matching": calls.strip().splitlines(),
              "working_tree_untouched": subprocess.run(["git", "diff", "--quiet", "--", "auditlab"], cwd=ROOT).returncode == 0}
    (a.work / "mutation-report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps({k: (v["status"], v["battery_killing_cases"], v["project_tests_killed"]) for k, v in results.items()}, indent=1))
    print("original vs reference:", report["original_vs_reference"]["agree"], "/", len(cases), "untouched:", report["working_tree_untouched"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
