"""Public manual examples; these do not certify the independent SQL verifier."""
import csv
from datetime import datetime, timezone
import itertools
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "foundation"


def read_rows(label):
    with (FIXTURES / label / "public" / "ledger.csv").open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


class PublicControlPairTests(unittest.TestCase):
    def test_boundary_order_and_timezone(self):
        """36 manual calculations: before/exactly at/after x 6 orders x 2 offsets."""
        rows = [r for r in read_rows("S02") if r["account"] == "UNAPPLIED"]
        boundary = datetime.fromisoformat("2026-12-01T00:00:00+05:00")
        cases = (("2026-11-30T23:59:00+05:00", 0),
                 ("2026-12-01T00:00:00+05:00", 2_000_000),
                 ("2026-12-01T00:01:00+05:00", 2_000_000))
        for instant, expected in cases:
            for order in itertools.permutations(rows):
                for utc in (False, True):
                    with self.subTest(instant=instant, order=[r["row_id"] for r in order], utc=utc):
                        balance = 0
                        for row in order:
                            timestamp = instant if row["row_id"] == "R7" else row["timestamp"]
                            parsed = datetime.fromisoformat(timestamp)
                            if utc:
                                parsed = datetime.fromisoformat(parsed.astimezone(timezone.utc).isoformat())
                            if parsed < boundary:
                                balance += int(row["credit_minor"]) - int(row["debit_minor"])
                        self.assertEqual(balance, expected)

    def test_public_index_proves_completeness_on_each_side(self):
        for label, expected in (("S01", 0), ("S02", 2_000_000)):
            with self.subTest(label=label):
                public = FIXTURES / label / "public"
                payment = json.loads((public / "payments.jsonl").read_text())
                manifest = json.loads((public / "manifest.json").read_text())
                index = payment["settlement_index"]
                self.assertEqual(index["before"], manifest["period_end"])
                rows = [r for r in read_rows(label) if r["account"] == "UNAPPLIED"
                        and r["payment_id"] == payment["payment_id"]
                        and datetime.fromisoformat(r["timestamp"]) < datetime.fromisoformat(index["before"])]
                self.assertCountEqual(index["unapplied_row_ids"], [r["row_id"] for r in rows])
                self.assertEqual(sum(int(r["credit_minor"]) - int(r["debit_minor"]) for r in rows), expected)


if __name__ == "__main__":
    unittest.main()
