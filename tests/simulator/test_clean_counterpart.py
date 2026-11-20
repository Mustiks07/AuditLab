"""Generated clean data must defeat the observed row-count/date shortcut."""
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from auditlab.environment import open_session
from auditlab.generator import generate_episode
from auditlab.verifier import verify_report
from test_policies import run_public_policy
from tools.check_source_books import check_source_book


class CleanCounterpartTests(unittest.TestCase):
    @staticmethod
    def records(public, table):
        session = open_session(public)
        result, cursor = [], None
        while True:
            reply = session.inspect({"action": "list_records", "args": {
                "table": table, "cursor": cursor, "limit": 50}})
            assert reply["ok"], reply
            result.extend(reply["result"]["records"])
            cursor = reply["result"]["next_cursor"]
            if cursor is None:
                return result

    def test_shortcut_fires_on_clean_and_defective_but_financial_auditors_distinguish(self):
        nominals, dates, identifiers = set(), set(), set()
        with TemporaryDirectory() as temporary:
            for seed in range(10):
                source_primary = None
                for defect in (0, 1):
                    with self.subTest(seed=seed, defect=defect):
                        root = Path(temporary) / f"{seed}-{defect}"
                        generate_episode(seed, {"config_version": "1.1", "currencies": ["KZT"],
                            "clean_distractors": ["next_day_settlement"],
                            "issue_counts": {"payment_allocation": defect}}, root)
                        public = root / "public"
                        invoices = {r["invoice_id"]: r for r in self.records(public, "invoices")}
                        payments = self.records(public, "payments")
                        ledger = {r["row_id"]: r for r in self.records(public, "ledger")}
                        # Exact research shortcut, using only accessible public records.
                        candidates = [p for p in payments if len(p["invoice_refs"]) == 1
                            and len(p["settlement_index"]["unapplied_row_ids"]) == 2
                            and datetime.fromisoformat(p["timestamp"]) - datetime.fromisoformat(
                                invoices[p["invoice_refs"][0]]["issued_at"]) == timedelta(days=1)]
                        primary = {table: (public / f"{table}.jsonl").read_bytes()
                                   for table in ("invoices", "payments")}
                        if source_primary is None:
                            source_primary = primary
                        self.assertEqual(primary, source_primary)
                        self.assertEqual(len(candidates), 6)
                        wrong, clean = [], []
                        for payment in candidates:
                            rows = [ledger[key] for key in payment["settlement_index"]["unapplied_row_ids"]]
                            remaining = sum(r["credit_minor"] - r["debit_minor"] for r in rows)
                            (wrong if remaining else clean).append(payment)
                        self.assertEqual((len(clean), len(wrong)), (6 - defect, defect))
                        nominals.update(paid["amount_minor"] for paid in clean)
                        dates.update(paid["timestamp"] for paid in clean)
                        identifiers.update(paid["payment_id"] for paid in clean)
                        sql = verify_report(public)
                        ordinary = run_public_policy("reconcile", 17, public)[-1]["args"]["findings"]
                        self.assertEqual(sql["notes"], [])
                        for findings in (sql["findings"], ordinary):
                            self.assertEqual(len(findings), defect)
                            self.assertTrue(all(f"payment/{paid['payment_id']}" not in f["affected_ids"]
                                                for f in findings for paid in clean))
                            if defect:
                                self.assertIn(f"payment/{wrong[0]['payment_id']}", findings[0]["affected_ids"])
                        self.assertEqual(check_source_book(root)["status"], "passed")
        self.assertGreater(len(nominals), 1)
        self.assertGreater(len(dates), 1)
        self.assertEqual(len(identifiers), 60)  # stable within each pair, distinct across seeds

    def test_option_is_explicit_profile_11_only_and_replays_exactly(self):
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = {"config_version": "1.1", "clean_distractors": ["next_day_settlement"]}
            generate_episode(57, config, base / "first")
            generate_episode(57, config, base / "again")
            for path in (base / "first").rglob("*"):
                if path.is_file():
                    self.assertEqual(path.read_bytes(), (base / "again" / path.relative_to(base / "first")).read_bytes())
            with self.assertRaisesRegex(ValueError, "invalid_clean_distractors"):
                generate_episode(57, {**config, "config_version": "1.0"}, base / "legacy")
            self.assertFalse((base / "legacy").exists())
            generate_episode(57, {"config_version": "1.1"}, base / "default")
            default = json.loads((base / "default/private/provenance.json").read_bytes())
            self.assertNotIn("next_day_settlement", default["config"].get("clean_distractors", []))
