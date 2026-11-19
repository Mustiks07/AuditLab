"""Checks the committed open exports in tests/runner/golden/ (first real runs, for Claude Pro 3).

They were produced with the real simulator from PR #8 (open_session + make_policy("reconcile"),
policy in its own process):
    python -m auditlab.runner policy --snapshot fixtures/foundation/S02 --name reconcile --seed 19 \
        --run-id S02-reconcile --export-dir tests/runner/golden/S02-reconcile
This test needs no simulator: it re-derives everything it can from the files and the fixture truth.
"""
import hashlib
import json
import unittest
from pathlib import Path

from auditlab.evaluator import score_submission

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parents[1] / "fixtures" / "foundation"
GOLDEN = HERE / "golden"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def hashes(path):
    return {str(f.relative_to(path)): hashlib.sha256(f.read_bytes()).hexdigest() for f in path.rglob("*") if f.is_file()}


class GoldenExports(unittest.TestCase):
    def test_layout_status_and_scores(self):
        runs = sorted(p for p in GOLDEN.iterdir() if p.is_dir())
        self.assertEqual([p.name for p in runs], ["S01-reconcile", "S02-reconcile"])
        for run_dir in runs:
            with self.subTest(run=run_dir.name):
                self.assertEqual(sorted(p.name for p in run_dir.iterdir()),
                                 ["public", "run.json", "score.json", "submission.json", "trajectories.jsonl"])
                run = load(run_dir / "run.json")
                label = run["episode_id"]
                self.assertEqual({k: run[k] for k in ("run_id", "policy", "mode", "status")},
                                 {"run_id": run_dir.name, "policy": "reconcile", "mode": "train", "status": "completed"})
                self.assertEqual(hashes(run_dir / "public"), hashes(FIXTURES / label / "public"))
                rows = [json.loads(line) for line in (run_dir / "trajectories.jsonl").read_text(encoding="utf-8").splitlines()]
                self.assertEqual([r["step"] for r in rows], list(range(1, len(rows) + 1)))
                self.assertEqual([r["observation"]["remaining_steps"] for r in rows],
                                 [100 - i for i in range(1, len(rows) + 1)])
                self.assertTrue(all(r["reward"] == 0 and not r["terminated"] for r in rows[:-1]))
                last = rows[-1]
                self.assertEqual((last["action"]["action"], last["terminated"], last["truncated"]), ("submit", True, False))
                submission = load(run_dir / "submission.json")
                self.assertEqual(last["action"]["args"], submission)
                truth = load(FIXTURES / label / "private" / "truth.json")
                score = score_submission(submission, truth, run["inspection_steps"])
                self.assertEqual(load(run_dir / "score.json"), score)
                self.assertEqual(last["reward"], score["reward"])
                self.assertEqual((score["fp"], score["fn"]), (0, 0))
                blob = "".join(p.read_text(encoding="utf-8") for p in run_dir.rglob("*") if p.is_file())
                self.assertNotIn("evidence_sets", blob)
                self.assertNotIn("issue_id", blob)


if __name__ == "__main__":
    unittest.main()
