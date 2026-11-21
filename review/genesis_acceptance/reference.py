"""Independent reference scorer for the Genesis FinTech matcher conventions (fintech-checks.txt,
"Точные соглашения matcher"). Written for this review; it does not import auditlab.evaluator.

Used as a second oracle: the published expected values are checked against it, and the
project evaluator is compared with both. Exact rational arithmetic (Fraction).
"""
from __future__ import annotations

from fractions import Fraction

FINDING_KEYS = {"type", "affected_ids", "claimed_amount_minor", "currency", "evidence_ids", "explanation"}
MAX_FINDINGS, MAX_EVIDENCE, MAX_EXPLANATION = 30, 10, 1000
STEP_COST = Fraction(2, 1000)


class Invalid(Exception):
    pass


def check_structure(findings) -> None:
    """Rule 7/8: raw sizes and value types before any set normalisation."""
    if not isinstance(findings, list):
        raise Invalid("findings_not_list")
    if len(findings) > MAX_FINDINGS:
        raise Invalid("too_many_findings")
    for f in findings:
        if not isinstance(f, dict) or not FINDING_KEYS <= set(f):
            raise Invalid("missing_field")
        if not isinstance(f["type"], str):
            raise Invalid("type_not_string")
        if not isinstance(f["affected_ids"], list) or not all(isinstance(x, str) for x in f["affected_ids"]):
            raise Invalid("affected_ids")
        amount = f["claimed_amount_minor"]
        if type(amount) is not int:  # bool, str, float are rejected
            raise Invalid("amount_type")
        if not isinstance(f["currency"], str):
            raise Invalid("currency_type")
        ev = f["evidence_ids"]
        if not isinstance(ev, list) or not all(isinstance(x, str) for x in ev):
            raise Invalid("evidence_ids")
        if len(ev) > MAX_EVIDENCE:  # raw length, before removing repeats
            raise Invalid("too_many_evidence_ids")
        if not isinstance(f["explanation"], str) or len(f["explanation"]) > MAX_EXPLANATION:  # code points
            raise Invalid("explanation")


def edge(finding, issue, available_refs, tau=0) -> bool:
    """All TP conditions (rules 1-6): exact strings, sets, tau, full evidence set, every ref available."""
    if finding["type"] != issue["type"] or finding["currency"] != issue["currency"]:
        return False
    if set(finding["affected_ids"]) != set(issue["affected_ids"]):
        return False
    if abs(finding["claimed_amount_minor"] - issue["expected_amount_minor"]) > tau:
        return False
    evidence = set(finding["evidence_ids"])
    if available_refs is not None and not evidence <= set(available_refs):
        return False  # a nonexistent or unavailable evidence id makes the finding unmatched
    return any(set(group) <= evidence for group in issue["evidence_sets"])


def _max_size(adj, n_left, fixed_left, fixed_right):
    owner = {}

    def augment(u, seen):
        for v in adj[u]:
            if v in fixed_right or v in seen:
                continue
            seen.add(v)
            if v not in owner or augment(owner[v], seen):
                owner[v] = u
                return True
        return False

    return sum(augment(u, set()) for u in range(n_left) if u not in fixed_left)


def matching(findings, issues, available_refs=None, tau=0):
    """Maximum matching; ties broken by the official lexicographic rule over (issue_id, finding index)."""
    order = sorted(range(len(issues)), key=lambda j: issues[j]["issue_id"])
    adj = [[j for j in range(len(issues)) if edge(f, issues[j], available_refs, tau)] for f in findings]
    best = _max_size(adj, len(findings), set(), set())
    chosen, used_left, used_right = [], set(), set()
    for j in order:  # smallest issue_id first, then smallest finding index, keeping a maximum matching
        for i in range(len(findings)):
            if i in used_left or j not in adj[i]:
                continue
            rest = _max_size(adj, len(findings), used_left | {i}, used_right | {j})
            if len(chosen) + 1 + rest == best:
                chosen.append((issues[j]["issue_id"], i)); used_left.add(i); used_right.add(j)
                break
    assert len(chosen) == best
    return chosen


def score(submission_findings, truth_issues, inspection_steps, available_refs=None, tau=0) -> dict:
    try:
        check_structure(submission_findings)
    except Invalid as error:
        return {"valid": False, "reason": str(error), "tp": None, "fp": None, "fn": None, "precision": None,
                "recall": None, "f1": None, "return": Fraction(-1)}
    pairs = matching(submission_findings, truth_issues, available_refs, tau)
    tp = len(pairs); fp = len(submission_findings) - tp; fn = len(truth_issues) - tp
    precision = Fraction(tp, tp + fp) if tp + fp else Fraction(0)
    recall = Fraction(tp, tp + fn) if tp + fn else Fraction(0)
    f1 = Fraction(1) if tp + fp + fn == 0 else Fraction(2 * tp, 2 * tp + fp + fn)
    ret = max(Fraction(-1), min(Fraction(1), 2 * f1 - 1 - STEP_COST * inspection_steps))
    return {"valid": True, "tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1,
            "return": ret, "pairs": pairs}


def reward_from(tp, fp, fn, steps) -> Fraction:
    """Formula only (for deriving the published reward vectors without any matcher)."""
    f1 = Fraction(1) if tp + fp + fn == 0 else Fraction(2 * tp, 2 * tp + fp + fn)
    return max(Fraction(-1), min(Fraction(1), 2 * f1 - 1 - STEP_COST * steps))
