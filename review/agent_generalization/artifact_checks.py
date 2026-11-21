"""Acceptance checks for failure / reduction / curriculum / training artifacts and for result claims.

Each check returns {"status": passed|failed|not_checked, ...}. Failure kinds are recomputed by the
reviewer from the saved answers and truth; the artifact's own labels are only compared, never trusted.
"""
from __future__ import annotations

import re
from collections import Counter

from review.genesis_acceptance import reference as ref

PERSISTENT = 2  # same failure kind in >= 2 of 3 repetitions (protocol)


def failure_kinds(attempt: dict, truth: dict, refs=None) -> set[str]:
    st = attempt.get("status")
    if st in ("timeout", "budget_stop"):
        return {"timeout"}
    if st == "invalid_response":
        return {"invalid_submit"}
    findings = (attempt.get("final_answer") or {}).get("findings", [])
    s = ref.score(findings, truth["issues"], int(attempt.get("inspection_steps") or 0), refs)
    if not s["valid"]:
        return {"invalid_submit"}
    kinds = set()
    if not truth["issues"] and findings:
        kinds.add("FP_on_clean")
    matched = {i for i, _ in s.get("pairs", [])}
    for issue in truth["issues"]:
        if issue["issue_id"] in matched:
            continue
        near = [f for f in findings if f["type"] == issue["type"] and set(f["affected_ids"]) == set(issue["affected_ids"])]
        if near and any(f["claimed_amount_minor"] != issue["expected_amount_minor"] or f["currency"] != issue["currency"] for f in near):
            kinds.add("wrong_amount_currency_or_entities")
        elif near:
            kinds.add("incomplete_evidence")
        else:
            kinds.add("FN")
    if truth["issues"] and s["fp"] and not kinds:
        kinds.add("extra_FP")
    return kinds


def check_failure(failure: dict, attempts: list[dict], truth: dict, refs=None) -> dict:
    """The declared persistent failure must hold on ALL listed repetitions, recomputed independently."""
    if not attempts:
        return {"status": "not_checked", "reason": "no attempts"}
    declared = failure.get("kind")
    listed = {a["attempt_id"] for a in attempts}
    omitted = sorted(set(failure.get("all_attempt_ids", listed)) - listed)
    per = [failure_kinds(a, truth, refs) for a in attempts]
    hits = sum(declared in k for k in per)
    ok = not omitted and len(attempts) >= 3 and hits >= PERSISTENT
    return {"status": "passed" if ok else "failed", "declared": declared, "hits": hits, "repetitions": len(attempts),
            "recomputed": [sorted(k) for k in per], "omitted_attempts": omitted}


def check_reduction(reduction: dict, validation_after: dict, before: list[dict], after: list[dict], truth_before: dict, truth_after: dict) -> dict:
    kind = reduction.get("failure_kind")
    same_defect = Counter(i["type"] for i in truth_before["issues"]) == Counter(i["type"] for i in truth_after["issues"])
    hits_b = sum(kind in failure_kinds(a, truth_before) for a in before)
    hits_a = sum(kind in failure_kinds(a, truth_after) for a in after)
    ok = validation_after.get("status") == "passed" and same_defect and hits_b >= PERSISTENT and hits_a >= PERSISTENT and len(after) >= 3
    return {"status": "passed" if ok else "failed", "validated": validation_after.get("status"), "same_defect_types": same_defect,
            "hits_before": hits_b, "hits_after": hits_a, "repetitions_after": len(after)}


def check_curriculum(curriculum_histories: dict[str, str], curriculum_chains: dict[str, set[str]],
                     forbidden_histories: set[str], forbidden_chains: set[str]) -> dict:
    """curriculum_*: example_id -> canonical whole history / defect-chain digests (canonical.py), computed on the trusted side."""
    dup = [h for h, n in Counter(curriculum_histories.values()).items() if n > 1]
    leak_whole = sorted(e for e, h in curriculum_histories.items() if h in forbidden_histories)
    leak_chain = sorted(e for e, cs in curriculum_chains.items() if cs & forbidden_chains)
    ok = not dup and not leak_whole and not leak_chain
    return {"status": "passed" if ok else "failed", "renamed_or_identical_copies": len(dup),
            "examples_sharing_whole_history_with_dev_or_holdout": leak_whole, "examples_sharing_defect_chain_with_dev_or_holdout": leak_chain}


def check_training(training: dict) -> dict:
    kind = training.get("kind")
    if kind != "weights":
        return {"status": "failed" if training.get("claimed_as") == "training" else "not_checked",
                "reason": f"kind={kind!r} is not weight training (prompt/memory/few-shot are evaluated separately)"}
    has_job = bool(training.get("job_id")) and bool(training.get("checkpoint_id") or training.get("result_model"))
    mock = bool(training.get("mock")) or str(training.get("provider", "")).lower() in ("mock", "fake", "test_double")
    ok = has_job and not mock and training.get("status") == "succeeded"
    return {"status": "passed" if ok else "failed", "job_identity": has_job, "mock": mock, "job_status": training.get("status")}


CLAIM_RULES = [
    ("official_score", re.compile(r"(official (score|result|ranking)|официальн\w* (балл|результат|рейтинг))", re.I),
     re.compile(r"(not (an )?official|не (является )?официальн|неофициальн)", re.I)),
    ("recording_as_live", re.compile(r"\b(live run|живой запуск|новый запуск)\b", re.I), re.compile(r"(not a live|not live|не (является )?(живым|новым)|не живой|не новый|только когда|only when)", re.I)),
    ("unproven_improvement", re.compile(r"(improv\w+ (proven|confirmed|shown)|улучшение (доказано|подтверждено|показано))", re.I),
     re.compile(r"(not (shown|proven)|не (показан|доказан|подтвержд))", re.I)),
    ("mock_as_measurement", re.compile(r"\bmock\w*\b.*\b(measur\w+|измерен\w+)", re.I), re.compile(r"(not a measurement|не (является )?измерен)", re.I)),
]


def check_claims(texts: dict[str, str]) -> dict:
    """Flag sentences that state an official score, a recording as live, an unproven improvement or a mock as a measurement,
    unless the same sentence negates it. Heuristic; every hit needs a human look."""
    hits = []
    for name, text in texts.items():
        for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
            for rule, claim, negation in CLAIM_RULES:
                if claim.search(sentence) and not negation.search(sentence):
                    hits.append({"source": name, "rule": rule, "sentence": sentence.strip()[:200]})
    return {"status": "passed" if not hits else "failed", "hits": hits}
