"""Reward vectors 1-20, open examples M01-M12 and extra negative examples.

Three layers per item:
  expectation  - the published value, re-derived independently (formula / reference scorer)
  core         - AuditLab's internal evaluator (auditlab.evaluator.matcher.evaluate)
  official     - the jury interface score_episode() of judge_adapter.py (not_checked if absent)
"""
from __future__ import annotations

import copy
import json
from fractions import Fraction as Fr
from pathlib import Path

from . import fixtures as fx
from . import reference as ref

EPS = 1e-9
F = lambda **o: {**{"type": "period_cutoff", "affected_ids": ["invoice/I1"], "claimed_amount_minor": 10000, "currency": "KZT",
                    "evidence_ids": ["invoices/DI1", "ledger/R1"], "explanation": "Issued 2026-11-30, recognised 2026-12-01."}, **o}
WRONG = F(affected_ids=["invoice/I3"], claimed_amount_minor=30000, evidence_ids=["invoices/DI3", "ledger/R5"])  # valid, false
FA_B = F()
TYPES = ["duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff"]


def V(tp, fp, fn, p, r, f1, ret):
    return {"tp": tp, "fp": fp, "fn": fn, "precision": Fr(p), "recall": Fr(r), "f1": Fr(f1), "return": Fr(ret)}


INVALID = {"invalid": True}
# (id, published text, truth key, findings, inspection_steps, published expectation)
REWARD = [
    ("R01", "one correct F", "A", [F()], 10, V(1, 0, 0, 1, 1, 1, "0.98")),
    ("R02", "empty findings", "A", [], 10, V(0, 0, 1, 0, 0, 0, -1)),
    ("R03", "empty truth, empty findings", "-", [], 10, V(0, 0, 0, 0, 0, 1, "0.98")),
    ("R04", "empty truth, one false finding", "-", [F()], 10, V(0, 1, 0, 0, 0, 0, -1)),
    ("R05", "two identical F", "A", [F(), F()], 10, V(1, 1, 0, Fr(1, 2), 1, Fr(2, 3), Fr(47, 150))),
    ("R06", "wrong type", "A", [F(type="duplicate_invoice")], 10, V(0, 1, 1, 0, 0, 0, -1)),
    ("R07", "other existing, unaffected record", "A", [F(affected_ids=["invoice/I3"])], 10, V(0, 1, 1, 0, 0, 0, -1)),
    ("R08", "amount 10001, tau 0", "A", [F(claimed_amount_minor=10001)], 10, V(0, 1, 1, 0, 0, 0, -1)),
    ("R09", "other currency", "A", [F(currency="USD")], 10, V(0, 1, 1, 0, 0, 0, -1)),
    ("R10", "only D1", "A", [F(evidence_ids=["invoices/DI1"])], 10, V(0, 1, 1, 0, 0, 0, -1)),
    ("R11", "missing evidence id instead of D2", "A", [F(evidence_ids=["invoices/DI1", "ledger/R404"])], 10, V(0, 1, 1, 0, 0, 0, -1)),
    ("R12", "5 records x 4 types = 20 findings", "A",
     [F(type=t, affected_ids=[f"invoice/I{n}"]) for n in range(1, 6) for t in TYPES], 10, V(1, 19, 0, Fr(1, 20), 1, Fr(2, 21), Fr(-871, 1050))),
    ("R13", "A and B; only F for A", "AB", [F()], 10, V(1, 0, 1, 1, Fr(1, 2), Fr(2, 3), Fr(47, 150))),
    ("R14", "A and B; F for A plus a false finding", "AB", [F(), WRONG], 10, V(1, 1, 1, Fr(1, 2), Fr(1, 2), Fr(1, 2), "-0.02")),
    ("R15", "inspection_steps=0", "A", [F()], 0, V(1, 0, 0, 1, 1, 1, 1)),
    ("R16", "inspection_steps=50", "A", [F()], 50, V(1, 0, 0, 1, 1, 1, "0.90")),
    ("R17", "inspection_steps=100", "A", [F()], 100, V(1, 0, 0, 1, 1, 1, "0.80")),
    ("R18", "31 findings", "A", [F()] + [copy.deepcopy(WRONG) for _ in range(30)], 10, INVALID),
    ("R19", "11 distinct evidence ids", "A", [F(evidence_ids=["invoices/DI1", "ledger/R1"] + [f"ledger/R{k}" for k in range(2, 11)])], 10, INVALID),
    ("R20", "explanation of 1001 ASCII characters", "A", [F(explanation="a" * 1001)], 10, INVALID),
]

M = lambda **o: {**{"type": "payment_allocation", "affected_ids": ["payment/P1", "invoice/I1"], "claimed_amount_minor": 10000,
                    "currency": "KZT", "evidence_ids": ["payments/DP1", "ledger/R4"], "explanation": "10000 KZT never applied."}, **o}
TP1, FPFN = (1, 0, 0), (0, 1, 1)
# (id, text, findings, tau, expectation (tp,fp,fn) | "invalid")
MCASES = [
    ("M01", "affected_ids=[R2,R1,R1]", [M(affected_ids=["invoice/I1", "payment/P1", "payment/P1"])], 0, TP1),
    ("M02", "affected_ids=[R1,R2,R3]", [M(affected_ids=["payment/P1", "invoice/I1", "invoice/I2"])], 0, FPFN),
    ("M03", "evidence=[D2,D1,D3] (D3 available)", [M(evidence_ids=["ledger/R4", "payments/DP1", "invoices/DI1"])], 0, TP1),
    ("M04", "evidence=[D1,D2,U]", [M(evidence_ids=["payments/DP1", "ledger/R4", "ledger/R404"])], 0, FPFN),
    ("M05", "11 evidence ids, 10 repeat D1", [M(evidence_ids=["payments/DP1"] * 10 + ["ledger/R4"])], 0, "invalid"),
    ("M06a", "currency 'kzt'", [M(currency="kzt")], 0, FPFN),
    ("M06b", "currency 'KZT '", [M(currency="KZT ")], 0, FPFN),
    ("M07", "amount 10001, tau 0", [M(claimed_amount_minor=10001)], 0, FPFN),
    ("M08a", "tau=1, amount 10001", [M(claimed_amount_minor=10001)], 1, TP1),
    ("M08b", "tau=1, amount 10002", [M(claimed_amount_minor=10002)], 1, FPFN),
    ("M09a", "amount '10000'", [M(claimed_amount_minor="10000")], 0, "invalid"),
    ("M09b", "amount true", [M(claimed_amount_minor=True)], 0, "invalid"),
    ("M12", "two identical correct findings", [M(), M()], 0, (1, 1, 0)),
]
EXTRA = [  # requested additional negative examples
    ("X01", "available extra evidence (D3) beside the full set", [M(evidence_ids=["payments/DP1", "ledger/R4", "invoices/DI1"])], 0, TP1),
    ("X02", "unavailable extra evidence beside the full set", [M(evidence_ids=["payments/DP1", "ledger/R4", "payments/DOES_NOT_EXIST"])], 0, FPFN),
    ("X03", "31 identical findings (limit before de-duplication)", [M() for _ in range(31)], 0, "invalid"),
    ("X04a", "affected id with different case", [M(affected_ids=["Payment/P1", "invoice/I1"])], 0, FPFN),
    ("X04b", "affected id with trailing space", [M(affected_ids=["payment/P1 ", "invoice/I1"])], 0, FPFN),
    ("X04c", "evidence id with trailing space", [M(evidence_ids=["payments/DP1", "ledger/R4 "])], 0, FPFN),
    ("X05", "amount 10000.0 (fraction/float)", [M(claimed_amount_minor=10000.0)], 0, "invalid"),
    ("X06a", "explanation 1000 emoji code points", [M(explanation="\U0001F600" * 1000)], 0, TP1),
    ("X06b", "explanation 1001 Cyrillic code points", [M(explanation="ж" * 1001)], 0, "invalid"),
    ("X06c", "explanation 500 x (e + combining acute) = 1000 code points", [M(explanation="é" * 500)], 0, TP1),
    ("X06d", "explanation 1001 code points with combining marks", [M(explanation="é" * 500 + "x")], 0, "invalid"),
    ("X07", "unknown string type", [M(type="not_a_known_type")], 0, FPFN),
    ("X08", "missing required field (explanation)", [{k: v for k, v in M().items() if k != "explanation"}], 0, "invalid"),
]


def _close(a, b):
    return a is not None and b is not None and abs(float(a) - float(b)) <= EPS


def _core(evaluate, truth, findings, steps, tau):
    ev = evaluate({"episode_id": truth["episode_id"], "findings": copy.deepcopy(findings)}, truth, steps, tolerance_minor=tau)
    s = ev.score
    return {"status": ev.status, "reason": ev.reason, "tp": s["tp"], "fp": s["fp"], "fn": s["fn"], "precision": s["precision"],
            "recall": s["recall"], "f1": s["f1"], "return": s["reward"]}


def _compare(got, exp, full):
    """got: core/official dict; exp: V(...) or INVALID; full -> compare P/R/F1 too."""
    if "invalid" in exp:
        return got.get("status") == "invalid_submission" or got.get("valid") is False or (got.get("tp") is None and _close(got.get("return"), -1))
    if got.get("status") == "invalid_submission":
        return False
    ok = (got["tp"], got["fp"], got["fn"]) == (exp["tp"], exp["fp"], exp["fn"]) and _close(got["return"], exp["return"])
    if full:
        ok = ok and all(_close(got[k], exp[k]) for k in ("precision", "recall", "f1"))
    return ok


def run(work: Path, policy: dict, evaluate, max_matching, adapter) -> list[dict]:
    out = []
    gr_a = fx.reward_fixture(work / "GR", policy, with_b=False)
    gr_ab = fx.reward_fixture(work / "GR-B", policy, with_b=True)
    gm = fx.m_fixture(work / "GM", policy)
    empty = json.loads((gr_a / "private" / "truth.json").read_text(encoding="utf-8")) | {"issues": []}
    truths = {"A": json.loads((gr_a / "private" / "truth.json").read_text(encoding="utf-8")),
              "AB": json.loads((gr_ab / "private" / "truth.json").read_text(encoding="utf-8")), "-": empty}
    dirs = {"A": gr_a, "AB": gr_ab, "-": gr_a}
    refs_gr = fx.public_refs(gr_a / "public"); refs_gm = fx.public_refs(gm / "public")
    assert "ledger/R404" not in refs_gr | refs_gm and "payments/DOES_NOT_EXIST" not in refs_gm  # U chosen after reading data
    for rid, text, tkey, findings, steps, exp in REWARD:
        truth = truths[tkey]
        # expectation layer: published value vs formula-only derivation vs independent reference scorer
        refscore = ref.score(findings, truth["issues"], steps, refs_gr)
        derived_ok = True
        if "invalid" not in exp:
            derived_ok = ref.reward_from(exp["tp"], exp["fp"], exp["fn"], steps) == exp["return"]
        reference_ok = _compare(refscore, exp, True)
        core = _core(evaluate, truth, findings, steps, 0)
        core_ok = _compare(core, exp, True)
        official = _official(adapter, dirs[tkey] if tkey != "-" else None, truth, findings, steps, exp, work, rid)
        out.append({"id": rid, "block": "reward", "expectation_source": "fintech-checks.txt, block 3, test " + rid[1:],
                    "stimulus": f"{text}; inspection_steps={steps}", "expected": _plain(exp),
                    "expectation_checks": {"published_reward_equals_formula": derived_ok, "reference_scorer_agrees": reference_ok},
                    "core": {"result": _plain(core), "status": "passed" if core_ok else "failed"},
                    "official": official, "status": official["status"]})
    gm_truth = json.loads((gm / "private" / "truth.json").read_text(encoding="utf-8"))
    for block, cases in (("m-examples", MCASES), ("extra-negative", EXTRA)):
        for cid, text, findings, tau, exp in cases:
            expv = INVALID if exp == "invalid" else {"tp": exp[0], "fp": exp[1], "fn": exp[2],
                                                     "return": ref.reward_from(exp[0], exp[1], exp[2], 10)}
            refscore = ref.score(findings, gm_truth["issues"], 10, refs_gm, tau)
            core = _core(evaluate, gm_truth, findings, 10, tau)
            core_ok = _compare(core, expv, False)
            official = _official(adapter, gm if tau == 0 else None, gm_truth, findings, 10, expv, work, cid,
                                 note=None if tau == 0 else "a published per-type tau mechanism is needed for the jury interface")
            out.append({"id": cid, "block": block, "expectation_source": "fintech-checks.txt, " + ("M-examples" if block == "m-examples" else "task brief, matcher rules 1-8"),
                        "stimulus": text + (f"; tau={tau}" if tau else ""), "expected": _plain(expv),
                        "expectation_checks": {"reference_scorer_agrees": _compare(refscore, expv, False)},
                        "core": {"result": _plain(core), "status": "passed" if core_ok else "failed"},
                        "official": official, "status": official["status"]})
    # M10/M11 on the graph level (the definitions give the edge graph directly)
    for cid, adj, expected_owner, text in (("M10", [[0, 1], [0]], [1, 0], "F0 fits A and B, F1 fits only A: expect F1->A, F0->B"),
                                           ("M11", [[0, 1], [0, 1]], [0, 1], "F0 and F1 fit both: expect A->F0, B->F1")):
        issues = [{"issue_id": "A"}, {"issue_id": "B"}]
        pairs = _ref_graph(adj, issues)
        ref_owner = [dict((j, i) for j, i in pairs)[k] for k in ("A", "B")]
        owner = list(max_matching(adj, 2))
        perm = list(max_matching([adj[1], adj[0]], 2))  # permuted findings: TP must not change
        core_ok = owner == expected_owner and sum(o is not None for o in perm) == 2
        out.append({"id": cid, "block": "m-examples", "expectation_source": "fintech-checks.txt, M-examples + tie-break rule",
                    "stimulus": text + " (graph level; AuditLab validate_truth rejects two issues with equal type+affected_ids, so this graph cannot be built from a valid AuditLab truth)",
                    "expected": {"owners_A_B": expected_owner, "tp": 2},
                    "expectation_checks": {"reference_scorer_agrees": ref_owner == expected_owner},
                    "core": {"result": {"owners_A_B": owner, "tp": sum(o is not None for o in owner), "tp_after_permutation": sum(o is not None for o in perm)},
                             "status": "passed" if core_ok else "failed"},
                    "official": {"status": "not_checked", "reason": "graph-level example; not expressible through score_episode with a valid AuditLab truth"},
                    "status": "not_checked"})
    return out


def _ref_graph(adj, issues):
    order = sorted(range(len(issues)), key=lambda j: issues[j]["issue_id"])

    def best(fl, fr):
        owner = {}
        def aug(u, seen):
            for v in adj[u]:
                if v in fr or v in seen: continue
                seen.add(v)
                if v not in owner or aug(owner[v], seen):
                    owner[v] = u; return True
            return False
        return sum(aug(u, set()) for u in range(len(adj)) if u not in fl)
    target, chosen, ul, ur = best(set(), set()), [], set(), set()
    for j in order:
        for i in range(len(adj)):
            if i not in ul and j in adj[i] and len(chosen) + 1 + best(ul | {i}, ur | {j}) == target:
                chosen.append((issues[j]["issue_id"], i)); ul.add(i); ur.add(j); break
    return chosen


def _official(adapter, episode_dir, truth, findings, steps, exp, work, cid, note=None):
    if adapter is None:
        return {"status": "not_checked", "reason": "judge_adapter.py is not importable at this commit"}
    if episode_dir is None:
        return {"status": "not_checked", "reason": note or "no episode directory for this case"}
    d = Path(episode_dir)
    if not truth["issues"]:
        d = work / f"empty-{cid}"
        if not d.exists():
            import shutil
            shutil.copytree(episode_dir, d)
            (d / "private" / "truth.json").write_text(json.dumps(truth), encoding="utf-8")
    try:
        got = adapter.score_episode(str(d), {"episode_id": truth["episode_id"], "findings": copy.deepcopy(findings)}, steps)
    except Exception as error:  # noqa: BLE001
        return {"status": "failed", "reason": f"score_episode raised {type(error).__name__}: {error}"}
    keys = {"tp", "fp", "fn", "precision", "recall", "f1", "return"}
    if not isinstance(got, dict) or set(got) < keys:
        return {"status": "failed", "result": got, "reason": "score_episode result lacks the published keys"}
    if "invalid" in exp:
        ok = _close(got["return"], -1) and all(got[k] is None for k in keys - {"return"})
    else:
        ok = (got["tp"], got["fp"], got["fn"]) == (exp["tp"], exp["fp"], exp["fn"]) and _close(got["return"], exp["return"]) and \
             all(_close(got[k], exp[k]) for k in ("precision", "recall", "f1") if k in exp)
    return {"status": "passed" if ok else "failed", "result": got}


def _plain(d):
    return {k: (str(v) if isinstance(v, Fr) else v) for k, v in d.items() if k != "pairs"}
