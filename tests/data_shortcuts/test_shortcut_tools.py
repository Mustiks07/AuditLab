"""Controls for the data-shortcut tools (protocol data-shortcuts-3.0). They test the tools, not the generator.

A planted shortcut must be found and confirmed, noise must not be, and the decision thresholds must hold.
"""
import json, random, shutil, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from review.data_shortcuts import heuristics as h  # noqa: E402

try:
    import judge_adapter as ja
    HAVE_GEN = True
except Exception:  # pragma: no cover
    HAVE_GEN = False


def synthetic(n_fam=40, per=50, seed=1, planted=True):
    rng = random.Random(seed)
    rows = []
    for f in range(n_fam):
        clean = f % 4 == 0
        for i in range(per):
            y = int(not clean and i < 2)
            rows.append({"family": f"F{f}", "episode": f"E{f}", "part": "explore" if f % 2 else "evaluate", "affected": y,
                         "company_has_issue": int(not clean), "planted": (y if planted else rng.random() < 0.04) * 1,
                         "noise": rng.random(), "id_prefix1": rng.choice("ABC")})
    return rows


class Heuristics(unittest.TestCase):
    def test_planted_shortcut_is_selected_and_confirmed(self):
        rows = synthetic()
        ex = [r for r in rows if r["part"] == "explore"]; ho = [r for r in rows if r["part"] == "evaluate"]
        rule = h.select(ex, "planted", "affected")
        e = h.evaluate(ho, "planted", rule, "affected", 500)
        self.assertEqual((e["precision"], e["recall"]), (1.0, 1.0))
        self.assertEqual(e["flags_in_clean_companies"], 0)
        self.assertEqual(h.classify(e, h.evaluate(ex, "planted", rule, "affected", 0), "artificial", None), "confirmed")

    def test_noise_is_not_confirmed(self):
        rows = synthetic(planted=False)
        ex = [r for r in rows if r["part"] == "explore"]; ho = [r for r in rows if r["part"] == "evaluate"]
        for f in ("noise", "id_prefix1", "planted"):
            rule = h.select(ex, f, "affected")
            e = h.evaluate(ho, f, rule, "affected", 300)
            self.assertNotEqual(h.classify(e, h.evaluate(ex, f, rule, "affected", 0), "artificial", None), "confirmed", f)

    def test_too_few_positives_is_only_a_hypothesis(self):
        rows = synthetic(n_fam=12)
        ex = [r for r in rows if r["part"] == "explore"]; ho = [r for r in rows if r["part"] == "evaluate"]
        rule = h.select(ex, "planted", "affected")
        e = h.evaluate(ho, "planted", rule, "affected", 300)
        self.assertLess(e["positives"], 20)
        self.assertEqual(h.classify(e, h.evaluate(ex, "planted", rule, "affected", 0), "artificial", None), "hypothesis")

    def test_context_signal_no_better_than_the_financial_rule_is_not_a_shortcut(self):
        e = {"precision": 0.6, "lift": 30, "recall": 0.5, "positives": 40, "base_rate": 0.02, "precision_ci95": [0.4, 0.8], "flags_in_clean_companies": 3}
        self.assertEqual(h.classify(e, e, "context", {"precision": 0.9}), "nothing_found")
        self.assertEqual(h.classify(e, e, "artificial", {"precision": 0.9}), "confirmed")

    def test_company_level_counts(self):
        rows = synthetic()
        c = h.company_level([r for r in rows if r["part"] == "evaluate"], "planted", ("==", 1))
        self.assertEqual(c["clean_flagged"], 0); self.assertEqual(c["precision"], 1.0)


@unittest.skipUnless(HAVE_GEN, "not_checked: generator unavailable")
class PlanProfiles(unittest.TestCase):
    def test_default_study_and_historical_counterexamples_do_not_share_a_profile_label(self):
        from review.data_shortcuts import datasets
        from review.data_shortcuts.counterexamples import CASES

        rows = datasets.plan_c(Path("unused"), per_cell=1)
        self.assertTrue(any(row["stratum"].startswith("adapter-default:") for row in rows))
        for row in rows:
            if row["stratum"].startswith("adapter-default:"):
                self.assertNotIn("config_version", row["config"])
        self.assertTrue(all(case["config_version"] == "1.0" for name, case in CASES.items() if "profile_1.0" in name))


class OnGeneratedEpisodes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        base = {"config_version": "1.2", "company_size": {"ledger_rows_target": 200, "documents_target": 40}, "currencies": ["KZT", "USD"]}
        ja.generate_episode(9399001, {**base, "allocation_mode": "omitted", "issue_counts": {"payment_allocation": 1},
                                      "financial_case": {"kind": "allocation", "amount_minor": 777777, "applied_bps": 5000, "tranches": 2}}, str(cls.tmp / "omit"))
        ja.generate_episode(9399002, {**base, "issue_counts": {"payment_allocation": 1},
                                      "financial_case": {"kind": "allocation", "amount_minor": 900001, "applied_bps": 4000, "tranches": 2,
                                                         "settlement_offset_minutes": 30}}, str(cls.tmp / "late"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_labels_and_financial_columns(self):
        from review.data_shortcuts.features import records
        rows = records(self.tmp / "omit")
        truth = json.loads((self.tmp / "omit/private/truth.json").read_text())
        ev = {e for i in truth["issues"] for g in i["evidence_sets"] for e in g if not e.startswith(("policy/", "fx/"))}  # records only
        self.assertEqual({r["key"] for r in rows if r["evidence"]}, ev)
        pay = [r for r in rows if r["table"] == "payments" and r["affected"]]
        self.assertEqual(len(pay), 1)
        self.assertEqual((pay[0]["payment_without_application"], pay[0]["index_len"], pay[0]["calc_affected"]), (1, 1, 1))
        self.assertTrue(all(r["calc_affected"] == r["affected"] for r in rows))

    def test_diversity_reads_the_late_tranche(self):
        from review.data_shortcuts.diversity import situation
        cfg = {"financial_case": {"kind": "allocation", "amount_minor": 900001, "applied_bps": 4000, "tranches": 2, "settlement_offset_minutes": 30}}
        s = situation({"dir": str(self.tmp / "late"), "config": cfg, "stratum": "alloc_late_tranche"})
        self.assertEqual(s["status"], "passed")
        self.assertEqual((s["tranches_before"], s["late_offsets_min"], s["alloc_remaining"]), (2, [30], 900001 - 900001 * 4000 // 10000))
        bad = situation({"dir": str(self.tmp / "late"), "config": {"financial_case": {**cfg["financial_case"], "tranches": 3}}, "stratum": "alloc_late_tranche"})
        self.assertEqual(bad["status"], "failed")

    def test_paired_counts_report_differences(self):
        from review.data_shortcuts.companies import FEATURES, paired
        a = {f: 1 for f in FEATURES}; b = dict(a, treasury_journals=2)
        p = paired([{**a, "family": "x", "variant": "case", "stratum": "s"}, {**b, "family": "x", "variant": "defect_off", "stratum": "s"}])
        self.assertEqual((p["treasury_journals"]["error_smaller"], p["ledger_rows"]["equal"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
