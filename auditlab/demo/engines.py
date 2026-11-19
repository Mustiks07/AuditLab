"""Pick the real module of another workstream when it is merged, else the labelled stand-in."""
from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

from . import standin

STANDIN = "demo-standin"


def _module(name: str):
    try:
        if importlib.util.find_spec(name) is None:
            return None
    except ModuleNotFoundError:
        return None
    return importlib.import_module(name)


def session(public_dir: Path):
    env = _module("auditlab.environment")
    if env is not None and hasattr(env, "open_session"):
        return env.open_session(public_dir), "auditlab.environment"
    return standin.StandinSession(public_dir), STANDIN


def policy(name: str, seed: int = 0):
    mod = _module("auditlab.policies")
    if mod is not None and hasattr(mod, "make_policy"):
        return mod.make_policy(name, seed), "auditlab.policies"
    if name != "reconcile":
        raise ValueError(f"policy {name!r} needs auditlab.policies")
    return standin.ReconcilePolicy(seed), STANDIN


def scorer():
    mod = _module("auditlab.evaluator")
    if mod is not None and hasattr(mod, "score_submission"):
        def score(submission, truth, steps):
            return dict(mod.score_submission(submission, truth, steps))
        return score, "auditlab.evaluator"
    return standin.score, STANDIN


def verifier():
    mod = _module("auditlab.verifier")
    if mod is not None and hasattr(mod, "verify_snapshot"):
        return mod.verify_snapshot, "auditlab.verifier"
    return None, None
