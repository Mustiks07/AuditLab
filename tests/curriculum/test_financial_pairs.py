from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from auditlab.generator import generate_episode
from auditlab.release.rehearsal import file_hashes
from auditlab.release.transforms import read_json
from auditlab.research.curriculum.financial_pairs import build_financial_variants, check_financial_variant
from review.genesis_acceptance.independent_calc import audit
from tools.check_source_books import check_source_book


BASE = {"config_version": "1.2", "base_currency": "KZT", "currencies": ["KZT"], "timezone": "UTC",
        "target_period": "2035-10", "company_size": {"ledger_rows_target": 240, "documents_target": 76},
        "clean_distractors": ["next_day_settlement"]}


class FinancialPairTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def paired_reports(self, config, seed, *, amount_minor=None):
        baseline = self.root / "baseline"
        generate_episode(seed, config, baseline)
        independent = audit(baseline / "public")
        variants = build_financial_variants(baseline, seed, config, self.root / "controls", amount_minor=amount_minor)
        after_answers = {item.kind: audit(item.snapshot / "public") for item in variants}
        reports = {}
        for item in variants:
            report = check_financial_variant(baseline, item, independent, after_answers[item.kind])
            self.assertEqual(report["status"], "passed", report)
            reports[item.kind] = report
        # Trusted comparisons follow the independent public answer and pair check.
        for snapshot in [baseline, *(item.snapshot for item in variants)]:
            self.assertEqual(check_source_book(snapshot)["status"], "passed", snapshot)
        return baseline, independent, {item.kind: item for item in variants}, after_answers, reports

    def test_variable_amount_and_exact_exclusive_allocation_boundary(self):
        allocation = {"kind": "allocation", "amount_minor": 1_234_567, "applied_bps": 6200,
                      "tranches": 3, "settlement_offset_minutes": 0}
        config = {**BASE, "financial_case": allocation, "issue_counts": {"payment_allocation": 1}}
        baseline, original, variants, after, reports = self.paired_reports(config, 78173, amount_minor=1_926_519)
        self.assertEqual(set(reports), {"id_order", "lawful_distractor", "amount", "period_crossing"})
        amount = reports["amount"]["public_calculation"]
        self.assertEqual(amount["before_allocation"]["balance_minor"], 469_136)
        self.assertEqual(amount["after_allocation"]["balance_minor"], 732_078)
        self.assertEqual(reports["amount"]["expected"][0]["claimed_amount_minor"], 732_078)
        crossing = reports["period_crossing"]["public_calculation"]
        self.assertEqual(crossing["crossing"]["from"]["timestamp"], crossing["before_allocation"]["deadline"])
        self.assertEqual(crossing["after_allocation"]["balance_minor"], 0)
        self.assertEqual(reports["period_crossing"]["expected"], [])
        self.assertEqual(file_hashes(baseline / "private/source_book"),
                         file_hashes(variants["period_crossing"].snapshot / "private/source_book"))
        refund = reports["lawful_distractor"]["public_calculation"]["refund"]
        self.assertEqual(refund["received_minor"], refund["refunded_minor"])
        self.assertEqual(refund["balance_minor"], 0)
        published = read_json(variants["lawful_distractor"].snapshot / "public/manifest.json")
        self.assertEqual((published["ledger_rows"], published["document_count"]), (244, 77))
        altered = deepcopy(after["amount"])
        altered["findings"][0]["claimed_amount_minor"] += 1
        self.assertEqual(check_financial_variant(baseline, variants["amount"], original, altered)["status"], "failed")
        replay = build_financial_variants(baseline, 78173, config, self.root / "replay", amount_minor=1_926_519)
        for item in replay:
            self.assertEqual(file_hashes(item.snapshot), file_hashes(variants[item.kind].snapshot))

    def test_invoice_recognition_crossing_and_amount_are_independent(self):
        config = {**BASE, "financial_case": {"kind": "invoice_cutoff", "amount_minor": 2_468_135,
                                           "issued_before_minutes": 110, "recognition_offset_minutes": 0},
                  "issue_counts": {"period_cutoff": 1}}
        _, _, _, _, reports = self.paired_reports(config, 78233, amount_minor=3_120_507)
        self.assertEqual(reports["amount"]["expected"][0]["claimed_amount_minor"], 3_120_507)
        records = reports["period_crossing"]["public_calculation"]
        before, after = records["before_recognition"], records["after_recognition"]
        self.assertEqual(before["recognized_at"], before["period_end"])
        self.assertFalse(before["recognized_inside_period"])
        self.assertTrue(after["recognized_inside_period"])
        self.assertEqual(before["nominal_minor"], after["nominal_minor"])

    def test_shared_payment_fx_and_allocation_each_remain_when_the_other_is_fixed(self):
        config = {**BASE, "currencies": ["KZT", "USD"],
                  "interaction": "allocation_fx_same_payment",
                  "issue_counts": {"payment_allocation": 1, "fx_conversion": 1}}
        baseline, _, variants, _, reports = self.paired_reports(config, 78129)
        self.assertEqual(set(reports), {"id_order", "lawful_distractor", "fixed", "interaction"})
        fixed = reports["fixed"]["public_calculation"]
        interacting = reports["interaction"]["public_calculation"]
        self.assertEqual([finding["type"] for finding in reports["fixed"]["expected"]], ["payment_allocation"])
        self.assertEqual([finding["type"] for finding in reports["interaction"]["expected"]], ["fx_conversion"])
        self.assertEqual(fixed["before_allocation"]["balance_minor"], fixed["after_allocation"]["balance_minor"])
        self.assertEqual(fixed["after_fx"]["difference_minor"], 0)
        self.assertEqual(interacting["before_fx"], interacting["after_fx"])
        self.assertEqual(interacting["after_allocation"]["balance_minor"], 0)
        self.assertEqual(fixed["shared_payment_id"], interacting["shared_payment_id"])
        for kind in ("fixed", "interaction"):
            self.assertEqual(file_hashes(baseline / "private/source_book"),
                             file_hashes(variants[kind].snapshot / "private/source_book"))

    def test_rejects_unsupported_omission_as_a_crossing_pair(self):
        config = {**BASE, "financial_case": {"kind": "allocation", "amount_minor": 1_234_567,
                    "applied_bps": 6200, "tranches": 2}, "allocation_mode": "omitted",
                  "issue_counts": {"payment_allocation": 1}}
        with self.assertRaisesRegex(ValueError, "Expected a partial delayed allocation"):
            build_financial_variants(self.root / "baseline", 78173, config, self.root / "controls")
        self.assertFalse((self.root / "controls").exists())
