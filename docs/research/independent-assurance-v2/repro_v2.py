"""Reproduce the counterexamples of the independent assurance v2 report (Claude Pro 2, #43).

    python docs/research/independent-assurance-v2/repro_v2.py OUT_DIR

Run from the repository root with the pinned lock (main 988a5db + this branch). Public data only.
C1  protocol 1.1 comparison counted HTTP 429 attempts as model misses (fixed here in compare.py)
C2  two published dev episodes of one company counted as two independent families
C3  duplicate + allocation: a fair clean control exists only with archive_copy and split_payment distractors
C4  evaluator test gap: the default tolerance for unlisted types (dict tolerance) is not pinned by any test
"""
import json, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
out = Path(sys.argv[1]).resolve(); out.mkdir(parents=True, exist_ok=True)
report = {}

# C1 -------------------------------------------------------------------------------------------------
published = json.loads((ROOT / "docs/research/gemini/evidence/comparison.json").read_text(encoding="utf-8"))["arms"]["gemini-memory"]
from review.agent_generalization import compare  # noqa: E402
rows = [{"status": "provider_error", "variant": v, "truth_issues": n,
         "score": {"tp": 0, "fp": 0, "fn": n, "valid": True, "return": r, "excluded": False, "n_findings": 0}}
        for v, n, r in (("error", 1, -1), ("clean", 0, 1))]
fixed = compare.metrics(rows)
report["C1_service_failure_as_miss"] = {"published_micro_f1_memory_arm": published["micro_f1"], "published_status": published["status"],
                                        "fixed_micro_f1_agent_ended": fixed["micro_f1"], "fixed_micro_f1_by_rule": fixed["micro_f1_by_rule"],
                                        "service_outcomes": fixed["service_outcomes"]}

# C2 -------------------------------------------------------------------------------------------------
from review.agent_generalization.canonical import chains  # noqa: E402
a, b = (ROOT / "datasets/open/dev" / e for e in ("E4C30409A2F4E6DFE858E8FF06B8C4436", "E509506A67D640282D2FD1D8B154EF4EC"))
pa, pb = (json.loads((x / "private/provenance.json").read_text(encoding="utf-8")) for x in (a, b))
com = lambda x: {c["digest"] for c in chains(x / "public").values() if c["canon"]["invoices"] or c["canon"]["payments"]}
report["C2_one_company_two_families"] = {"same_seed": pa["seed"] == pb["seed"],
                                         "config_keys_that_differ": sorted(k for k in set(pa["config"]) | set(pb["config"]) if pa["config"].get(k) != pb["config"].get(k)),
                                         "shared_commercial_chains": [len(com(a) & com(b)), len(com(a)), len(com(b))]}

# C3 -------------------------------------------------------------------------------------------------
from auditlab.generator import generate_episode  # noqa: E402
from review.agent_generalization.pairs import matched_clean_control  # noqa: E402
from review.agent_generalization.validator import clean_variant  # noqa: E402
Z = {"duplicate_invoice": 0, "payment_allocation": 0, "fx_conversion": 0, "period_cutoff": 0}
def cfg(d, **c):
    return {"config_version": "1.1", "company_size": {"ledger_rows_target": 400, "documents_target": 65}, "base_currency": "KZT",
            "currencies": ["KZT", "USD"], "timezone": "Asia/Almaty", "target_period": "2026-11", "interaction": None,
            "clean_distractors": d, "issue_counts": {**Z, **c}}
c3 = {}
for label, d in (("split_payment+refund", ["split_payment", "refund"]), ("archive_copy+split_payment", ["archive_copy", "split_payment"])):
    e, o = out / f"c3-{label}", out / f"c3-{label}-off"
    generate_episode(9231001, cfg(d, duplicate_invoice=1, payment_allocation=1), e)
    generate_episode(9231001, cfg(d), o)
    sb = clean_variant(e, out / f"c3-{label}-source-book")
    c3[label] = {"defect_off": matched_clean_control(e, o), "source_book": matched_clean_control(e, sb)}
report["C3_duplicate_allocation_controls"] = {k: {x: {"status": v[x]["status"], "differing": v[x]["differing_features"],
                                                      "same_history": v[x]["checks"]["same_commercial_history_as_source_book"]} for x in v} for k, v in c3.items()}

# C4 -------------------------------------------------------------------------------------------------
from review.agent_generalization.mutation_testing import make_copy  # noqa: E402
probe = ("import json\nfrom auditlab.evaluator.matcher import evaluate\n"
         "t={'episode_id':'E1','issues':[{'issue_id':'X1','type':'payment_allocation','affected_ids':['payment/P1','invoice/I1'],"
         "'expected_amount_minor':1000,'currency':'KZT','evidence_sets':[['payments/D1']]}]}\n"
         "s={'episode_id':'E1','findings':[{'type':'payment_allocation','affected_ids':['invoice/I1','payment/P1'],'claimed_amount_minor':1001,"
         "'currency':'KZT','evidence_ids':['payments/D1'],'explanation':'x'}]}\n"
         "print(json.dumps(evaluate(s,t,0,{'fx_conversion':5},rules='genesis-2026-11-20').score))\n")
c4 = {}
for label, mutant in (("original", None), ("Y1_default_1", "Y1_dict_tolerance_default")):
    d = make_copy(out / f"c4-{label}", mutant)
    run = subprocess.run([sys.executable, "-c", probe], cwd=d, capture_output=True, text=True, env={**__import__("os").environ, "PYTHONPATH": str(d)})
    tests = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests/evaluator"], cwd=d, capture_output=True, text=True,
                           env={**__import__("os").environ, "PYTHONPATH": str(d)})
    c4[label] = {"score": json.loads(run.stdout), "project_tests": tests.stderr.strip().splitlines()[-1]}
report["C4_tolerance_default_untested"] = c4
print(json.dumps(report, indent=1, default=str))
