"""Disaggregate objectives after a structurally valid, trusted evaluation."""
from auditlab.evaluator.evidence import PublicEvidence
from auditlab.evaluator.service import public_tolerance
from auditlab.release.transforms import read_json


OBJECTIVES = ("missed_issue", "clean_false_positive", "wrong_amount", "wrong_evidence")


def _cause(item):
    return item["type"], tuple(sorted(set(item["affected_ids"]))), item["currency"]


def assess_failures(snapshot, findings, evaluation):
    """Keep scorer FP/FN diagnostics, and attribute single-axis financial failures.

    A wrong amount must have sufficient available evidence. An evidence failure
    must have the right amount. Ambiguous and compound errors stay in the raw
    FP/FN diagnostics, without being credited to either targeted objective.
    """
    truth = read_json(snapshot / "private/truth.json")
    issues = {issue["issue_id"]: issue for issue in truth["issues"]}
    matched = {issue_id for _, issue_id in evaluation["matches"]}
    if not matched.issubset(issues):
        raise ValueError("Trusted evaluation does not match the snapshot")
    missed = [issue for issue_id, issue in issues.items() if issue_id not in matched]
    failures = [{"reason": "missed_issue", "type": issue["type"], "affected_ids": issue["affected_ids"],
                 "issue_id": issue["issue_id"]} for issue in missed]
    unmatched = evaluation["unmatched"]
    if not unmatched:
        return failures

    evidence = PublicEvidence(snapshot / "public") if missed else None
    tolerance = public_tolerance(snapshot / "public") if missed else 0
    for index, diagnostic in unmatched.items():
        finding = findings[int(index)]
        failures.append({"reason": "false_positive", "type": finding["type"],
                         "affected_ids": finding["affected_ids"], "diagnostic": diagnostic})
        if not issues:
            failures.append({"reason": "clean_false_positive", "type": finding["type"],
                             "affected_ids": finding["affected_ids"], "diagnostic": diagnostic})
            continue
        candidates = [issue for issue in missed if _cause(issue) == _cause(finding)]
        if len(candidates) != 1:
            continue
        [issue] = candidates
        refs = set(finding["evidence_ids"])
        correct_refs = all(ref in evidence for ref in refs) and any(
            set(group) <= refs for group in issue["evidence_sets"])
        limit = tolerance.get(issue["type"], 0) if isinstance(tolerance, dict) else tolerance
        difference = abs(finding["claimed_amount_minor"] - issue["expected_amount_minor"])
        reason = None
        if difference > limit and correct_refs:
            reason = "wrong_amount"
        elif difference <= limit and not correct_refs:
            reason = "wrong_evidence"
        if reason or (difference > limit and not correct_refs):
            failures.append({"reason": reason or "compound_error", "type": issue["type"],
                             "affected_ids": issue["affected_ids"], "issue_id": issue["issue_id"],
                             "finding_index": int(index)})
    return failures


def objective_failures(receipt, objective):
    """Select an objective only from an actually submitted answer."""
    if objective not in OBJECTIVES:
        raise ValueError(f"Unknown financial objective: {objective}")
    if receipt.get("status") != "submitted":
        return []
    failures = receipt.get("failures") or []
    attributed = {item["issue_id"] for item in failures
                  if item["reason"] in ("wrong_amount", "wrong_evidence", "compound_error")}
    return [item for item in failures if item["reason"] == objective and (
        objective != "missed_issue" or item.get("issue_id") not in attributed)]
