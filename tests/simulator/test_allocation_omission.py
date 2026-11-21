import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from auditlab.generator import generate_episode
from review.genesis_acceptance.independent_calc import audit
import test_clean_counterpart
from test_policies import run_public_policy
from tools.check_source_books import check_source_book


class AllocationOmissionTests(unittest.TestCase):
    def test_complete_omission_partial_and_clean(self):
        records = test_clean_counterpart.CleanCounterpartTests.records
        with TemporaryDirectory() as directory:
            for seed, currency, size in ((40703, "KZT", {}), (40729, "JPY", {
                "ledger_rows_target": 240, "documents_target": 76,
            })):
                choices = {}
                for name, mode, count in (("omitted", "omitted", 1), ("clean", "omitted", 0),
                                          ("partial", "partial", 1)):
                    with self.subTest(seed=seed, mode=name):
                        snapshot = Path(directory) / f"{seed}-{name}"
                        generate_episode(seed, {"config_version": "1.1", "base_currency": currency,
                            "currencies": [currency], "clean_distractors": ["next_day_settlement"],
                            "company_size": size, "allocation_mode": mode,
                            "issue_counts": {"payment_allocation": count}}, snapshot)
                        public = snapshot / "public"
                        reviewer = audit(public)
                        self.assertEqual(reviewer["notes"], [])
                        payments = {row["payment_id"]: row for row in records(public, "payments")}
                        ledger = {row["row_id"]: row for row in records(public, "ledger")}
                        ordinary_actions = run_public_policy("reconcile", 17, public)
                        self.assertLessEqual(len(ordinary_actions), 100)
                        for findings in (reviewer["findings"], ordinary_actions[-1]["args"]["findings"]):
                            self.assertEqual(len(findings), count)
                            for finding in findings:
                                self.assertEqual(finding["type"], "payment_allocation")
                                self.assertEqual(finding["currency"], currency)
                                _, payment_id = next(a for a in finding["affected_ids"] if a.startswith("payment/")).split("/")
                                payment = payments[payment_id]
                                index = payment["settlement_index"]
                                self.assertLess(datetime.fromisoformat(payment["timestamp"]), datetime.fromisoformat(index["before"]))
                                receipts = [row for row in ledger.values() if row["payment_id"] == payment_id]
                                applied = sum(row["debit_minor"] for row in receipts if row["account"] == "UNAPPLIED")
                                amount = payment["amount_minor"]
                                self.assertEqual(applied, 0 if name == "omitted" else amount * 3 // 5)
                                self.assertEqual(finding["claimed_amount_minor"], amount - applied)
                                if name == "omitted":
                                    self.assertEqual(len(receipts), 2)  # only the genuine receipt
                                    self.assertEqual(len(index["unapplied_row_ids"]), 1)
                        truth = json.loads((snapshot / "private/truth.json").read_bytes())
                        self.assertEqual([i["expected_amount_minor"] for i in truth["issues"]],
                                         [f["claimed_amount_minor"] for f in reviewer["findings"]])
                        self.assertEqual(check_source_book(snapshot)["status"], "passed")
                        if name == "omitted":
                            changes = json.loads((snapshot / "private/source_transition.json").read_bytes())["changes"]
                            removed = [c["before"] for c in changes if c["table"] == "ledger" and c["after"] is None]
                            self.assertEqual({r["account"] for r in removed}, {"AR", "UNAPPLIED"})
                            self.assertEqual(len({r["journal_id"] for r in removed}), 1)
                        choices[name] = (len(ledger), set(payments), (public / "invoices.jsonl").read_bytes())
                self.assertEqual(choices["omitted"], choices["clean"])

    def test_excludes_conflicting_scenarios(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "unused"
            config = {"config_version": "1.1", "allocation_mode": "omitted"}
            for change, error in (({"config_version": "1.0"}, "invalid_allocation_mode"),
                                  ({"settlement_timing": "before"}, "incompatible_allocation_mode"),
                                  ({"interaction": "allocation_fx_same_payment"}, "incompatible_allocation_mode")):
                with self.assertRaisesRegex(ValueError, error):
                    generate_episode(40703, {**config, **change}, output)
            self.assertFalse(output.exists())
