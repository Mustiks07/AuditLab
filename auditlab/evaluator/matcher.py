"""Maximum-cardinality matching of findings to truth issues and reward arithmetic (SPEC §9).

Evaluator 1.2. Shared by both rule sets (schema.RULES):

* Every cited evidence key must be available in the episode when the caller supplies the public
  evidence index (evidence.PublicEvidence); one unavailable key makes the finding unmatched (FP),
  even if it also contains a complete admissible set.
* Among maximum matchings the organizers' tie-break is used: issue_ids sorted by Unicode code
  points, then the lexicographically smallest sorted sequence of (issue_id, finding index).

Only for ``genesis-2026-11-20``: any string type / integer amount is structurally valid (see
schema.py), and truth may contain two issues with the same (type, affected_ids), as the official
ambiguity fixtures M10/M11 require. The internal generator profile keeps rejecting that.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from auditlab.contracts import MAX_EVIDENCE, MAX_STEPS, Score, Submission
from auditlab.evaluator.schema import (ISSUE_TYPES, RULES, RULES_INTERNAL, SubmissionError,
                                       validate_submission)

INSPECTION_COST = Fraction(2, 1000)


class TruthError(ValueError):
    """The closed reference itself is malformed or ambiguous (a generator defect, not a policy error)."""


@dataclass
class Evaluation:
    """Trusted result. `score` is the only part that may ever reach a policy, and only in train mode."""

    score: Score
    status: str  # "scored" | "invalid_submission"
    reason: str | None = None
    matches: list[tuple[int, str]] = field(default_factory=list)  # (finding index, issue_id)
    unmatched: dict[int, str] = field(default_factory=dict)  # finding index -> diagnostic reason


def _canon_ids(ids: list[str]) -> tuple[str, ...]:
    return tuple(sorted(set(ids)))


def validate_truth(truth: object, *, distinct_causes: bool = True) -> None:
    """Validate closed truth before matching, without coercion (SPEC §5 and §9.3).

    A financial issue is identified by type and canonical affected entities. Changing
    its amount, currency or evidence does not create a second financial cause. With
    distinct_causes=False (Genesis ambiguity fixtures) only issue_id must be unique.
    """
    if not isinstance(truth, dict) or not isinstance(truth.get("episode_id"), str) \
            or not truth["episode_id"] or not isinstance(truth.get("issues"), list):
        raise TruthError("truth must be an object with episode_id and issues")
    seen: set[tuple] = set()
    issue_ids: set[str] = set()
    for issue in truth["issues"]:
        if not isinstance(issue, dict):
            raise TruthError("truth issue must be an object")
        issue_id = issue.get("issue_id")
        if not isinstance(issue_id, str) or not issue_id:
            raise TruthError("issue_id must be a non-empty string")
        issue_type = issue.get("type")
        if not isinstance(issue_type, str) or issue_type not in ISSUE_TYPES:
            raise TruthError("unknown truth issue type")
        affected = issue.get("affected_ids")
        if not isinstance(affected, list) or not affected \
                or any(not isinstance(value, str) or not value for value in affected):
            raise TruthError("affected_ids must be a non-empty list of non-empty strings")
        amount = issue.get("expected_amount_minor")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            raise TruthError("expected_amount_minor must be a nonnegative integer, not boolean")
        currency = issue.get("currency")
        if not isinstance(currency, str) or not currency:
            raise TruthError("currency must be a non-empty string")
        sets = issue.get("evidence_sets")
        if not isinstance(sets, list) or not sets:
            raise TruthError("evidence_sets must be a non-empty list")
        for group in sets:
            if not isinstance(group, list) or not 1 <= len(group) <= MAX_EVIDENCE \
                    or any(not isinstance(value, str) or not value for value in group):
                raise TruthError("each evidence set must contain 1..10 non-empty string IDs")
        key = (issue_type, _canon_ids(affected))
        if (distinct_causes and key in seen) or issue_id in issue_ids:
            raise TruthError("duplicate financial issue in truth")
        seen.add(key)
        issue_ids.add(issue_id)


def _covers(issue: dict, evidence: set[str]) -> bool:
    return any(set(group) <= evidence for group in issue["evidence_sets"])


def _tolerance(tolerance_minor: int | dict, issue_type: str) -> int:
    if isinstance(tolerance_minor, dict):
        return tolerance_minor.get(issue_type, 0)
    return tolerance_minor


def _field_match(finding: dict, issue: dict, tolerance_minor: int | dict) -> bool:
    return (finding["type"] == issue["type"]
            and _canon_ids(finding["affected_ids"]) == _canon_ids(issue["affected_ids"])
            and finding["currency"] == issue["currency"]
            and abs(finding["claimed_amount_minor"] - issue["expected_amount_minor"])
            <= _tolerance(tolerance_minor, issue["type"]))


def _max_matching(adjacency: list[list[int]], right_count: int) -> list[int | None]:
    """Owner (finding index) of every issue in the organizers' maximum matching.

    `adjacency[finding]` lists issue indexes, issues in issue_id order. Same choice as
    _lexmin_matching; kept with its 1.1 signature for callers and probes of the old helper.
    """
    candidates = [[left for left, rights in enumerate(adjacency) if right in rights] for right in range(right_count)]
    chosen = _lexmin_matching(candidates)
    return [chosen.get(right) for right in range(right_count)]


def _cardinality(candidates: list[list[int]], first: int, banned: set[int]) -> int:
    """Size of a maximum matching of issues first.. to findings outside `banned`."""
    owner: dict[int, int] = {}

    def augment(issue: int, seen: set[int]) -> bool:
        for finding in candidates[issue]:
            if finding in banned or finding in seen:
                continue
            seen.add(finding)
            if finding not in owner or augment(owner[finding], seen):
                owner[finding] = issue
                return True
        return False

    return sum(augment(issue, set()) for issue in range(first, len(candidates)))


def _lexmin_matching(candidates: list[list[int]]) -> dict[int, int]:
    """Maximum matching with the organizers' deterministic choice (fintech-checks 2026-11-20).

    `candidates[j]` lists the finding indexes that may match issue j; issues are already sorted
    by issue_id. Of all maximum-cardinality matchings return the one whose sorted sequence of
    (issue, finding) pairs is lexicographically smallest: walk the issues in order and give each
    the smallest finding that still allows a maximum matching of the rest. An issue with no such
    finding is unmatched in every admissible optimum. Returns {issue: finding}.
    """
    remaining = _cardinality(candidates, 0, set())
    chosen: dict[int, int] = {}
    used: set[int] = set()
    for issue, findings in enumerate(candidates):
        if remaining == 0:
            break
        for finding in sorted(findings):
            if finding in used:
                continue
            if _cardinality(candidates, issue + 1, used | {finding}) == remaining - 1:
                chosen[issue] = finding
                used.add(finding)
                remaining -= 1
                break
    return chosen


def _f1(tp: int, fp: int, fn: int) -> Fraction:
    denominator = 2 * tp + fp + fn
    return Fraction(1) if denominator == 0 else Fraction(2 * tp, denominator)


def reward_from_counts(tp: int, fp: int, fn: int, inspection_steps: int) -> Fraction:
    """clip(2*F1 - 1 - 0.002*inspections, -1, 1) in exact arithmetic (SPEC §9.5)."""
    raw = 2 * _f1(tp, fp, fn) - 1 - INSPECTION_COST * inspection_steps
    return max(Fraction(-1), min(Fraction(1), raw))


def _score(tp: int, fp: int, fn: int, inspection_steps: int) -> Score:
    precision = Fraction(tp, tp + fp) if tp + fp else Fraction(0)
    recall = Fraction(tp, tp + fn) if tp + fn else Fraction(0)
    return {"tp": tp, "fp": fp, "fn": fn, "precision": float(precision), "recall": float(recall),
            "f1": float(_f1(tp, fp, fn)), "reward": float(reward_from_counts(tp, fp, fn, inspection_steps))}


def evaluate(submission: Submission | object, truth: dict, inspection_steps: int,
             tolerance_minor: int | dict = 0, *, rules: str = RULES_INTERNAL,
             available_evidence: object = None) -> Evaluation:
    """Full trusted evaluation. A structurally invalid submission scores -1 (SPEC §8).

    `available_evidence` is a container of the episode's available evidence keys
    (evidence.PublicEvidence); None skips the availability check (unit use without files).
    `tolerance_minor` is one integer or {issue type: integer}; unlisted types use 0.
    """
    if rules not in RULES:
        raise ValueError(f"unknown rules: {rules!r}")
    validate_truth(truth, distinct_causes=rules == RULES_INTERNAL)
    if not isinstance(inspection_steps, int) or isinstance(inspection_steps, bool) \
            or not 0 <= inspection_steps <= MAX_STEPS:
        raise ValueError("inspection_steps must be an integer in 0..100")
    issues = sorted(truth["issues"], key=lambda issue: issue["issue_id"])
    try:
        validate_submission(submission, truth["episode_id"], rules=rules)
    except SubmissionError as error:
        score: Score = {"tp": 0, "fp": 0, "fn": len(issues), "precision": 0.0, "recall": 0.0,
                        "f1": 0.0, "reward": -1.0}
        return Evaluation(score, "invalid_submission", error.code)

    findings = submission["findings"]  # type: ignore[index]
    # Edges use the findings' own input indexes: the published tie-break is defined on them. The
    # counts TP/FP/FN never depend on the order, only which of identical findings is matched.
    field_edges: list[list[int]] = []
    adjacency: list[list[int]] = []
    unavailable: list[bool] = []
    for finding in findings:
        evidence = set(finding["evidence_ids"])
        by_field = [j for j, issue in enumerate(issues) if _field_match(finding, issue, tolerance_minor)]
        missing = available_evidence is not None and any(key not in available_evidence for key in evidence)
        field_edges.append(by_field)
        unavailable.append(missing)
        adjacency.append([] if missing else [j for j in by_field if _covers(issues[j], evidence)])
    candidates = [[i for i, edges in enumerate(adjacency) if j in edges] for j in range(len(issues))]
    chosen = _lexmin_matching(candidates)  # {issue: finding}; issues are sorted by issue_id
    matches = sorted((finding, issues[issue]["issue_id"]) for issue, finding in chosen.items())
    tp = len(matches)
    matched_findings = set(chosen.values())
    unmatched: dict[int, str] = {}
    for index in range(len(findings)):
        if index in matched_findings:
            continue
        if any(j in chosen for j in adjacency[index]):
            unmatched[index] = "duplicate"
        elif field_edges[index] and unavailable[index]:
            unmatched[index] = "unavailable_evidence"
        elif field_edges[index]:
            unmatched[index] = "evidence_incomplete"
        else:
            unmatched[index] = "no_matching_issue"
    return Evaluation(_score(tp, len(findings) - tp, len(issues) - tp, inspection_steps), "scored",
                      matches=matches, unmatched=unmatched)


def score_submission(submission: Submission, truth: dict, inspection_steps: int) -> Score:
    """Public evaluator entry point of docs/INTERFACES.md."""
    return evaluate(submission, truth, inspection_steps).score
