import json
import unittest
from datetime import datetime
from fractions import Fraction
from itertools import product
from pathlib import Path
from tempfile import TemporaryDirectory

from auditlab.environment import open_session
from auditlab.generator import generate_episode


class ScenarioTests(unittest.TestCase):
    def call(self, session, action, **args):
        reply = session.inspect({"action": action, "args": args})
        self.assertTrue(reply["ok"], reply)
        return reply["result"]

    def records(self, session, table):
        records = []
        cursor = None
        while True:
            page = self.call(session, "list_records", table=table, cursor=cursor, limit=50)
            records.extend(page["records"])
            cursor = page["next_cursor"]
            if cursor is None:
                return records

    def snapshot(self, output, **config):
        generate_episode(31, {"clean_distractors": [], **config}, output)
        public = output / "public"
        manifest = json.loads((public / "manifest.json").read_text(encoding="utf-8"))
        return open_session(public), manifest

    def assert_truth(self, output, session, expected):
        truth = json.loads((output / "private" / "truth.json").read_text(encoding="utf-8"))
        actual = {}
        for issue in truth["issues"]:
            self.assertNotIn(issue["type"], actual)
            actual[issue["type"]] = (issue["currency"], issue["expected_amount_minor"])
            for group in issue["evidence_sets"]:
                for reference in group:
                    table, key = reference.split("/", 1)
                    self.call(session, "get_record", table=table, id=key)
        self.assertEqual(actual, expected)

    def fx_result(self, session, manifest, ledger):
        [valuation] = [row for row in ledger if row["account"] == "FX_VALUATION"]
        payment_id = valuation["payment_id"]
        if payment_id:
            [source] = [doc for doc in self.records(session, "payments") if doc["payment_id"] == payment_id]
            source_at, nominal = source["timestamp"], source["amount_minor"]
        else:
            [source] = [doc for doc in self.records(session, "invoices")
                        if doc["invoice_id"] == valuation["invoice_id"]]
            source_at, nominal = source["issued_at"], source["total_minor"]
        source_at = datetime.fromisoformat(source_at)
        local_zone = datetime.fromisoformat(manifest["period_start"]).tzinfo
        local_day = source_at.astimezone(local_zone).date()
        currency = source["currency"]
        base_currency = manifest["base_currency"]
        quote = self.call(session, "get_record", table="fx", id=f"{local_day}/{currency}/{base_currency}")
        exponents = manifest["currency_exponents"]
        scaled = Fraction(nominal, 10 ** exponents[currency]) * Fraction(quote["rate"]) * 10 ** exponents[base_currency]
        correct = (2 * scaled.numerator + scaled.denominator) // (2 * scaled.denominator)
        return source, source_at, local_day, correct, valuation["debit_minor"]

    def test_both_interaction_matrices(self):
        cases = (("allocation_fx_same_payment", "payment_allocation", 2_000, 50_000),
                 ("cutoff_fx_same_invoice", "period_cutoff", 10_000, 100_000))
        with TemporaryDirectory() as directory:
            for case, type_name, first_amount, fx_amount in cases:
                common_row_ids = None
                for first, fx in product((False, True), repeat=2):
                    with self.subTest(case=case, first=first, fx=fx):
                        output = Path(directory) / f"{case}-{int(first)}{int(fx)}"
                        session, manifest = self.snapshot(
                            output, interaction=case, issue_counts={type_name: int(first), "fx_conversion": int(fx)},
                        )
                        boundary = datetime.fromisoformat(manifest["period_end"])
                        ledger = self.records(session, "ledger")
                        row_ids = {row["row_id"] for row in ledger}
                        if common_row_ids is None:
                            common_row_ids = row_ids
                        self.assertEqual(row_ids, common_row_ids)
                        source, source_at, _, correct, booked = self.fx_result(session, manifest, ledger)
                        self.assertEqual(booked - correct, fx_amount if fx else 0)
                        if type_name == "payment_allocation":
                            referenced = source["settlement_index"]["unapplied_row_ids"]
                            balances = [row for row in ledger if row["row_id"] in referenced]
                            self.assertTrue(all(row["payment_id"] == source["payment_id"] and
                                                datetime.fromisoformat(row["timestamp"]) < boundary for row in balances))
                            amount = sum(row["credit_minor"] - row["debit_minor"] for row in balances)
                        else:
                            [recognition] = [row for row in ledger if row["account"] == "AR"]
                            self.assertEqual(source["invoice_id"], recognition["invoice_id"])
                            self.assertLess(source_at, boundary)
                            amount = source["total_minor"] if datetime.fromisoformat(recognition["timestamp"]) >= boundary else 0
                        self.assertEqual(amount, first_amount if first else 0)
                        expected = {}
                        if amount:
                            expected[type_name] = ("USD", amount)
                        if booked != correct:
                            expected["fx_conversion"] = ("KZT", booked - correct)
                        self.assert_truth(output, session, expected)

    def test_archive_copies_need_two_recognitions_to_be_a_duplicate(self):
        with TemporaryDirectory() as directory:
            for enabled in (False, True):
                with self.subTest(enabled=enabled):
                    output = Path(directory) / str(enabled)
                    session, _ = self.snapshot(
                        output, issue_counts={"duplicate_invoice": int(enabled)},
                        clean_distractors=[] if enabled else ["archive_copy"],
                    )
                    first, second = self.records(session, "invoices")
                    self.assertNotEqual(first["doc_id"], second["doc_id"])
                    self.assertEqual(first["invoice_id"], second["invoice_id"])
                    self.assertEqual(first["entity_id"], second["entity_id"])
                    postings = [row for row in self.records(session, "ledger") if row["account"] == "AR"]
                    self.assertEqual(len(postings), 1 + int(enabled))
                    self.assertTrue(all(row["invoice_id"] == first["invoice_id"] for row in postings))
                    excess = sum(row["debit_minor"] for row in postings) - first["total_minor"]
                    self.assertEqual(excess, 1_200_000 if enabled else 0)
                    self.assert_truth(output, session, {"duplicate_invoice": ("KZT", excess)} if excess else {})

    def test_fx_uses_local_calendar_and_rounds_once(self):
        with TemporaryDirectory() as directory:
            examples = (("JPY", 0, 25_301, 26_311), ("USD", 2, 5_000_000, 5_100_000),
                        ("KWD", 3, 16_006_601, 16_106_611))
            for currency, exponent, expected, recorded in examples:
                with self.subTest(currency=currency):
                    output = Path(directory) / currency
                    session, manifest = self.snapshot(
                        output, currencies=["KZT", currency], issue_counts={"fx_conversion": 1},
                    )
                    self.assertEqual(manifest["currency_exponents"][currency], exponent)
                    _, source_at, local_day, correct, booked = self.fx_result(session, manifest, self.records(session, "ledger"))
                    self.assertEqual((source_at.day, local_day.day), (13, 14))
                    self.assertEqual((correct, booked), (expected, recorded))
                    self.assert_truth(output, session, {"fx_conversion": ("KZT", recorded - expected)})
