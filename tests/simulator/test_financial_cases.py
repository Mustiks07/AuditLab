"""Profile 1.2 makes the economic inputs explicit while preserving matched books."""

import csv
from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from auditlab.generator import generate_episode
from auditlab.release.rehearsal import file_hashes
from review.genesis_acceptance.independent_calc import audit
from test_policies import run_public_policy
from tools.check_source_books import check_source_book


def _json(path):
    return json.loads(path.read_bytes())


def _payments(public):
    return [json.loads(line) for line in (public / "payments.jsonl").read_bytes().splitlines()]


def _ledger(public):
    with (public / "ledger.csv").open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _posted_balance(public, payment_id):
    manifest = _json(public / "manifest.json")
    cutoff = datetime.fromisoformat(manifest["period_end"])
    movement = [row for row in _ledger(public)
                if row["payment_id"] == payment_id and row["account"] == "UNAPPLIED"
                and datetime.fromisoformat(row["timestamp"]) < cutoff]
    return sum(int(row["credit_minor"]) - int(row["debit_minor"]) for row in movement), movement


def _non_treasury_rows(public):
    ledger = _ledger(public)
    treasury = {row["journal_id"] for row in ledger if row["account"] == "CASH_RESERVE"}
    return {row["row_id"]: row for row in ledger if row["journal_id"] not in treasury}


def _findings_signature(findings, amount="claimed_amount_minor"):
    return sorted((f["type"], sorted(f["affected_ids"]), f["currency"], f[amount]) for f in findings)


class FinancialCaseTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def pair(self, seed, case, issue, **config):
        original = {"config_version": "1.2", "financial_case": case, "clean_distractors": [], **config}
        snapshots = []
        for label, count in (("clean", 0), ("faulty", 1)):
            snapshot = self.root / f"{seed}-{label}"
            generate_episode(seed, {**original, "issue_counts": {issue: count}}, snapshot)
            snapshots.append(snapshot)
        clean, faulty = snapshots
        clean_source, faulty_source = (snapshot / "private/source_book" for snapshot in snapshots)
        if config.get("allocation_mode") == "omitted":
            self.assertEqual(_non_treasury_rows(clean_source), _non_treasury_rows(faulty_source))
            same_files = {"fx.csv", "invoices.jsonl", "payments.jsonl", "policy.json"}
            self.assertEqual({key: value for key, value in file_hashes(clean_source).items() if key in same_files},
                             {key: value for key, value in file_hashes(faulty_source).items() if key in same_files})
        else:
            self.assertEqual(file_hashes(clean_source), file_hashes(faulty_source))
        return clean, faulty

    def assert_reviewers_agree(self, snapshot, expected):
        public = snapshot / "public"
        independent = audit(public)
        actions = run_public_policy("reconcile", 791, public)
        self.assertEqual(independent["notes"], [])
        self.assertLessEqual(len(actions), 100)
        for findings in (independent["findings"], actions[-1]["args"]["findings"]):
            self.assertEqual(_findings_signature(findings), expected)
        truth = _json(snapshot / "private/truth.json")
        self.assertEqual(_findings_signature(truth["issues"], "expected_amount_minor"), expected)
        self.assertEqual(check_source_book(snapshot)["status"], "passed")

    def test_standalone_allocation_amounts_ratios_and_tranches(self):
        for seed, currency, mode, amount, ratio, tranches, offset in (
            (71053, "JPY", "partial", 46_197, 4_301, 3, None),
            (71071, "KZT", "partial", 6_749_611, 7_389, 2, 0),
            (71093, "KWD", "omitted", 2_710_047, 6_000, 3, None),
        ):
            with self.subTest(mode=mode, currency=currency, boundary=offset):
                case = {"kind": "allocation", "amount_minor": amount, "applied_bps": ratio, "tranches": tranches}
                if offset is not None:
                    case["settlement_offset_minutes"] = offset
                clean, faulty = self.pair(
                    seed, case, "payment_allocation", base_currency=currency, currencies=[currency],
                    allocation_mode=mode, company_size={"ledger_rows_target": 200, "documents_target": 45},
                    clean_distractors=["refund", "next_day_settlement"],
                )
                amount_payments = [payment for payment in _payments(faulty / "public")
                                   if payment["amount_minor"] == amount and payment["invoice_refs"]]
                self.assertEqual(len(amount_payments), 2)
                by_balance = {_posted_balance(faulty / "public", payment["payment_id"])[0]: payment
                              for payment in amount_payments}
                applied = 0 if mode == "omitted" else amount * ratio // 10_000
                remaining = amount - applied
                self.assertEqual(set(by_balance), {0, remaining})
                payment = by_balance[remaining]
                payment_id = payment["payment_id"]
                affected = [f"invoice/{payment['invoice_refs'][0]}", f"payment/{payment_id}"]
                clean_balance, clean_movements = _posted_balance(clean / "public", payment_id)
                faulty_balance, faulty_movements = _posted_balance(faulty / "public", payment_id)
                self.assertEqual((clean_balance, faulty_balance), (0, remaining))
                self.assertEqual(len(clean_movements), 1 + tranches + int(offset is not None))
                self.assertEqual(len(faulty_movements), 1 if mode == "omitted" else 1 + tranches)
                clean_manifest = _json(clean / "public/manifest.json")
                faulty_manifest = _json(faulty / "public/manifest.json")
                self.assertEqual(clean_manifest["document_count"], faulty_manifest["document_count"])
                self.assertEqual(clean_manifest["ledger_rows"], faulty_manifest["ledger_rows"])
                self.assertEqual(set(payment["settlement_index"]["unapplied_row_ids"]),
                                 {row["row_id"] for row in faulty_movements})
                if offset is not None:
                    late, = [row for row in _ledger(faulty / "public") if row["payment_id"] == payment_id
                             and row["account"] == "UNAPPLIED" and row["row_id"] not in
                             payment["settlement_index"]["unapplied_row_ids"]]
                    self.assertEqual(late["timestamp"], faulty_manifest["period_end"])
                    self.assertEqual(int(late["debit_minor"]), remaining)
                    original, = [row for row in clean_movements if row["row_id"] == late["row_id"]]
                    self.assertEqual({**original, "timestamp": late["timestamp"]}, late)
                if mode == "omitted":
                    movement_rows = [row for row in _ledger(faulty / "public") if row["payment_id"] == payment_id]
                    self.assertEqual({row["account"] for row in movement_rows}, {"CASH", "UNAPPLIED"})
                    self.assertEqual(len(movement_rows), 2)
                    clean_padding = [r for r in _ledger(clean / "public") if r["account"] == "CASH_RESERVE"]
                    faulty_padding = [r for r in _ledger(faulty / "public") if r["account"] == "CASH_RESERVE"]
                    self.assertEqual(len(faulty_padding) - len(clean_padding), tranches)
                refunds = [doc for doc in _payments(clean / "public") if not doc["invoice_refs"]]
                self.assertTrue(any(_posted_balance(clean / "public", doc["payment_id"])[0] == 0 and
                                    len(_posted_balance(clean / "public", doc["payment_id"])[1]) == 2
                                    for doc in refunds))
                self.assert_reviewers_agree(clean, [])
                self.assert_reviewers_agree(faulty, [("payment_allocation", affected, currency, remaining)])

    def test_cutoff_exact_endpoint_and_wrong_early_recognition(self):
        for seed, issue_offset, posting_offset, next_period in ((71119, 35, 0, False), (71129, 300, 17, True)):
            amount = 395_761 + seed
            case = {"kind": "invoice_cutoff", "amount_minor": amount, "issued_before_minutes": issue_offset,
                    "recognition_offset_minutes": posting_offset, "issued_next_period": next_period}
            clean, faulty = self.pair(seed, case, "period_cutoff", clean_distractors=["refund"])
            manifest = _json(faulty / "public/manifest.json")
            self.assertTrue(manifest["period_end_exclusive"])
            cutoff = datetime.fromisoformat(manifest["period_end"])
            document, = [json.loads(line) for line in (faulty / "public/invoices.jsonl").read_bytes().splitlines()
                         if json.loads(line)["total_minor"] == amount]
            posting, = [row for row in _ledger(faulty / "public") if row["invoice_id"] == document["invoice_id"]
                        and row["account"] == "AR" and int(row["debit_minor"]) > 0]
            sign = 1 if next_period else -1
            self.assertEqual(datetime.fromisoformat(document["issued_at"]), cutoff + timedelta(minutes=sign * issue_offset))
            self.assertEqual(datetime.fromisoformat(posting["timestamp"]), cutoff - timedelta(minutes=sign * posting_offset))
            self.assertEqual(int(posting["debit_minor"]), amount)
            self.assert_reviewers_agree(clean, [])
            self.assert_reviewers_agree(faulty, [("period_cutoff", [f"invoice/{document['invoice_id']}"], "KZT", amount)])

    def test_profile_is_reproducible_without_publishing_control_configuration(self):
        config = {"config_version": "1.2", "financial_case": {"kind": "allocation", "amount_minor": 837_173,
                  "applied_bps": 6_913, "tranches": 2, "settlement_offset_minutes": 83},
                  "issue_counts": {"payment_allocation": 1}}
        first, second = self.root / "first", self.root / "second"
        generate_episode(71143, config, first)
        generate_episode(71143, config, second)
        self.assertEqual(file_hashes(first), file_hashes(second))
        provenance = _json(first / "private/provenance.json")
        self.assertEqual(provenance["generator_version"], "1.8")
        self.assertEqual(provenance["config"], config)
        public = _json(first / "public/manifest.json")
        self.assertTrue(public["period_end_exclusive"])
        self.assertNotIn("financial_case", public)
        self.assertNotIn("generator_version", public)

    def test_fx_histories_use_varied_application_patterns_and_boundary_offsets(self):
        patterns, offsets, ordinary = set(), set(), set()
        for seed in (71201, 71209, 71237):
            snapshots = {}
            for name, interaction, count in (
                ("combined", "allocation_fx_same_payment", 1),
                ("fixed-allocation", "allocation_fx_same_payment", 0),
                ("ordinary-fx", None, 0),
            ):
                snapshot = self.root / f"{seed}-{name}"
                generate_episode(seed, {
                    "config_version": "1.2", "issue_counts": {"payment_allocation": count, "fx_conversion": 1},
                    "interaction": interaction, "clean_distractors": [],
                }, snapshot)
                snapshots[name] = snapshot
            combined, fixed, standalone = (
                snapshots[name] for name in ("combined", "fixed-allocation", "ordinary-fx")
            )
            self.assertEqual(file_hashes(combined / "private/source_book"), file_hashes(fixed / "private/source_book"))
            public = combined / "public"
            payment, = _payments(public)
            payment_id = payment["payment_id"]
            balance, before = _posted_balance(public, payment_id)
            self.assertGreater(balance, 0)
            patterns.add(len(before) - 1)
            cutoff = datetime.fromisoformat(_json(public / "manifest.json")["period_end"])
            late, = [r for r in _ledger(public) if r["payment_id"] == payment_id and r["account"] == "UNAPPLIED"
                     and datetime.fromisoformat(r["timestamp"]) >= cutoff]
            offsets.add(datetime.fromisoformat(late["timestamp"]) - cutoff)
            for name, expected in (("combined", ["fx_conversion", "payment_allocation"]),
                                   ("fixed-allocation", ["fx_conversion"]), ("ordinary-fx", ["fx_conversion"])):
                case_public = snapshots[name] / "public"
                observed = audit(case_public)
                self.assertEqual(observed["notes"], [])
                self.assertEqual(sorted(item["type"] for item in observed["findings"]), expected)
                actions = run_public_policy("reconcile", 791, case_public)
                self.assertLessEqual(len(actions), 100)
                self.assertEqual(_findings_signature(observed["findings"]),
                                 _findings_signature(actions[-1]["args"]["findings"]))
            self.assertEqual(check_source_book(combined)["status"], "passed")
            self.assertEqual(check_source_book(fixed)["status"], "passed")
            self.assertEqual(check_source_book(standalone)["status"], "passed")
            ordinary_public = standalone / "public"
            other, = _payments(ordinary_public)
            remaining, moves = _posted_balance(ordinary_public, other["payment_id"])
            self.assertEqual(remaining, 0)
            applications = sorted((row for row in moves if int(row["debit_minor"]) > 0),
                                  key=lambda row: row["timestamp"])
            initial = sum(int(row["debit_minor"]) for row in applications[:-1])
            self.assertNotEqual(initial, other["amount_minor"] * 3 // 5)
            ordinary.add(len(applications))
        self.assertGreaterEqual(len(patterns), 2)
        self.assertGreaterEqual(len(offsets), 2)
        self.assertGreaterEqual(len(ordinary), 2)

    def test_configuration_rejects_unsupported_combinations(self):
        case = {"kind": "allocation", "amount_minor": 100, "tranches": 3}
        config = {"config_version": "1.2", "financial_case": case}
        for invalid, condition in (
            ({"config_version": "1.1"}, "requires config_version 1.2"),
            ({"financial_case": {**case, "applied_bps": 100}}, "at least one minor unit"),
            ({"financial_case": {**case, "amount_minor": True}}, "amount_minor"),
            ({"financial_case": {**case, "settlement_offset_minutes": 1}, "allocation_mode": "omitted"},
             "omission cannot also delay"),
            ({"issue_counts": {"duplicate_invoice": 1}}, "zero or one issue of its own type"),
            ({"settlement_timing": "before"}, "cannot combine"),
        ):
            output = self.root / "invalid"
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, condition):
                generate_episode(71147, {**config, **invalid}, output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
