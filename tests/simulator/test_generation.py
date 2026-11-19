import json
import unittest
from collections import Counter
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from auditlab.environment import open_session
from auditlab.generator import generate_episode
from auditlab.generator.core import Company


class GenerationTests(unittest.TestCase):
    def test_clean_history_has_refunds_split_payments_and_equal_amounts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "clean"
            generate_episode(14, {}, root)
            session = open_session(root / "public")

            def records(table):
                args = {"table": table, "cursor": None, "limit": 50}
                return session.inspect({"action": "list_records", "args": args})["result"]["records"]

            ledger = {row["row_id"]: row for row in records("ledger")}
            payments = records("payments")
            self.assertIn(2, Counter(key for payment in payments for key in payment["invoice_refs"]).values())
            invoices = records("invoices")
            self.assertTrue(any(a["entity_id"] != b["entity_id"] and a["total_minor"] == b["total_minor"]
                                for a in invoices for b in invoices))
            for payment in payments:
                movements = [ledger[key] for key in payment["settlement_index"]["unapplied_row_ids"]]
                self.assertEqual(sum(row["credit_minor"] - row["debit_minor"] for row in movements), 0)
            refund = next(payment for payment in payments if not payment["invoice_refs"])
            self.assertTrue(any(row["account"] == "CASH" and row["payment_id"] == refund["payment_id"]
                                and row["credit_minor"] == refund["amount_minor"] for row in ledger.values()))
            truth = json.loads((root / "private" / "truth.json").read_text(encoding="utf-8"))
            self.assertEqual(truth["issues"], [])

    def test_balanced_but_wrong_source_is_rejected_before_publication(self):
        post = Company.post

        def post_late_recognition(company, timestamp, currency, amount, debit, credit, *args, **kwargs):
            if (debit, credit) == ("AR", "REVENUE"):
                timestamp = company.period_end + timedelta(minutes=1)
            return post(company, timestamp, currency, amount, debit, credit, *args, **kwargs)

        with TemporaryDirectory() as directory, patch.object(Company, "post", post_late_recognition):
            root = Path(directory) / "invalid"
            with self.assertRaisesRegex(RuntimeError, "accounting_disagrees_with_truth"):
                generate_episode(14, {}, root)
            self.assertFalse(root.exists())
