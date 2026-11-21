"""The review's own oracle must reproduce every published expectation (it is checked, not trusted).

These tests cover the reviewer's reference scorer and fixtures only. Product conformance is
reported by review/genesis_acceptance/run.py as passed / failed / not_checked.
"""
import json
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from review.genesis_acceptance import fixtures as fx, matcher_checks as mc, reference as ref  # noqa: E402

POLICY = {"allocation": {"deadline": "2026-12-01T00:00:00+05:00"}, "period": {}, "fx_date": "x", "rounding": "ROUND_HALF_UP"}


class PublishedRewardVectors(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.a = json.loads((fx.reward_fixture(self.tmp / "A", POLICY, False) / "private/truth.json").read_text())
        self.ab = json.loads((fx.reward_fixture(self.tmp / "AB", POLICY, True) / "private/truth.json").read_text())
        self.refs = fx.public_refs(self.tmp / "A" / "public")

    def test_published_rewards_follow_the_formula(self):
        for rid, _, _, _, steps, exp in mc.REWARD:
            if "invalid" not in exp:
                with self.subTest(rid=rid):
                    self.assertEqual(ref.reward_from(exp["tp"], exp["fp"], exp["fn"], steps), exp["return"])

    def test_reference_scorer_reproduces_all_twenty_vectors(self):
        truths = {"A": self.a, "AB": self.ab, "-": {**self.a, "issues": []}}
        for rid, _, tkey, findings, steps, exp in mc.REWARD:
            with self.subTest(rid=rid):
                got = ref.score(findings, truths[tkey]["issues"], steps, self.refs)
                if "invalid" in exp:
                    self.assertFalse(got["valid"]); self.assertEqual(got["return"], -1)
                    self.assertIsNone(got["tp"])
                else:
                    for k in ("tp", "fp", "fn", "precision", "recall", "f1", "return"):
                        self.assertEqual(got[k], exp[k], k)

    def test_selected_exact_values(self):
        self.assertEqual(ref.reward_from(1, 1, 0, 10), Fraction(47, 150))
        self.assertEqual(ref.reward_from(1, 19, 0, 10), Fraction(-871, 1050))
        self.assertEqual(ref.reward_from(1, 1, 1, 10), Fraction(-1, 50))


class OpenExamples(unittest.TestCase):
    def test_m_and_extra_cases_against_reference(self):
        tmp = Path(tempfile.mkdtemp())
        gm = fx.m_fixture(tmp / "GM", POLICY)
        truth = json.loads((gm / "private/truth.json").read_text())
        refs = fx.public_refs(gm / "public")
        for cid, _, findings, tau, exp in mc.MCASES + mc.EXTRA:
            with self.subTest(cid=cid):
                got = ref.score(findings, truth["issues"], 10, refs, tau)
                if exp == "invalid":
                    self.assertFalse(got["valid"])
                else:
                    self.assertEqual((got["tp"], got["fp"], got["fn"]), exp)

    def test_tie_break_m10_m11(self):
        issues = [{"issue_id": "A"}, {"issue_id": "B"}]
        self.assertEqual(mc._ref_graph([[0, 1], [0]], issues), [("A", 1), ("B", 0)])
        self.assertEqual(mc._ref_graph([[0, 1], [0, 1]], issues), [("A", 0), ("B", 1)])

    def test_unavailable_evidence_is_never_assumed_absent_by_shape(self):
        tmp = Path(tempfile.mkdtemp())
        gm = fx.m_fixture(tmp / "GM", POLICY)
        refs = fx.public_refs(gm / "public")
        self.assertIn("invoices/DI1", refs)      # D3 exists
        self.assertNotIn("ledger/R404", refs)    # U checked against the data


if __name__ == "__main__":
    unittest.main()
