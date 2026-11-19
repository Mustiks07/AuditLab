"""Public release commands against a real completed runner export."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class ReleaseCliTests(unittest.TestCase):
    def test_replay_checks_real_export_and_rejects_unknown_run(self):
        from auditlab.demo.lab import build_samples
        from auditlab.demo.paths import DemoPaths

        with tempfile.TemporaryDirectory() as temporary:
            paths = DemoPaths(Path(temporary) / "source").ensure()
            build_samples(paths)
            runs = [json.loads(p.read_text(encoding="utf-8"))
                    for p in paths.exports.glob("*/run.json")]
            selected = next(r for r in runs if r["episode_id"] == "S02")
            command = [sys.executable, str(ROOT / "run.py"), "replay",
                       "--root", str(paths.root), "--exports", str(paths.exports), "--check", "--run"]
            environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            replay = subprocess.run(command + [selected["run_id"]], cwd=ROOT,
                                    capture_output=True, text=True, encoding="utf-8", timeout=30,
                                    env=environment)
            self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
            self.assertIn("наблюдения совпали", replay.stdout)
            self.assertIn("Повтор оценки: совпала", replay.stdout)
            missing = subprocess.run(command + ["unknown-run"], cwd=ROOT,
                                     capture_output=True, text=True, encoding="utf-8", timeout=30,
                                     env=environment)
            self.assertEqual(missing.returncode, 2, missing.stdout + missing.stderr)


if __name__ == "__main__":
    unittest.main()
