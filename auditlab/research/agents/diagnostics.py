"""Content diagnosis of one attempt, on the TRUSTED side (reads truth; never shown to an agent).

F1 alone hides what went wrong. For every truth issue: found or missed (by type). For every submitted
finding: correct, or why not - wrong amount, wrong currency, wrong affected entities, evidence missing
or unavailable, a duplicate, a false accusation on a clean company, or an unrelated claim. The labels
come from the evaluator's own matching (matches/unmatched) plus a field-by-field comparison with the
nearest truth issue; the official score itself is never recomputed here.

Service outcomes (rate limits, provider/bridge failures) are not content: they get no diagnosis.
"""
from __future__ import annotations

FINDING_LABELS = ("correct", "duplicate", "wrong_amount", "wrong_currency", "wrong_entities", "evidence_incomplete",
                  "evidence_unavailable", "false_accusation_on_clean", "unrelated_claim")


def _same_set(a, b) -> bool:
    return set(a or []) == set(b or [])


def diagnose(final_answer: dict | None, truth: dict, evaluation: dict | None) -> dict:
    issues = truth.get("issues") or []
    findings = (final_answer or {}).get("findings") if isinstance(final_answer, dict) else None
    findings = findings if isinstance(findings, list) else []
    matches = {int(index): issue_id for index, issue_id in ((evaluation or {}).get("matches") or [])}
    unmatched = {int(k): v for k, v in ((evaluation or {}).get("unmatched") or {}).items()}
    found = set(matches.values())
    per_issue = [{"issue_id": issue["issue_id"], "type": issue["type"], "found": issue["issue_id"] in found}
                 for issue in issues]
    per_finding = []
    for index, finding in enumerate(findings):
        if index in matches:
            label = "correct"
        elif not issues:
            label = "false_accusation_on_clean"
        elif unmatched.get(index) == "duplicate":
            label = "duplicate"
        elif unmatched.get(index) == "unavailable_evidence":
            label = "evidence_unavailable"
        elif unmatched.get(index) == "evidence_incomplete":
            label = "evidence_incomplete"
        else:
            label = "unrelated_claim"
            same_type = [i for i in issues if isinstance(finding, dict) and finding.get("type") == i["type"]]
            entity = [i for i in same_type if _same_set(finding.get("affected_ids"), i["affected_ids"])]
            if entity:
                issue = entity[0]
                if finding.get("currency") != issue["currency"]:
                    label = "wrong_currency"
                elif finding.get("claimed_amount_minor") != issue["expected_amount_minor"]:
                    label = "wrong_amount"
            elif any(set(finding.get("affected_ids") or []) & set(i["affected_ids"]) for i in same_type):
                label = "wrong_entities"
        per_finding.append({"index": index, "type": finding.get("type") if isinstance(finding, dict) else None,
                            "label": label})
    return {"truth_issues": len(issues), "clean": not issues, "submitted_findings": len(findings),
            "issues": per_issue, "findings": per_finding,
            "missed_by_type": _count(i["type"] for i in per_issue if not i["found"]),
            "finding_labels": _count(f["label"] for f in per_finding)}


def _count(values) -> dict:
    counts: dict = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))
