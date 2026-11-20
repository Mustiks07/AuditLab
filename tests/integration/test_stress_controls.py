"""Negative controls for financial stress evidence and fresh-store claims."""
import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StressControlsTests(unittest.TestCase):
    def test_reference_control_detects_amount_currency_extra_and_evidence(self):
        stress = load("check_generalization")
        finding = {"type": "fx_conversion", "affected_ids": ["invoice/I1"],
                   "claimed_amount_minor": 1, "currency": "JPY", "evidence_ids": ["ledger/R1"]}
        truth = {"issues": [{"type": "fx_conversion", "affected_ids": ["invoice/I1"],
                            "expected_amount_minor": 1, "currency": "JPY", "evidence_sets": [["ledger/R1"]]}]}
        self.assertEqual(stress.compare_reference([finding], truth), [])
        for field, value in (("claimed_amount_minor", 100), ("currency", "KWD"), ("evidence_ids", [])):
            altered = {**finding, field: value}
            self.assertTrue(stress.compare_reference([altered], truth), field)
        self.assertTrue(stress.compare_reference([finding, copy.deepcopy(finding)], truth))
        self.assertTrue(stress.compare_reference([], truth))
        self.assertEqual(stress.compare_reference([], {"issues": []}), [])

    def test_matrix_covers_all_currency_pairs_states_and_sizes(self):
        stress = load("check_generalization")
        cases = list(stress.matrix())
        self.assertEqual(len({r["seed"] for r in cases}), 1000)
        self.assertEqual(sum("next_day_settlement" in r["config"]["clean_distractors"] for r in cases), 500)
        self.assertEqual(len({tuple(r["config"]["currencies"]) for r in cases}), 20)
        self.assertEqual({r["config"]["company_size"]["ledger_rows_target"] for r in cases}, {240, 800, 2000})
        self.assertTrue(any(sum(r["config"]["issue_counts"].values()) == 12 for r in cases))
        self.assertTrue(any(sum(r["config"]["issue_counts"].values()) == 0 for r in cases))
        with self.assertRaises(ValueError):
            list(stress.matrix(0))

    def test_containerd_digest_and_mutable_tag_cannot_hide_preloaded_image(self):
        probe = load("check_clean_docker")
        images = {"runtime": {"id": "sha256:config", "oci_manifest_digest": "sha256:manifest", "tag": "runtime:release"}}
        self.assertEqual(probe.check(images, lambda _: False)["status"], "passed")
        for identity in images["runtime"].values():
            with self.subTest(identity=identity):
                result = probe.check(images, lambda value: value == identity)
                self.assertEqual(result["status"], "failed")
                self.assertTrue(result["images_present_before_load"]["runtime"])
        self.assertEqual(probe.check({}, lambda _: False)["status"], "failed")
