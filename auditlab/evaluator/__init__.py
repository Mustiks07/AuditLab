"""Trusted evaluator: strict submit validation, maximum matching, reward, oracle."""
from auditlab.evaluator.matcher import Evaluation, TruthError, evaluate, reward_from_counts, score_submission
from auditlab.evaluator.oracle import oracle_submission
from auditlab.evaluator.schema import SubmissionError, validate_submission

__all__ = ["Evaluation", "SubmissionError", "TruthError", "evaluate", "oracle_submission",
           "reward_from_counts", "score_submission", "validate_submission"]
