"""Minimal counterexamples of the data-shortcut review v3 (Claude Pro 2). Internal checks, not a jury score.

    python docs/reviews/data-shortcuts-v3/repro_v3.py OUT_DIR [--checkout PATH]

Episodes come from the checkout's judge_adapter in a separate process (default: this repository), with
fresh seeds 9360001-9360003. Each case prints its input config, expected and actual result and `defect`.
"""
import argparse, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from review.data_shortcuts.counterexamples import reproduce  # noqa: E402

ap = argparse.ArgumentParser(); ap.add_argument("out", type=Path); ap.add_argument("--checkout", type=Path, default=ROOT)
a = ap.parse_args()
rep = reproduce(a.checkout.resolve(), a.out.resolve())
(a.out.resolve() / "repro-v3.json").write_text(json.dumps(rep, indent=1), encoding="utf-8")
print(json.dumps(rep, indent=1))
