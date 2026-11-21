import csv
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from auditlab.generator import generate_episode
from auditlab.release.transforms import read_json
from auditlab.research.curriculum.families import boundary_variants
from auditlab.research.curriculum.laboratory import local_runner, _teacher
from auditlab.evaluator.schema import validate_submission
from review.genesis_acceptance.independent_calc import audit
from tools.check_source_books import check_source_book


class BoundaryFamilyTests(unittest.TestCase):
    def test_public_families_keep_expected_changes_and_source_transitions(self):
        config = {"config_version": "1.1", "base_currency": "KZT", "currencies": ["KZT"],
                  "target_period": "2032-08", "settlement_timing": "after",
                  "issue_counts": {"payment_allocation": 1}, "clean_distractors": ["next_day_settlement"],
                  "company_size": {"ledger_rows_target": 240, "documents_target": 76}}
        with TemporaryDirectory() as temp:
            root = Path(temp)
            seed = 41161
            parent = root / "p"
            generate_episode(seed, config, parent)
            variants = boundary_variants(parent, seed, config, root / "variants")
            self.assertEqual({v["kind"] for v in variants}, {"id_order", "lawful_refund", "fixed", "currency"})
            reconcile = local_runner("reconcile", "test-sha", "test")
            for variant in variants:
                with self.subTest(kind=variant["kind"]):
                    path = variant["snapshot"]
                    publication = read_json(path / "public/manifest.json")
                    self.assertEqual((publication["ledger_rows"], publication["document_count"]), (240, 76))
                    self.assertEqual(check_source_book(path)["status"], "passed")
                    answer = audit(path / "public")
                    self.assertEqual(answer["notes"], [])
                    fields = ("type", "affected_ids", "claimed_amount_minor", "currency")
                    self.assertEqual([{k: f[k] for k in fields} for f in answer["findings"]],
                                     variant["expected"])
                    teacher = _teacher(answer)["submission"]
                    validate_submission(teacher, publication["episode_id"])
                    receipt = reconcile(path, root / variant["kind"], 1, 60)
                    self.assertEqual(receipt["status"], "submitted")
                    self.assertFalse(receipt["failures"])
                    self.assertLessEqual(receipt["actions"], 100)
            renamed = variants[0]["snapshot"] / "public"
            self.assertNotEqual((renamed / "ledger.csv").read_bytes(), (parent / "public/ledger.csv").read_bytes())
            self.assertRegex(read_json(renamed / "manifest.json")["episode_id"], r"^E[A-F0-9]{32}$")
            for row in csv.DictReader((renamed / "ledger.csv").read_text(encoding="utf-8").splitlines()):
                self.assertRegex(row["row_id"], r"^R[A-F0-9]{24}$")
            for line in (renamed / "payments.jsonl").read_text(encoding="utf-8").splitlines():
                self.assertRegex(json.loads(line)["payment_id"], r"^P[A-F0-9]{24}$")
            added_refunds = (variants[1]["snapshot"] / "private/provenance.json")
            source = read_json(parent / "private/provenance.json")
            self.assertGreater(sum(e["kind"] == "refund" for e in read_json(added_refunds)["events"]),
                               sum(e["kind"] == "refund" for e in source["events"]))
