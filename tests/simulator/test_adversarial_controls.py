"""Matched controls must require reading the financial facts to resolve a candidate."""
import json
import unittest
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from auditlab.generator import generate_episode
from auditlab.verifier import verify_report
from auditlab.verifier.crosscheck import compare
import test_clean_counterpart
from test_policies import run_public_policy


class AdversarialControlTests(unittest.TestCase):
    def assert_findings(self, root, kind, count):
        public = root / "public"
        report = verify_report(public, engine="sqlite")
        actions = run_public_policy("reconcile", 17, public)
        self.assertLessEqual(len(actions), 100)
        findings = actions[-1]["args"]["findings"]
        truth = json.loads((root / "private/truth.json").read_bytes())
        self.assertEqual(report["notes"], [])
        self.assertEqual(compare(report, truth, public), [])
        self.assertEqual(Counter(f["type"] for f in findings), Counter({kind: count}))
        for issue in truth["issues"]:
            [finding] = [f for f in findings if sorted(f["affected_ids"]) == issue["affected_ids"]]
            self.assertEqual((finding["claimed_amount_minor"], finding["currency"]),
                             (issue["expected_amount_minor"], issue["currency"]))
            self.assertTrue(any(set(group) <= set(finding["evidence_ids"]) for group in issue["evidence_sets"]))

    def test_archive_and_valuation_candidate_counts_survive_zero_one_and_five_faults(self):
        common = {"config_version": "1.1", "timezone": "UTC", "target_period": "2028-11",
                  "company_size": {"ledger_rows_target": 240, "documents_target": 76}}
        records = test_clean_counterpart.CleanCounterpartTests.records
        with TemporaryDirectory() as directory:
            for kind in ("duplicate_invoice", "fx_conversion"):
                original = None
                for faults in (0, 1, 5):
                    with self.subTest(kind=kind, faults=faults):
                        root = Path(directory) / f"{kind}-{faults}"
                        generate_episode(82460601, {**common, "issue_counts": {kind: faults}}, root)
                        public = root / "public"
                        docs = tuple((public / name).read_bytes()
                                     for name in ("invoices.jsonl", "payments.jsonl", "fx.csv"))
                        ledger = records(public, "ledger")
                        manifest = json.loads((public / "manifest.json").read_bytes())
                        self.assertEqual((manifest["ledger_rows"], manifest["document_count"]), (240, 76))
                        copies = Counter(row["invoice_id"] for row in records(public, "invoices"))
                        archives = {key for key, count in copies.items() if count == 2}
                        valuations = {row["row_id"] for row in ledger if row["account"] == "FX_VALUATION"}
                        self.assertEqual(len(archives), 6)
                        self.assertGreaterEqual(len(valuations), 6)
                        if original is None:
                            original = (docs, valuations)
                        self.assertEqual((docs, valuations), original)
                        recognized = Counter(row["invoice_id"] for row in ledger
                                             if row["account"] == "AR" and row["debit_minor"] > 0)
                        self.assertEqual(sum(recognized[key] == 2 for key in archives),
                                         faults if kind == "duplicate_invoice" else 0)
                        self.assert_findings(root, kind, faults)

    def test_boundary_motifs_have_clean_and_wrong_recognitions(self):
        records = test_clean_counterpart.CleanCounterpartTests.records
        with TemporaryDirectory() as directory:
            for interaction in (None, "cutoff_fx_same_invoice"):
                for faults in (0, 1):
                    with self.subTest(interaction=interaction, faults=faults):
                        root = Path(directory) / f"{interaction}-{faults}"
                        generate_episode(82460601, {"config_version": "1.1", "timezone": "UTC",
                            "target_period": "2028-11", "interaction": interaction,
                            "issue_counts": {"period_cutoff": faults}}, root)
                        public = root / "public"
                        manifest = json.loads((public / "manifest.json").read_bytes())
                        end = datetime.fromisoformat(manifest["period_end"])
                        invoices = {row["invoice_id"]: row for row in records(public, "invoices")}
                        ledger = records(public, "ledger")
                        recognitions = [row for row in ledger if row["account"] == "AR" and row["debit_minor"]
                                        and datetime.fromisoformat(row["timestamp"]) >= end]
                        self.assertTrue(any(datetime.fromisoformat(invoices[r["invoice_id"]]["issued_at"]) >= end
                                            for r in recognitions))
                        self.assertEqual(sum(datetime.fromisoformat(invoices[r["invoice_id"]]["issued_at"]) < end
                                             for r in recognitions), faults)
                        foreign = [i for i in invoices.values() if i["currency"] != manifest["base_currency"]
                                   and end - timedelta(days=1) <= datetime.fromisoformat(i["issued_at"]) < end]
                        self.assertEqual(len(foreign), 2)
                        foreign_keys = {i["invoice_id"] for i in foreign}
                        self.assertEqual(sum(r["invoice_id"] in foreign_keys for r in recognitions),
                                         faults if interaction else 0)
                        self.assert_findings(root, "period_cutoff", faults)
