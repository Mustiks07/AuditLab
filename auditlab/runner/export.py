"""Atomic, write-once run directories (SPEC §11, INTERFACES 1.1 "Завершённый публичный экспорт").

Files are written as ASCII JSON (non-ASCII escaped), so readers get identical values whatever
text encoding they open them with.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path


def _dump(path: Path, value: object) -> None:
    # newline="\n": identical bytes on every OS (REP-01 hashes), no CRLF on Windows.
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n")


def _trajectory(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")


def publish_directory(target: Path, build) -> Path | None:
    """Build next to `target` in a hidden staging directory, then rename into place.

    The target must not exist; an existing export is never overwritten (returns None).
    """
    target = Path(target)
    if os.path.lexists(target):
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=target.parent))
    try:
        build(staging)
        os.rename(staging, target)  # atomic on one filesystem; fails if target appeared meanwhile
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        if os.path.lexists(target):
            return None
        raise
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target


def write_public_run(run_dir: Path, run: dict, public_dir: Path, trajectory: list[dict],
                     submission: object, score: dict | None) -> Path | None:
    """run.json, public/, trajectories.jsonl, submission.json, score.json directly in `run_dir`.

    Open (train) mode only. Nothing from private/ is copied. `score` is None only for a failed attempt.
    """
    def build(staging: Path) -> None:
        shutil.copytree(public_dir, staging / "public")
        _trajectory(staging / "trajectories.jsonl", trajectory)
        _dump(staging / "submission.json", submission)
        if score is not None:
            _dump(staging / "score.json", score)
        _dump(staging / "run.json", run)

    return publish_directory(Path(run_dir), build)


def write_trusted_report(trusted_dir: Path, run_id: str, report: dict, trajectory: list[dict]) -> Path | None:
    """Closed diagnostics live apart from anything a policy or the console can read."""
    def build(staging: Path) -> None:
        _dump(staging / "report.json", report)
        _trajectory(staging / "trajectory.jsonl", trajectory)

    return publish_directory(Path(trusted_dir) / run_id, build)
