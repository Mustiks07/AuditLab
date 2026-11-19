"""Maximum-cardinality matching of findings to truth issues and reward arithmetic (SPEC §9)."""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from auditlab.contracts import MAX_STEPS, Score, Submission
from auditlab.evaluator.schema import SubmissionError, validate_submission

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


def validate_truth(truth: object) -> None:
    """Reject a reference that violates the uniqueness rule of SPEC §9.3 or lacks required fields."""
    if not isinstance(truth, dict) or not isinstance(truth.get("episode_id"), str) \
            or not isinstance(truth.get("issues"), list):
        raise TruthError("truth must be an object with episode_id and issues")
    seen: set[tuple] = set()
    issue_ids: set[str] = set()
    for issue in truth["issues"]:
        try:
            key = (issue["type"], _canon_ids(issue["affected_ids"]), issue["currency"],
                   int(issue["expected_amount_minor"]))
            sets = issue["evidence_sets"]
            issue_id = issue["issue_id"]
        except (KeyError, TypeError, ValueError) as error:
            raise TruthError(f"malformed truth issue: {error!r}") from error
        if not sets or any(not group for group in sets):
            raise TruthError("every issue needs at least one non-empty evidence set")
        if key in seen or issue_id in issue_ids:
            raise TruthError("duplicate financial issue in truth")
        seen.add(key)
        issue_ids.add(issue_id)


def _covers(issue: dict, evidence: set[str]) -> bool:
    return any(set(group) <= evidence for group in issue["evidence_sets"])


def _field_match(finding: dict, issue: dict, tolerance_minor: int) -> bool:
    return (finding["type"] == issue["type"]
            and _canon_ids(finding["affected_ids"]) == _canon_ids(issue["affected_ids"])
            and finding["currency"] == issue["currency"]
            and abs(finding["claimed_amount_minor"] - issue["expected_amount_minor"]) <= tolerance_minor)


def _max_matching(adjacency: list[list[int]], right_count: int) -> list[int | None]:
    """Kuhn's augmenting paths in a fixed order, so equal-size optima are chosen reproducibly."""
    owner: list[int | None] = [None] * right_count

    def augment(left: int, seen: set[int]) -> bool:
        for right in adjacency[left]:
            if right in seen:
                continue
            seen.add(right)
            if owner[right] is None or augment(owner[right], seen):
                owner[right] = left
                return True
        return False

    for left in range(len(adjacency)):
        augment(left, set())
    return owner


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
             tolerance_minor: int = 0) -> Evaluation:
    """Full trusted evaluation. A structurally invalid submission scores -1 (SPEC §8)."""
    validate_truth(truth)
    if not isinstance(inspection_steps, int) or isinstance(inspection_steps, bool) \
            or not 0 <= inspection_steps <= MAX_STEPS:
        raise ValueError("inspection_steps must be an integer in 0..100")
    issues = sorted(truth["issues"], key=lambda issue: issue["issue_id"])
    try:
        validate_submission(submission, truth["episode_id"])
    except SubmissionError as error:
        score: Score = {"tp": 0, "fp": 0, "fn": len(issues), "precision": 0.0, "recall": 0.0,
                        "f1": 0.0, "reward": -1.0}
        return Evaluation(score, "invalid_submission", error.code)

    findings = submission["findings"]  # type: ignore[index]
    # Canonical order makes the matching independent of the order the policy listed its findings.
    order = sorted(range(len(findings)), key=lambda i: (
        findings[i]["type"], _canon_ids(findings[i]["affected_ids"]), findings[i]["currency"],
        findings[i]["claimed_amount_minor"], tuple(sorted(set(findings[i]["evidence_ids"]))), i))
    adjacency = []
    field_edges = []
    for index in order:
        evidence = set(findings[index]["evidence_ids"])
        by_field = [j for j, issue in enumerate(issues) if _field_match(findings[index], issue, tolerance_minor)]
        field_edges.append(by_field)
        adjacency.append([j for j in by_field if _covers(issues[j], evidence)])
    owner = _max_matching(adjacency, len(issues))
    matched_left = {left: right for right, left in enumerate(owner) if left is not None}
    matches = sorted((order[left], issues[right]["issue_id"]) for left, right in matched_left.items())
    tp = len(matches)
    matched_rights = set(matched_left.values())
    unmatched: dict[int, str] = {}
    for position, index in enumerate(order):
        if position in matched_left:
            continue
        if any(j in matched_rights for j in adjacency[position]):
            unmatched[index] = "duplicate"
        elif field_edges[position]:
            unmatched[index] = "evidence_incomplete"
        else:
            unmatched[index] = "no_matching_issue"
    return Evaluation(_score(tp, len(findings) - tp, len(issues) - tp, inspection_steps), "scored",
                      matches=matches, unmatched=dict(sorted(unmatched.items())))


def score_submission(submission: Submission, truth: dict, inspection_steps: int) -> Score:
    """Public evaluator entry point of docs/INTERFACES.md."""
    return evaluate(submission, truth, inspection_steps).score
