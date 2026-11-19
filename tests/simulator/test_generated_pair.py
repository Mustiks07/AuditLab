import shutil
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from auditlab.environment import open_session
from auditlab.generator import generate_episode


class GeneratedPairTests(unittest.TestCase):
    def test_same_seed_snapshots_bind_cursors_to_public_content(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for label, timing in (("before", "before"), ("replay", "before"), ("after", "after")):
                generate_episode(817, {"settlement_timing": timing, "clean_distractors": []}, root / label)
            for path in (root / "before").glob("*/*"):
                self.assertEqual(path.read_bytes(), (root / "replay" / path.parent.name / path.name).read_bytes())
            first = open_session(root / "before" / "public")
            args = {"table": "ledger", "limit": 1, "cursor": None}
            cursor = first.inspect({"action": "list_records", "args": args})["result"]["next_cursor"]
            next_page = {"action": "list_records", "args": {**args, "cursor": cursor}}
            self.assertTrue(open_session(root / "replay" / "public").inspect(next_page)["ok"])
            self.assertFalse(open_session(root / "after" / "public").inspect(next_page)["ok"])

    def test_boundary_from_public_tools(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            histories = {}
            for timing, minute, expected in (("before", -1, 0), ("at", 0, 2_000_000), ("after", 1, 2_000_000)):
                with self.subTest(timing=timing):
                    episode = root / timing
                    generate_episode(817, {"settlement_timing": timing, "clean_distractors": []}, episode)
                    isolated_public = root / f"auditor-{timing}" / "public"
                    shutil.copytree(episode / "public", isolated_public)
                    shutil.rmtree(episode)
                    session = open_session(isolated_public)

                    def inspect(action, **args):
                        reply = session.inspect({"action": action, "args": args})
                        self.assertTrue(reply["ok"], reply)
                        self.assertNotIn("remaining_steps", reply)
                        return reply["result"]

                    page = inspect("list_records", table="payments", cursor=None, limit=10)
                    payment = inspect("get_document", doc_id=page["records"][0]["doc_id"])
                    invoices = inspect("list_records", table="invoices", cursor=None, limit=10)
                    invoice = inspect("get_document", doc_id=invoices["records"][0]["doc_id"])
                    self.assertEqual(payment["invoice_refs"], [invoice["invoice_id"]])
                    self.assertEqual(payment["amount_minor"], 5_000_000)
                    policy = inspect("get_record", table="policy", id="allocation")
                    self.assertEqual(payment["settlement_index"]["before"], policy["deadline"])
                    deadline = datetime.fromisoformat(policy["deadline"])
                    rows = inspect("list_records", table="ledger", cursor=None, limit=50)["records"]
                    index = payment["settlement_index"]["unapplied_row_ids"]
                    movements = [inspect("get_record", table="ledger", id=key) for key in index]
                    self.assertCountEqual(index, [row["row_id"] for row in rows if row["account"] == "UNAPPLIED"
                                                  and datetime.fromisoformat(row["timestamp"]) < deadline])
                    expression = "+".join(f"({row['credit_minor']}-{row['debit_minor']})" for row in movements)
                    self.assertEqual(int(inspect("calculator", expression=expression)), expected)
                    final = next(row for row in rows if row["account"] == "UNAPPLIED" and row["debit_minor"] == 2_000_000)
                    self.assertEqual(datetime.fromisoformat(final["timestamp"]), deadline + timedelta(minutes=minute))
                    self.assertFalse(session.inspect({"action": "get_record", "args": {"table": "private", "id": "truth"}})["ok"])
                    histories[timing] = [{key: value for key, value in row.items() if key != "timestamp"} for row in rows]
            self.assertEqual(histories["before"], histories["after"])
