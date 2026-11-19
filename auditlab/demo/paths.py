"""Directory layout of the demo workspace.

runs/demo/exports/  completed public exports  (web: read)
runs/demo/ops/      operation status files     (web: read; no answers inside)
runs/demo/trusted/  training truth per episode (trusted lab only; web never opens it)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PACKAGE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class DemoPaths:
    root: Path

    @classmethod
    def default(cls) -> "DemoPaths":
        return cls(Path(os.environ.get("AUDITLAB_DEMO_ROOT", REPO / "runs" / "demo")).resolve())

    @property
    def exports(self) -> Path:
        return self.root / "exports"

    @property
    def ops(self) -> Path:
        return self.root / "ops"

    @property
    def trusted(self) -> Path:
        return self.root / "trusted"

    def ensure(self) -> "DemoPaths":
        for path in (self.exports, self.ops, self.trusted):
            path.mkdir(parents=True, exist_ok=True)
        return self
