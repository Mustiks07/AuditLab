"""Runner: public reset/step facade, budget, timer, isolation, trajectories and exports."""
from auditlab.runner.env import AuditEnvironment, RunnerError


def make_env(mode: str, **options) -> AuditEnvironment:
    """docs/INTERFACES.md entry point. `mode` is "train" or "closed".

    Optional keyword arguments (trusted side only): session_opener, generator, clock, evaluator
    ("subprocess" | "inprocess" | callable), export_dir (train only), trusted_dir.
    Per-episode runner options go in reset(seed, config)["runner"]:
    snapshot_dir, time_limit_s, run_id, policy.
    """
    return AuditEnvironment(mode, **options)


__all__ = ["AuditEnvironment", "RunnerError", "make_env"]
