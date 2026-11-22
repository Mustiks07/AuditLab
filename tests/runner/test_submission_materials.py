"""The delivered trajectory is executable evidence, with strict corruption controls."""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from tools.export_trajectories import ROOT, export, replay
from tools.make_judge_config import create


class SubmissionMaterials(unittest.TestCase):
    def test_published_trajectory_replays_all_twenty_episodes(self):
        result = replay(ROOT / "datasets/open-1.3", ROOT / "trajectories.jsonl")
        self.assertEqual(result["episodes"], 20)
        manifest = json.loads((ROOT / "trajectories.manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["trajectories_sha256"], hashlib.sha256((ROOT / "trajectories.jsonl").read_bytes()).hexdigest())
        self.assertEqual({split: sum(x["split"] == split for x in manifest["episodes"])
                          for split in ("train", "dev")}, {"train": 10, "dev": 10})

    def test_corrupted_reward_and_missing_episodes_are_rejected(self):
        lines = (ROOT / "trajectories.jsonl").read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "corrupt.jsonl"
            path.write_text(json.dumps({**first, "reward": 1}) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "replay mismatch"):
                replay(ROOT / "datasets/open-1.3", path)
            path.write_text(lines[0] + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "finish all"):
                replay(ROOT / "datasets/open-1.3", path)

    def test_isolated_regeneration_matches_published_bytes(self):
        image = os.environ.get("AUDITLAB_POLICY_IMAGE")
        if not image:
            self.skipTest("requires the real policy image, supplied by CI")
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "export"
            export(ROOT / "datasets/open-1.3", output, image)
            self.assertEqual((output / "trajectories.jsonl").read_bytes(), (ROOT / "trajectories.jsonl").read_bytes())

    def test_private_key_is_random_restricted_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a.json", Path(tmp) / "b.json"
            create(a)
            original = a.read_bytes()
            create(b)
            key = json.loads(original)["episode_key"]
            self.assertEqual(len(key), 64)
            self.assertNotEqual(key, json.loads(b.read_bytes())["episode_key"])
            with self.assertRaises(FileExistsError):
                create(a)
            self.assertEqual(a.read_bytes(), original)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(a.stat().st_mode), 0o600)
