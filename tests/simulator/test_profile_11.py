"""Bounded books and semantically varied audit cases; legacy snapshots stay immutable."""
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from itertools import product
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from auditlab.generator import generate_episode
from auditlab.environment import open_session
from auditlab.generator.core import Company
from test_policies import run_public_policy


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


class Profile11Tests(unittest.TestCase):
    def test_period_and_policy_versions_are_explicit_without_changing_rule_values(self):
        with TemporaryDirectory() as temporary:
            for profile in ("1.0", "1.1"):
                root = Path(temporary) / profile
                generate_episode(14, {"config_version": profile}, root)
                manifest = load(root / "public" / "manifest.json")
                policy = load(root / "public" / "policy.json")
                session = open_session(root / "public")
                def inspect(action, **args):
                    return session.inspect({"action": action, "args": args})["result"]
                rows = inspect("list_records", table="policy", cursor=None, limit=50)["records"]
                if profile == "1.1":
                    self.assertIs(manifest["period_end_exclusive"], True)
                    versions = inspect("get_record", table="policy", id="rule_versions")
                    self.assertEqual(set(versions["rules"]), set(policy) - {"rule_versions"})
                    for row in rows:
                        declared = versions["version"] if row["id"] == "rule_versions" else versions["rules"][row["id"]]
                        self.assertEqual(row["version"], declared)
                else:
                    self.assertNotIn("period_end_exclusive", manifest)
                    self.assertNotIn("rule_versions", policy)
                    self.assertTrue(all(set(row) == {"id", "value"} for row in rows))
                for row in rows:
                    self.assertEqual(inspect("get_record", table="policy", id=row["id"]), row["value"])
                self.assertEqual(inspect("get_record", table="policy", id="rounding"), "ROUND_HALF_UP")
                self.assertIsInstance(inspect("get_record", table="policy", id="fx_date"), str)

    def test_clean_refund_has_source_cash_return_and_no_audit_discrepancy(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "clean-refund"
            generate_episode(14, {"config_version": "1.1", "issue_counts": {},
                "currencies": ["KZT"], "clean_distractors": ["refund"]}, root)
            self.assertEqual(load(root / "private" / "truth.json")["issues"], [])
            session = open_session(root / "public")
            def records(table):
                return session.inspect({"action": "list_records", "args": {
                    "table": table, "cursor": None, "limit": 50}})["result"]["records"]
            [deposit] = [row for row in records("payments") if not row["invoice_refs"]]
            rows = [row for row in records("ledger") if row["payment_id"] == deposit["payment_id"]]
            [receipt] = [row for row in rows if row["account"] == "CASH" and row["debit_minor"]]
            [returned] = [row for row in rows if row["account"] == "CASH" and row["credit_minor"]]
            self.assertEqual(receipt["debit_minor"], deposit["amount_minor"])
            self.assertEqual(returned["credit_minor"], deposit["amount_minor"])
            self.assertGreater(datetime.fromisoformat(returned["timestamp"]), datetime.fromisoformat(receipt["timestamp"]))
            self.assertEqual(sum(row["credit_minor"] - row["debit_minor"] for row in rows if row["account"] == "UNAPPLIED"), 0)
            self.assertEqual(self.assert_reconcile(root)[-1]["args"]["findings"], [])

    def assert_reconcile(self, root):
        actions = run_public_policy("reconcile", 17, root / "public")
        self.assertLessEqual(len(actions), 100)
        findings = actions[-1]["args"]["findings"]
        def key(row, amount):
            return row["type"], tuple(sorted(row["affected_ids"])), row[amount], row["currency"]
        truth = load(root / "private" / "truth.json")["issues"]
        self.assertEqual(Counter(key(row, "claimed_amount_minor") for row in findings),
                         Counter(key(row, "expected_amount_minor") for row in truth))
        for issue in truth:
            [finding] = [f for f in findings if key(f, "claimed_amount_minor") == key(issue, "expected_amount_minor")]
            self.assertLessEqual(len(finding["evidence_ids"]), 10)
            self.assertTrue(any(set(e) <= set(finding["evidence_ids"]) for e in issue["evidence_sets"]))
        return actions

    def test_two_thousand_rows_two_hundred_documents_and_twelve_issues(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "episode"
            generate_episode(7, {"config_version": "1.1",
                "company_size": {"ledger_rows_target": 2000, "documents_target": 200},
                "issue_counts": dict.fromkeys(("duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff"), 3)}, root)
            manifest = load(root / "public" / "manifest.json")
            self.assertEqual((manifest["ledger_rows"], manifest["document_count"]), (2000, 200))
            self.assert_reconcile(root)
            provenance = load(root / "private" / "provenance.json")
            self.assertEqual(provenance["generator_version"], "1.3")
            transfers = [e for e in provenance["events"] if e["kind"] == "treasury_transfer"]
            self.assertTrue(transfers)
            with (root / "public" / "ledger.csv").open(encoding="utf-8", newline="") as stream:
                ledger = list(csv.DictReader(stream))
            journals = defaultdict(list)
            for row in ledger:
                journals[row["journal_id"]].append(row)
            for event in transfers:
                rows = journals[event["journal_id"]]
                self.assertEqual(len(rows), 2)
                self.assertEqual({r["account"] for r in rows}, {"CASH", "CASH_RESERVE"})
                self.assertEqual(sum(int(r["debit_minor"]) - int(r["credit_minor"]) for r in rows), 0)
                self.assertTrue(all(r["invoice_id"] == r["payment_id"] == "null" for r in rows))
            # Treasury moves preserve combined cash and cannot introduce overdrafts.
            balances = defaultdict(int)
            for row in sorted(ledger, key=lambda r: (datetime.fromisoformat(r["timestamp"]), r["journal_id"])):
                if row["account"] in {"CASH", "CASH_RESERVE"} and row["currency"] == "KZT":
                    balances[row["account"]] += int(row["debit_minor"]) - int(row["credit_minor"])
                    self.assertGreaterEqual(balances[row["account"]], 0)

    def test_seed_changes_case_nominals_and_dates_without_breaking_replay(self):
        signatures = defaultdict(set)
        with TemporaryDirectory() as temporary:
            for seed in range(5):
                config = {"config_version": "1.1", "issue_counts": dict.fromkeys(
                    ("duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff"), 1)}
                root = Path(temporary) / str(seed)
                replay = Path(temporary) / f"replay-{seed}"
                generate_episode(seed, config, root)
                generate_episode(seed, config, replay)
                for source in root.rglob("*"):
                    if source.is_file():
                        self.assertEqual(source.read_bytes(), (replay / source.relative_to(root)).read_bytes())
                self.assert_reconcile(root)
                for issue in load(root / "private" / "truth.json")["issues"]:
                    signatures[issue["type"]].add(issue["expected_amount_minor"])
                dates = [d["issued_at"] for d in map(json.loads, (root / "public" / "invoices.jsonl").read_text().splitlines())]
                signatures["dates"].add(tuple(sorted(dates)))
        self.assertTrue(all(len(values) > 1 for values in signatures.values()), signatures)

    def test_interaction_four_states_share_sources_and_independent_effects(self):
        with TemporaryDirectory() as temporary:
            for interaction, kind in (("allocation_fx_same_payment", "payment_allocation"),
                                      ("cutoff_fx_same_invoice", "period_cutoff")):
                sources, values = None, {}
                for first, fx in product((0, 1), repeat=2):
                    root = Path(temporary) / f"{interaction}-{first}{fx}"
                    generate_episode(51, {"config_version": "1.1", "clean_distractors": [],
                        "interaction": interaction, "issue_counts": {kind: first, "fx_conversion": fx}}, root)
                    documents = sorted((json.loads(line)["doc_id"], json.loads(line).get("total_minor"),
                                        json.loads(line).get("amount_minor"))
                                       for name in ("invoices", "payments")
                                       for line in (root / "public" / f"{name}.jsonl").read_text().splitlines())
                    if sources is None:
                        sources = documents
                    self.assertEqual(documents, sources)
                    truth = load(root / "private" / "truth.json")["issues"]
                    self.assertEqual(Counter(row["type"] for row in truth), Counter({kind: first, "fx_conversion": fx}))
                    for row in truth:
                        previous = values.setdefault(row["type"], row["expected_amount_minor"])
                        self.assertEqual(row["expected_amount_minor"], previous)
                    self.assert_reconcile(root)

    def test_new_profile_limits_and_legacy_boundary_pair(self):
        with TemporaryDirectory() as temporary:
            for config in ({"company_size": {"documents_target": 201}},
                           {"issue_counts": dict.fromkeys(("duplicate_invoice", "payment_allocation", "fx_conversion"), 5)}):
                with self.assertRaises(ValueError):
                    generate_episode(7, {"config_version": "1.1", **config}, Path(temporary) / "invalid")
            for mode, amount in (("before", 0), ("after", 2_000_000)):
                root = Path(temporary) / mode
                generate_episode(7, {"config_version": "1.1", "settlement_timing": mode}, root)
                truth = load(root / "private" / "truth.json")["issues"]
                self.assertEqual(sum(i["expected_amount_minor"] for i in truth), amount)
                self.assert_reconcile(root)

    def test_timezone_database_is_pinned_and_dst_boundaries_are_real(self):
        with TemporaryDirectory() as temporary:
            for name, offsets in (("America/New_York", ("-05:00", "-04:00")), ("UTC", ("+00:00", "+00:00"))):
                root = Path(temporary) / name.replace("/", "-")
                generate_episode(11, {"config_version": "1.1", "timezone": name, "target_period": "2026-03",
                    "issue_counts": {"fx_conversion": 2, "period_cutoff": 1}}, root)
                manifest = load(root / "public" / "manifest.json")
                self.assertEqual((manifest["period_start"][-6:], manifest["period_end"][-6:]), offsets)
                self.assert_reconcile(root)
            for zone in ("Missing/Zone", "../UTC", "America//New_York"):
                with self.assertRaisesRegex(ValueError, "invalid_timezone"):
                    generate_episode(11, {"config_version": "1.1", "timezone": zone}, Path(temporary) / "bad")

    def test_balanced_but_wrong_treasury_event_is_rejected(self):
        original = Company.post
        def wrong_account(company, timestamp, currency, amount, debit, credit, *args, **kwargs):
            if debit == "CASH_RESERVE":
                credit = "REVENUE"
            return original(company, timestamp, currency, amount, debit, credit, *args, **kwargs)
        with TemporaryDirectory() as temporary, patch.object(Company, "post", wrong_account):
            with self.assertRaisesRegex(RuntimeError, "inconsistent_treasury_transfer"):
                generate_episode(7, {"config_version": "1.1",
                    "company_size": {"ledger_rows_target": 2000, "documents_target": 200}}, Path(temporary) / "bad")
