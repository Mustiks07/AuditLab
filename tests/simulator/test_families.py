import csv
import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from auditlab.release.transforms import transform_public
from datasets.audit_families import check_published_and_internal, check_splits, fingerprints


FOUNDATION = Path(__file__).resolve().parents[2] / "fixtures" / "foundation"


class EconomicsFamilyTests(unittest.TestCase):
    def test_transformed_twin_crosses_split_and_late_variant_stays_in_family(self):
        before = FOUNDATION / "S01" / "public"
        after = FOUNDATION / "S02" / "public"
        with TemporaryDirectory() as directory:
            transformed = Path(directory) / "transformed"
            transform_public(after, transformed, seed=812)
            self.assertEqual(fingerprints(after), fingerprints(transformed))
            self.assertEqual(fingerprints(before)["family"], fingerprints(after)["family"])
            self.assertNotEqual(fingerprints(before)["book"], fingerprints(after)["book"])
            report = check_splits({"train": {"after": after}, "dev": {"before": before, "renamed": transformed}})
            self.assertEqual(report["cross"]["family"]["train/dev"], [["after", "before"], ["after", "renamed"]])
            self.assertEqual(report["cross"]["book"]["train/dev"], [["after", "renamed"]])

    def test_archive_copy_and_second_commercial_invoice_are_distinct(self):
        original = FOUNDATION / "S01" / "public"
        with TemporaryDirectory() as directory:
            public = Path(directory) / "public"
            shutil.copytree(original, public)
            manifest_path = public / "manifest.json"
            manifest = json.loads(manifest_path.read_bytes())
            manifest["document_count"] += 1
            manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            invoices = public / "invoices.jsonl"
            invoice = json.loads(invoices.read_bytes())
            archive = dict(invoice, doc_id="DArchive")
            invoices.write_text("\n".join(map(json.dumps, (invoice, archive))) + "\n", encoding="utf-8")
            self.assertEqual(fingerprints(original)["family"], fingerprints(public)["family"])

            actual_second_invoice = dict(archive, invoice_id="IAnother")
            invoices.write_text("\n".join(map(json.dumps, (invoice, actual_second_invoice))) + "\n", encoding="utf-8")
            ledger = public / "ledger.csv"
            with ledger.open(encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                header, rows = reader.fieldnames, list(reader)
            extra = [dict(row, row_id=f"another-{row['row_id']}", invoice_id="IAnother", journal_id="JAnother")
                     for row in rows if row["payment_id"] == "null"]
            with ledger.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=header)
                writer.writeheader()
                writer.writerows(rows + extra)
            manifest["ledger_rows"] += len(extra)
            manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            self.assertNotEqual(fingerprints(original)["family"], fingerprints(public)["family"])

    def test_published_internal_split_reports_known_twins_only_inside_their_split(self):
        report = check_published_and_internal()
        self.assertEqual(report["counts"], {"train": 10, "dev": 10, "internal": 20})
        for matches in report["cross"].values():
            self.assertEqual(matches, {"train/dev": [], "train/internal": [], "dev/internal": []})
        self.assertEqual({split: len(pairs) for split, pairs in report["within"]["family"].items()},
                         {"train": 1, "dev": 1, "internal": 0})
