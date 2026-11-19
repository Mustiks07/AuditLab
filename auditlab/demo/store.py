"""Read-only access to completed public run exports. The web process uses only this.

An export directory holds run.json, public/ (the snapshot the auditor saw),
trajectories.jsonl, submission.json, score.json (docs/INTERFACES.md) and
optionally verifier.json and console.json (console metadata: label, lineage,
engines; never overrides the runner's run.json fields).
Only completed open runs are listed: closed-mode, unfinished attempts and
exports that contain a private/ directory are refused.
No other path on disk is reachable through this class.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
OP_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{5,63}$")
EXPORT_FILES = ("run.json", "trajectories.jsonl", "submission.json", "score.json", "verifier.json", "console.json")
CORE_FIELDS = ("run_id", "episode_id", "policy", "mode", "status")


class NotFound(LookupError):
    pass


class Refused(PermissionError):
    pass


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


class ExportStore:
    def __init__(self, roots: list[Path], ops_dir: Path | None = None):
        self.roots = [Path(r).resolve() for r in roots]
        self.ops_dir = Path(ops_dir).resolve() if ops_dir else None

    # ------------------------------------------------------------------ listing
    def _candidates(self):
        for root in self.roots:
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                if child.is_dir() and not child.name.startswith(".") and (child / "run.json").is_file():
                    yield child

    def _check(self, directory: Path) -> tuple[dict | None, str | None]:
        try:
            meta = _read_json(directory / "run.json")
        except (OSError, ValueError):
            return None, "run.json unreadable"
        if not isinstance(meta, dict) or not RUN_ID_RE.match(str(meta.get("run_id", ""))):
            return None, "run.json has no valid run_id"
        try:
            extra = _read_json(directory / "console.json") or {}
        except (OSError, ValueError):
            extra = {}
        if isinstance(extra, dict):
            meta = {**{k: v for k, v in extra.items() if k not in CORE_FIELDS}, **meta}
        if meta.get("mode") != "train":
            return meta, "closed run: not available in the console"
        if meta.get("status") != "completed":
            return meta, "attempt is not completed: not published in the console"
        if (directory / "private").exists():
            return meta, "export contains private/: refused"
        for name in ("submission.json", "score.json", "trajectories.jsonl"):
            if not (directory / name).is_file():
                return meta, f"export has no {name}: refused"
        return meta, None

    def list_runs(self) -> list[dict]:
        out, seen = [], set()
        for directory in self._candidates():
            meta, problem = self._check(directory)
            if problem or meta is None or meta["run_id"] in seen:
                continue
            seen.add(meta["run_id"])
            out.append(meta)
        return out

    def run_dir(self, run_id: str) -> Path:
        if not RUN_ID_RE.match(run_id or ""):
            raise NotFound(run_id)
        for directory in self._candidates():
            meta, problem = self._check(directory)
            if meta is not None and meta.get("run_id") == run_id:
                if problem:
                    raise Refused(problem)
                return directory
        raise NotFound(run_id)

    # ------------------------------------------------------------------ reading
    def meta(self, run_id: str) -> dict:
        directory = self.run_dir(run_id)
        meta, _ = self._check(directory)
        return dict(meta or {})

    def read(self, run_id: str, name: str):
        if name not in EXPORT_FILES:
            raise Refused(name)
        if name == "run.json":
            return self.meta(run_id)
        path = self.run_dir(run_id) / name
        if name.endswith(".jsonl"):
            if not path.exists():
                return None
            return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return _read_json(path)

    def public_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "public"

    def op_status(self, op_key: str) -> dict | None:
        if self.ops_dir is None or not OP_KEY_RE.match(op_key or ""):
            return None
        return _read_json(self.ops_dir / f"{op_key}.json")
