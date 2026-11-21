"""Strict structural validation of a submit body (SPEC §7 "Валидация", §9.1, MAT-03).

Structural failure ends the episode with reward -1 (SPEC §8). Semantic mistakes (wrong amount,
wrong currency, wrong ids) are *structurally valid* and go to matching, where they become FP.

Two published rule sets share this code (see matcher.RULES):

* ``auditlab-1.1`` (default; public schema 1.0): ``type`` must be one of ISSUE_TYPES and
  ``claimed_amount_minor`` nonnegative, otherwise the submit is structurally invalid.
* ``genesis-2026-11-20`` (the organizers' judge adapter): structure means JSON types, required
  fields and the original list/text limits only. Any string ``type`` and any integer amount are
  structurally valid; an unknown type or a negative amount simply cannot match (FP).
"""
from __future__ import annotations

from auditlab.contracts import MAX_EVIDENCE, MAX_EXPLANATION, MAX_FINDINGS

RULES_INTERNAL = "auditlab-1.1"
RULES_GENESIS = "genesis-2026-11-20"
RULES = (RULES_INTERNAL, RULES_GENESIS)
ISSUE_TYPES = frozenset({"duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff"})
SUBMISSION_KEYS = frozenset({"episode_id", "findings"})
FINDING_KEYS = frozenset({"type", "affected_ids", "claimed_amount_minor", "currency",
                          "evidence_ids", "explanation"})


class SubmissionError(ValueError):
    """Structurally invalid submission. `code` is a short stable reason for the trusted log only."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _str_list(value: object, code: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SubmissionError(code)
    return value


def validate_submission(submission: object, episode_id: str | None = None, *,
                        rules: str = RULES_INTERNAL) -> None:
    """Raise SubmissionError if `submission` violates the published schema of `rules`.

    Limits are checked on the submitted lists and text as sent, before any set normalization:
    11 copies of one evidence ID are 11 IDs. Text length is counted in Unicode code points.
    """
    if rules not in RULES:
        raise ValueError(f"unknown rules: {rules!r}")
    internal = rules == RULES_INTERNAL
    if not isinstance(submission, dict) or set(submission) != SUBMISSION_KEYS:
        raise SubmissionError("bad_envelope")
    if not isinstance(submission["episode_id"], str):
        raise SubmissionError("bad_episode_id")
    if episode_id is not None and submission["episode_id"] != episode_id:
        raise SubmissionError("episode_mismatch")
    findings = submission["findings"]
    if not isinstance(findings, list):
        raise SubmissionError("bad_findings")
    if len(findings) > MAX_FINDINGS:
        raise SubmissionError("too_many_findings")
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != FINDING_KEYS:
            raise SubmissionError("bad_finding_keys")
        if not isinstance(finding["type"], str) or (internal and finding["type"] not in ISSUE_TYPES):
            raise SubmissionError("bad_type")
        _str_list(finding["affected_ids"], "bad_affected_ids")
        if not _is_int(finding["claimed_amount_minor"]) or (internal and finding["claimed_amount_minor"] < 0):
            raise SubmissionError("bad_amount")
        if not isinstance(finding["currency"], str):
            raise SubmissionError("bad_currency")
        if len(_str_list(finding["evidence_ids"], "bad_evidence_ids")) > MAX_EVIDENCE:
            raise SubmissionError("too_many_evidence_ids")
        explanation = finding["explanation"]
        if not isinstance(explanation, str) or len(explanation) > MAX_EXPLANATION:
            raise SubmissionError("bad_explanation")
