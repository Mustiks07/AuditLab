"""Runner: public reset/step facade, budget, timer, isolation, trajectories and exports."""
from auditlab.runner.env import AuditEnvironment, RunnerError


def make_env(mode: str, **options) -> AuditEnvironment:
    """docs/INTERFACES.md entry point. `mode` is "train" or "closed".

    Then reset(seed, {"snapshot_dir": ..., "export_dir": ..., "policy_name": ...}) (INTERFACES 1.1).
    Optional trusted reset keys: run_id, time_limit_s. Any other key is generator configuration,
    used only when snapshot_dir is absent. export_dir is the run directory itself and must be new.

    Optional keyword arguments (trusted side, tests): session_opener, generator, clock,
    evaluator ("subprocess" | "inprocess" | callable), trusted_dir.
    """
    return AuditEnvironment(mode, **options)


__all__ = ["AuditEnvironment", "RunnerError", "make_env"]
