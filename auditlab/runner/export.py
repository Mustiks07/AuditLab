"""Atomic, write-once run directories (SPEC §11, INTERFACES "Завершённый публичный экспорт")."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path


def _dump(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _publish(root: Path, run_id: str, build) -> Path | None:
    """Build in a temp directory next to the target, then rename. Never overwrites a finished run."""
    root.mkdir(parents=True, exist_ok=True)
    target = root / run_id
    if target.exists():
        return None
    staging = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=root))
    try:
        build(staging)
        os.replace(staging, target)  # atomic on one filesystem; fails if a non-empty target appeared meanwhile
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        if not target.exists():
            raise
        return None
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target


def _trajectory(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_public_run(export_dir: Path, run: dict, public_dir: Path, trajectory: list[dict],
                     submission: object, score: dict) -> Path | None:
    """run.json, public/, trajectories.jsonl, submission.json, score.json. Open mode only; no truth inside."""
    def build(staging: Path) -> None:
        _dump(staging / "run.json", run)
        shutil.copytree(public_dir, staging / "public")
        _trajectory(staging / "trajectories.jsonl", trajectory)
        _dump(staging / "submission.json", submission)
        _dump(staging / "score.json", score)

    return _publish(Path(export_dir), run["run_id"], build)


def write_trusted_report(trusted_dir: Path, run_id: str, report: dict, trajectory: list[dict]) -> Path | None:
    """Closed diagnostics live apart from anything a policy or the console can read."""
    def build(staging: Path) -> None:
        _dump(staging / "report.json", report)
        _trajectory(staging / "trajectory.jsonl", trajectory)

    return _publish(Path(trusted_dir), run_id, build)
