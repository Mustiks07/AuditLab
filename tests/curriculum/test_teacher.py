import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from auditlab.release.transforms import write_json
from auditlab.research.curriculum.prepare_teacher import _check_teacher


class TeacherBindingTests(unittest.TestCase):
    def test_rejects_changed_truth_and_teacher_artifact(self):
        with TemporaryDirectory() as temporary:
            bundle = Path(temporary)
            snapshot = bundle / "source"
            (snapshot / "private").mkdir(parents=True)
            teacher_file = bundle / "teacher.json"
            truth_file = snapshot / "private/truth.json"
            issue = {"type": "payment_allocation", "affected_ids": ["payment/P", "invoice/I"], "currency": "KZT"}
            teacher = {"submission": {"episode_id": "E", "findings": [{**issue, "claimed_amount_minor": 20,
                        "evidence_ids": ["payments/D", "ledger/R"]}]}}
            truth = {"episode_id": "E", "issues": [{**issue, "expected_amount_minor": 20,
                     "evidence_sets": [["ledger/R", "payments/D"]]}]}
            write_json(teacher_file, teacher)
            write_json(truth_file, truth)
            selected = {"teacher": {"path": "teacher.json", "sha256": hashlib.sha256(teacher_file.read_bytes()).hexdigest()}}
            _check_teacher(snapshot, bundle / "curriculum.json", selected)
            truth["issues"][0]["expected_amount_minor"] = 21
            write_json(truth_file, truth)
            with self.assertRaisesRegex(ValueError, "truth differs"):
                _check_teacher(snapshot, bundle / "curriculum.json", selected)
            write_json(teacher_file, {"submission": {"episode_id": "E", "findings": []}})
            with self.assertRaisesRegex(ValueError, "submission changed"):
                _check_teacher(snapshot, bundle / "curriculum.json", selected)
