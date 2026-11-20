"""Independent public fixtures for DST folds, exact halves and a misleading shortcut.

These cases are handwritten and never call the generator or read private truth. Both SQL
engines and the ordinary policy must recover the stated arithmetic from public documents.
"""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from cases import Case, dump
from test_timezones import rewrite_all_instants_as_utc
from auditlab.environment import open_session
from auditlab.policies import make_policy
from auditlab.verifier import verify_report

ENGINES = ("sqlite", "duckdb") if importlib.util.find_spec("duckdb") else ("sqlite",)


def finding_key(finding):
    return (finding["type"], tuple(sorted(finding["affected_ids"])),
            finding["claimed_amount_minor"], finding["currency"],
            tuple(sorted(finding["evidence_ids"])))


def fold_case(root, base, source, amount, rate, expected):
    case = Case("FOLD-HALF", base=base)
    # Same wall-clock hour, distinct instants: 05:30Z and 06:30Z on 1 November.
    events = ("2026-11-01T01:30:00-04:00", "2026-11-01T01:30:00-05:00")
    for index, (event, recorded) in enumerate(zip(events, (expected, expected - 1)), 1):
        invoice, entity = f"I{index}", f"C{index}"
        case.invoice(f"D{index}", invoice, event, amount, source, entity)
        case.journal(event, ("AR", amount, 0), ("REVENUE", 0, amount),
                     currency=source, invoice=invoice, entity=entity)
        case.journal(event, ("FX_VALUATION", recorded, 0), ("FX_OFFSET", 0, recorded),
                     currency=base, invoice=invoice, entity=entity)
    for day, quote in (("2026-10-31", "1"), ("2026-11-01", rate), ("2026-11-02", "4")):
        case.rate(day, source, base, quote)
    public = case.write(root)
    start, end = "2026-11-01T00:00:00-04:00", "2026-12-01T00:00:00-05:00"
    manifest = json.loads((public / "manifest.json").read_text())
    manifest.update(timezone="America/New_York", period_start=start, period_end=end)
    dump(public / "manifest.json", manifest)
    policy = json.loads((public / "policy.json").read_text())
    policy["allocation"]["deadline"] = end
    dump(public / "policy.json", policy)
    return public


def allocation_shortcut_case(root, applied):
    case = Case("SAME-SHORTCUT")
    issued, received = "2026-11-13T10:00:00+05:00", "2026-11-14T10:00:00+05:00"
    case.invoice("D4", "I4", issued, 10_000)
    case.journal(issued, ("AR", 10_000, 0), ("REVENUE", 0, 10_000), invoice="I4")
    case.payment("D9", "P9", received, 10_000, ["I4"])
    case.journal(received, ("CASH", 10_000, 0), ("UNAPPLIED", 0, 10_000),
                 invoice="I4", payment="P9")
    case.journal("2026-11-14T10:01:00+05:00", ("UNAPPLIED", applied, 0), ("AR", 0, applied),
                 invoice="I4", payment="P9")
    return case.write(root)


class PublicFinancialEdgeTests(unittest.TestCase):
    def assert_public_answers(self, public, expected):
        self.assertFalse((public.parent / "private").exists())
        expected_keys = sorted(map(finding_key, expected))
        for engine in ENGINES:
            with self.subTest(engine=engine):
                report = verify_report(public, engine=engine)
                self.assertEqual(report["notes"], [])
                self.assertEqual(sorted(map(finding_key, report["findings"])), expected_keys)

        # An ordinary policy sees only the public action responses, no files or answer.
        session = open_session(public)
        manifest = json.loads((public / "manifest.json").read_text())
        observation = {"manifest": manifest, "remaining_steps": 100}
        policy, observed = make_policy("reconcile", 17), set()
        for step in range(1, 101):
            action = policy.act(observation)
            if action["action"] == "submit":
                findings = action["args"]["findings"]
                self.assertEqual(sorted(map(finding_key, findings)), expected_keys)
                for finding in findings:
                    self.assertTrue(set(finding["evidence_ids"]) <= observed)
                break
            self.assertEqual(action["action"], "list_records")
            reply = session.inspect(action)
            self.assertTrue(reply["ok"], reply)
            table = action["args"]["table"]
            for record in reply["result"]["records"]:
                if table == "fx":
                    key = "/".join(record[field] for field in ("date", "source_currency", "target_currency"))
                else:
                    key = record[{"ledger": "row_id", "invoices": "doc_id",
                                  "payments": "doc_id", "policy": "id"}[table]]
                observed.add(f"{table}/{key}")
            observation = {**reply, "remaining_steps": 100 - step}
        else:
            self.fail("ordinary policy did not submit within the 100-action budget")

    def test_repeated_dst_hour_and_half_up_in_zero_two_three_decimal_bases(self):
        # Hand calculations, independent of the production converters:
        # 1 JPY * 2.505 KZT/JPY * 100 = 250.5 -> 251 KZT minor.
        # 1000 KWD minor / 1000 * 2.5 JPY/KWD = 2.5 -> 3 JPY minor.
        # 1 JPY * 1.0005 KWD/JPY * 1000 = 1000.5 -> 1001 KWD minor.
        # Banker's rounding would incorrectly mark I1 and accept I2 in each case.
        inputs = (("KZT", "JPY", 1, "2.505", 251),
                  ("JPY", "KWD", 1000, "2.5", 3),
                  ("KWD", "JPY", 1, "1.0005", 1001))
        for base, source, amount, rate, expected in inputs:
            with self.subTest(base=base, source=source), tempfile.TemporaryDirectory() as temp:
                public = fold_case(Path(temp), base, source, amount, rate, expected)
                findings = [{"type": "fx_conversion", "affected_ids": ["invoice/I2"],
                             "claimed_amount_minor": 1, "currency": base,
                             "evidence_ids": ["invoices/D2", "ledger/R7", "policy/fx_date",
                                              f"fx/2026-11-01/{source}/{base}"]}]
                invoices = [json.loads(line) for line in (public / "invoices.jsonl").read_text().splitlines()]
                first, second = (datetime.fromisoformat(row["issued_at"]) for row in invoices)
                self.assertEqual(first.replace(tzinfo=None), second.replace(tzinfo=None))
                self.assertEqual(second - first, timedelta(hours=1))
                self.assertEqual(first.astimezone(timezone.utc).hour, 5)
                self.assertEqual(second.astimezone(timezone.utc).hour, 6)
                for utc in (False, True):
                    with self.subTest(utc=utc):
                        if utc:
                            rewrite_all_instants_as_utc(public)
                        self.assert_public_answers(public, findings)

    def test_same_document_shortcut_can_mean_clean_or_unallocated(self):
        for applied, expected in ((10_000, 0), (6_000, 4_000)):
            with self.subTest(applied=applied), tempfile.TemporaryDirectory() as temp:
                public = allocation_shortcut_case(Path(temp), applied)
                invoice = json.loads((public / "invoices.jsonl").read_text())
                payment = json.loads((public / "payments.jsonl").read_text())
                self.assertEqual(payment["invoice_refs"], [invoice["invoice_id"]])
                self.assertEqual(len(payment["settlement_index"]["unapplied_row_ids"]), 2)
                self.assertEqual(datetime.fromisoformat(payment["timestamp"])
                                 - datetime.fromisoformat(invoice["issued_at"]), timedelta(days=1))
                # Same shortcut in both cases; only actual credits minus debits decides.
                findings = [] if not expected else [{
                    "type": "payment_allocation", "affected_ids": ["payment/P9", "invoice/I4"],
                    "claimed_amount_minor": expected, "currency": "KZT",
                    "evidence_ids": ["payments/D9", "ledger/R4", "ledger/R5", "policy/allocation"],
                }]
                self.assert_public_answers(public, findings)


if __name__ == "__main__":
    unittest.main()
