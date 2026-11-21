"""Genesis FinTech judge entry points (https://genesis-exe.com/fintech-checks.html, 2026-11-20).

    generate_episode(seed, config, output_dir) -> None
    make_env() -> object with reset(seed, config) and step(action)
    score_episode(episode_dir, findings, inspection_steps) -> dict

Run from the repository root (or /app in the AuditLab runtime image), so that `import
judge_adapter` and the `auditlab` package resolve. Everything here runs on the judge's trusted
side; the policy only ever receives observations returned by reset/step. Implementation and the
exact wire mapping: auditlab/runner/genesis.py and docs/GENESIS_ADAPTER.md.
"""
from auditlab.runner.genesis import generate_episode, make_env, score_episode

__all__ = ["generate_episode", "make_env", "score_episode"]
