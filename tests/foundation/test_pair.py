import csv
import json
import unittest
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from auditlab.contracts import MAX_FINDINGS, MAX_STEPS, PAGE_LIMIT

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "foundation"


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class FoundationTests(unittest.TestCase):
    def test_contract_limits(self):
        self.assertEqual((MAX_STEPS, PAGE_LIMIT, MAX_FINDINGS), (100, 50, 30))

    def test_pair_from_public_records(self):
        for episode, expected_minor in (("S01", 0), ("S02", 2_000_000)):
            with self.subTest(episode=episode):
                public = FIXTURES / episode / "public"
                manifest = load_json(public / "manifest.json")
                payment = load_json(public / "payments.jsonl")
                invoice = load_json(public / "invoices.jsonl")
                self.assertEqual(payment["invoice_refs"], [invoice["invoice_id"]])
                boundary = datetime.fromisoformat(manifest["period_end"])
                with (public / "ledger.csv").open(encoding="utf-8", newline="") as reader:
                    rows = list(csv.DictReader(reader))
                self.assertEqual(manifest["ledger_rows"], len(rows))

                journals = defaultdict(int)
                balance = 0
                movement_ids = []
                for row in rows:
                    net = int(row["credit_minor"]) - int(row["debit_minor"])
                    journals[row["journal_id"], row["currency"]] += net
                    if (row["account"] == "UNAPPLIED" and row["payment_id"] == payment["payment_id"]
                            and datetime.fromisoformat(row["timestamp"]) < boundary):
                        balance += net
                        movement_ids.append(row["row_id"])
                self.assertTrue(all(net == 0 for net in journals.values()))
                self.assertCountEqual(payment["settlement_index"]["unapplied_row_ids"], movement_ids)
                self.assertEqual(balance, expected_minor)
                truth = load_json(FIXTURES / episode / "private" / "truth.json")
                self.assertEqual(truth["episode_id"], manifest["episode_id"])
                self.assertEqual([issue["expected_amount_minor"] for issue in truth["issues"]],
                                 [balance] if balance else [])
                for issue in truth["issues"]:
                    evidence = {"payments/" + payment["doc_id"], "policy/allocation"}
                    evidence.update("ledger/" + row_id for row_id in movement_ids)
                    self.assertTrue(any(set(group) == evidence for group in issue["evidence_sets"]))

    def test_pairs_differ_in_settlement_date_only(self):
        public = [FIXTURES / label / "public" for label in ("S01", "S02")]
        def read_ledger(directory):
            with (directory / "ledger.csv").open(encoding="utf-8", newline="") as reader:
                return {row["row_id"]: row for row in csv.DictReader(reader)}
        left, right = map(read_ledger, public)
        self.assertEqual(left.keys(), right.keys())
        differences = {(key, field) for key in left for field in left[key]
                       if left[key][field] != right[key][field]}
        self.assertEqual(differences, {("R7", "timestamp"), ("R8", "timestamp")})
