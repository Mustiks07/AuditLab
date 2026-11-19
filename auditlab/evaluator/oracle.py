"""Trusted oracle: builds a perfect submission from the closed truth. Lives only on the trusted side."""
from __future__ import annotations

from auditlab.contracts import Submission


def oracle_submission(truth: dict) -> Submission:
    """One finding per truth issue, citing the smallest admissible evidence set."""
    findings = []
    for issue in sorted(truth["issues"], key=lambda item: item["issue_id"]):
        evidence = min(issue["evidence_sets"], key=lambda group: (len(group), sorted(group)))
        findings.append({"type": issue["type"], "affected_ids": list(issue["affected_ids"]),
                         "claimed_amount_minor": int(issue["expected_amount_minor"]),
                         "currency": issue["currency"], "evidence_ids": sorted(evidence),
                         "explanation": f"Oracle: {issue['type']} for {', '.join(issue['affected_ids'])}."})
    return {"episode_id": truth["episode_id"], "findings": findings}
