"""Open train/dev episodes of the simulator (datasets/open/*/*). For each episode the verifier
gets a copy of public/ only, its answer is written first, and only then truth is read."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from auditlab.verifier import verify_report  # noqa: E402
from auditlab.verifier.crosscheck import compare  # noqa: E402

DATASETS = ROOT / "datasets" / "open"
RECORDED = Path(__file__).parent / "answers" / "simulator-0043cd9" / "answers.json"


def episodes():
    return sorted(p.parent for p in DATASETS.glob("*/*/public") if (p.parent / "private" / "truth.json").is_file())


@unittest.skipUnless(episodes(), "не проверено: datasets/open ещё не в этой ветке (поставляет PR симулятора)")
class GeneratedDatasetTests(unittest.TestCase):
    def test_public_only_answer_then_truth(self):
        recorded = {r["episode_id"]: r for r in json.loads(RECORDED.read_text(encoding="utf-8"))["episodes"]}
        for episode in episodes():
            with self.subTest(episode=f"{episode.parent.name}/{episode.name}"), \
                    tempfile.TemporaryDirectory() as temporary:
                public = Path(temporary) / "public"
                shutil.copytree(episode / "public", public)
                report = verify_report(public)
                answer = Path(temporary) / "answer.json"
                answer.write_text(json.dumps(report["findings"], sort_keys=True), encoding="utf-8")
                previous = recorded.get(report["episode_id"])
                if previous and previous["input_sha256"] == report["input_sha256"]:
                    # Same public files as the answers committed before truth: same answer.
                    self.assertEqual(report["findings"], previous["findings"])
                truth = json.loads((episode / "private" / "truth.json").read_text(encoding="utf-8"))
                self.assertEqual(compare(report, truth, public), [])


if __name__ == "__main__":
    unittest.main()
