import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auditlab.contracts import MAX_STEPS, PAGE_LIMIT
from auditlab.environment import PUBLIC_TABLES, open_session


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "foundation"


def inspect(session, action, **args):
    return session.inspect({"action": action, "args": args})


class PublicSessionTests(unittest.TestCase):
    def setUp(self):
        self.public = FIXTURES / "S02" / "public"
        self.session = open_session(self.public)

    def test_public_only_copy_supports_settlement_reconciliation(self):
        for episode, expected in (("S01", "0"), ("S02", "2000000")):
            with self.subTest(episode=episode), tempfile.TemporaryDirectory() as temp:
                public = shutil.copytree(FIXTURES / episode / "public", Path(temp) / "public")
                self.assertFalse((public.parent / "private").exists())
                with patch.object(Path, "open", autospec=True, side_effect=Path.open) as opened:
                    session = open_session(public)
                    page = inspect(session, "list_records", table="payments", cursor=None, limit=50)
                    payment = inspect(session, "get_document", doc_id=page["result"]["records"][0]["doc_id"])
                    invoice = inspect(session, "list_records", table="invoices", cursor=None, limit=50)
                    rule = inspect(session, "get_record", table="policy", id="allocation")
                    rows = [
                        inspect(session, "get_record", table="ledger", id=row_id)["result"]
                        for row_id in payment["result"]["settlement_index"]["unapplied_row_ids"]
                    ]
                    expression = "+".join(f"({row['credit_minor']}-{row['debit_minor']})" for row in rows)
                    result = inspect(session, "calculator", expression=expression)
                self.assertEqual(result, {"ok": True, "result": expected})
                self.assertEqual(payment["result"]["invoice_refs"], [invoice["result"]["records"][0]["invoice_id"]])
                self.assertEqual(rule["result"]["deadline"], payment["result"]["settlement_index"]["before"])
                self.assertEqual(
                    {call.args[0] for call in opened.call_args_list},
                    {public / name for name in (
                        "manifest.json", "ledger.csv", "invoices.jsonl", "payments.jsonl", "fx.csv", "policy.json",
                    )},
                )

    def test_pages_are_stable_complete_and_bound_to_table_and_episode(self):
        with tempfile.TemporaryDirectory() as temp:
            public = shutil.copytree(self.public, Path(temp) / "public")
            ledger = public / "ledger.csv"
            with ledger.open(encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                fields = reader.fieldnames
                examples = list(reader)
            first = inspect(self.session, "list_records", table="ledger", cursor=None, limit=1)
            cursor = first["result"]["next_cursor"]
            for session, table in ((open_session(FIXTURES / "S01" / "public"), "ledger"), (self.session, "policy")):
                reply = inspect(session, "list_records", table=table, cursor=cursor, limit=1)
                self.assertEqual(reply["error"]["code"], "invalid_cursor")

            rows = [dict(row, row_id=f"R{index:03d}") for index, row in enumerate(examples * 8)]
            with ledger.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(reversed(rows))
            session = open_session(public)
            cursor = None
            all_ids = []
            for limit, length in ((1, 1), (PAGE_LIMIT, 50), (PAGE_LIMIT, 13)):
                args = {"table": "ledger", "cursor": cursor, "limit": limit}
                page = inspect(session, "list_records", **args)["result"]
                self.assertEqual(inspect(open_session(public), "list_records", **args)["result"], page)
                self.assertEqual(len(page["records"]), length)
                all_ids.extend(row["row_id"] for row in page["records"])
                cursor = page["next_cursor"]
            self.assertIsNone(cursor)
            self.assertEqual(all_ids, [row["row_id"] for row in rows])
            empty = inspect(session, "list_records", table="fx", cursor=None, limit=1)
            self.assertEqual(empty, {"ok": True, "result": {"records": [], "next_cursor": None}})

    def test_public_references_return_json_types_and_independent_results(self):
        self.assertEqual(PUBLIC_TABLES, ("ledger", "invoices", "payments", "fx", "policy"))
        ledger = inspect(self.session, "get_record", table="ledger", id="R1")["result"]
        self.assertEqual(ledger["debit_minor"], 5_000_000)
        self.assertIs(type(ledger["debit_minor"]), int)
        self.assertIsNone(ledger["payment_id"])
        listed = inspect(self.session, "list_records", table="policy", cursor=None, limit=50)["result"]["records"]
        for record in listed:
            self.assertEqual(inspect(self.session, "get_record", table="policy", id=record["id"])["result"], record["value"])

        request = {"doc_id": "D9"}
        result = inspect(self.session, "get_document", **request)["result"]
        result["settlement_index"]["unapplied_row_ids"].clear()
        for _ in range(MAX_STEPS + 1):
            reply = inspect(self.session, "get_document", **request)
            self.assertEqual(set(reply), {"ok", "result"})
            self.assertEqual(reply["result"]["settlement_index"]["unapplied_row_ids"], ["R4", "R5"])
        json.dumps(reply, allow_nan=False)

        with tempfile.TemporaryDirectory() as temp:
            public = shutil.copytree(self.public, Path(temp) / "public")
            with (public / "fx.csv").open("a", encoding="utf-8") as stream:
                stream.write("2026-11-15,USD,KZT,500.005\n")
            reply = inspect(open_session(public), "get_record", table="fx", id="2026-11-15/USD/KZT")
            self.assertEqual(reply["result"]["rate"], "500.005")

    def test_bad_requests_have_stable_errors_without_echoing_input(self):
        marker = "../private/truth.json"
        bad_requests = (
            ({"action": "get_record", "args": {"table": marker, "id": "truth"}}, "unknown_table"),
            ({"action": "get_record", "args": {"table": "ledger", "id": marker}}, "not_found"),
            ({"action": "get_document", "args": {"doc_id": marker}}, "not_found"),
            ({"action": marker, "args": {}}, "unknown_action"),
            ({"action": "get_document", "args": []}, "invalid_action"),
            ({"action": "get_document", "args": {"doc_id": {}}}, "invalid_args"),
            ({"action": "get_document", "args": {"doc_id": "D9", "extra": marker}}, "invalid_args"),
        )
        for action, code in bad_requests:
            with self.subTest(action=action):
                reply = self.session.inspect(action)
                self.assertEqual(set(reply), {"ok", "error"})
                self.assertFalse(reply["ok"])
                self.assertEqual(reply["error"]["code"], code)
                self.assertNotIn(marker, json.dumps(reply))
                self.assertEqual(reply, self.session.inspect(action))
        for limit in (0, PAGE_LIMIT + 1, True, 1.0, "1"):
            reply = inspect(self.session, "list_records", table="ledger", cursor=None, limit=limit)
            self.assertEqual(reply["error"]["code"], "invalid_args")
        for cursor in (marker, 0, "a" * 10_000):
            reply = inspect(self.session, "list_records", table="ledger", cursor=cursor, limit=1)
            self.assertEqual(reply["error"]["code"], "invalid_cursor")

    def test_calculator_uses_bounded_decimal_arithmetic(self):
        for expression, expected in (
            ("0.1 + 0.2", "0.3"),
            (" -(2+3*4) / +.5 ", "-28"),
            ("50*510 - 50*500", "500"),
            ("-0.00", "0"),
            ("1/8", "0.125"),
        ):
            with self.subTest(expression=expression):
                self.assertEqual(inspect(self.session, "calculator", expression=expression), {"ok": True, "result": expected})
        for expression, code in (
            ("0/0", "division_by_zero"),
            ("2/(2-2)", "division_by_zero"),
            ("2**3", "invalid_expression"),
            ("1e2", "invalid_expression"),
            ("NaN", "invalid_expression"),
            ("1.2.3", "invalid_expression"),
            ("2(3)", "invalid_expression"),
            ("(1", "invalid_expression"),
            ("".join(["("] * 25 + ["1"] + [")"] * 25), "invalid_expression"),
            (" " * 256 + "1", "invalid_expression"),
            ("9" * 64 + "*10", "arithmetic_limit"),
            ("1/" + "9" * 64 + "/" + "9" * 64, "arithmetic_limit"),
        ):
            with self.subTest(expression=expression):
                reply = inspect(self.session, "calculator", expression=expression)
                self.assertEqual(reply["error"]["code"], code)
