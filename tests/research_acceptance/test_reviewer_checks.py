"""Tests of the reviewer's own checks (Claude Pro 2, #43). They test the checks, not the product."""
import csv, io, json, random, shutil, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from review.agent_generalization import compare  # noqa: E402

try:
    import tzdata  # noqa: F401
    import judge_adapter as ja
    HAVE_GEN = True
except Exception:  # noqa: BLE001
    HAVE_GEN = False

CFG = {"config_version": "1.1", "company_size": {"ledger_rows_target": 200, "documents_target": 40}, "base_currency": "KZT",
       "currencies": ["KZT", "USD"], "timezone": "Asia/Almaty", "target_period": "2026-11", "interaction": None,
       "issue_counts": {"duplicate_invoice": 0, "payment_allocation": 1, "fx_conversion": 1, "period_cutoff": 0},
       "clean_distractors": ["split_payment", "refund", "archive_copy"]}


class FamilyStatistics(unittest.TestCase):
    def test_bootstrap_interval_and_permutation(self):
        rng = random.Random(1)
        a = {f"f{i}": 0.5 + rng.gauss(0, 0.1) for i in range(60)}
        b = {k: v - 0.3 + rng.gauss(0, 0.05) for k, v in a.items()}
        strong = compare.paired(a, b, resamples=2000)
        self.assertTrue(strong["ci95"][0] > 0.2 and strong["ci95"][1] < 0.4 and strong["p_value"] < 0.01 and strong["shown"])
        null = compare.paired(a, {k: v + rng.gauss(0, 0.05) for k, v in a.items()}, resamples=2000)
        self.assertLessEqual(null["ci95"][0], 0); self.assertGreaterEqual(null["ci95"][1], -0.05)

    def test_repetitions_are_not_independent_families(self):
        rows = [{"family_id": "f1", "score": {"excluded": False, "return": 1}} for _ in range(3)] + \
               [{"family_id": "f2", "score": {"excluded": False, "return": -1}}]
        vals = compare.family_values(rows)
        self.assertEqual(vals, {"f1": 1.0, "f2": -1.0})  # three repeats collapse into one family value

    def test_missing_families_are_reported_not_imputed(self):
        r = compare.paired({"a": 1.0, "b": 0.0}, {"a": 0.0}, resamples=200)
        self.assertEqual(r["families"], 1); self.assertEqual(r["missing_in_b"], ["b"])
        self.assertIsNone(r['ci95']);self.assertIsNone(r['p_value']);self.assertFalse(r['shown'])

    def test_one_family_cannot_produce_a_zero_width_confidence_interval(self):
        r=compare.paired({'one':1.0},{'one':-1.0},resamples=200)
        self.assertEqual(r['difference'],2.0)
        self.assertIsNone(r['ci95']);self.assertFalse(r['shown'])


@unittest.skipUnless(HAVE_GEN, "not_checked: tzdata/judge_adapter unavailable")
class ValidatorControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from review.agent_generalization.validator import clean_variant
        cls.tmp = Path(tempfile.mkdtemp())
        ja.generate_episode(9399, {"team_config": CFG}, str(cls.tmp / "err"))
        clean_variant(cls.tmp / "err", cls.tmp / "clean")

    def copy(self, name):
        d = self.tmp / f"{name}-{random.random()}"
        shutil.copytree(self.tmp / "err", d)
        return d

    def validate(self, d):
        from review.agent_generalization.validator import validate_candidate
        return validate_candidate(d, adapter=ja, work=d.parent / (d.name + ".v"))

    def test_genuine_pair_passes(self):
        self.assertEqual(self.validate(self.tmp / "err")["status"], "passed")
        # the clean variant is derived from the source book: it has no provenance, so P4 and P10 are
        # honestly not_checked (protocol independent-assurance-2.0) and every other check passes
        r = self.validate(self.tmp / "clean")
        self.assertEqual(r["status"], "not_checked")
        self.assertEqual({k for k, c in r["checks"].items() if c["status"] != "passed"}, {"reproducible_generation", "mutation_declared"})

    def test_hidden_document_is_a_task_defect(self):
        d = self.copy("hidden")
        truth = json.loads((d / "private/truth.json").read_text())
        doc = next(r.split("/", 1)[1] for i in truth["issues"] for g in i["evidence_sets"] for r in g if r.startswith("payments/"))
        lines = [l for l in (d / "public/payments.jsonl").read_text().splitlines() if json.loads(l)["doc_id"] != doc]
        (d / "public/payments.jsonl").write_text("\n".join(lines) + "\n")
        r = self.validate(d)
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["checks"]["evidence_available"]["status"], "failed")
        self.assertEqual(r["checks"]["witness_trajectory"]["status"], "failed")

    def test_unexplained_change_in_the_book_is_rejected(self):
        d = self.copy("unbalanced")
        rows = list(csv.DictReader(io.StringIO((d / "public/ledger.csv").read_text())))
        i = next(k for k, r in enumerate(rows) if int(r["debit_minor"]) > 0)
        rows[i]["debit_minor"] = str(int(rows[i]["debit_minor"]) + 1)
        buf = io.StringIO(); w = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n"); w.writeheader(); w.writerows(rows)
        (d / "public/ledger.csv").write_text(buf.getvalue())
        r = self.validate(d)
        self.assertEqual(r["checks"]["mutation_explained"]["status"], "failed")
        self.assertEqual(r["status"], "failed")

    def test_wrong_truth_amount_is_caught_after_the_own_answer(self):
        d = self.copy("truth")
        truth = json.loads((d / "private/truth.json").read_text()); truth["issues"][0]["expected_amount_minor"] += 1
        (d / "private/truth.json").write_text(json.dumps(truth))
        r = self.validate(d)
        self.assertEqual(r["checks"]["answer_matches_truth"]["status"], "failed")

    def test_renamed_and_permuted_copy_is_the_same_history(self):
        from review.agent_generalization.canonical import whole_history
        from review.genesis_acceptance.data_checks import transform
        dst = self.tmp / f"twin-{random.random()}"
        transform(self.tmp / "err", dst, 7, "both")
        self.assertEqual(whole_history(self.tmp / "err/public"), whole_history(dst / "public"))
        self.assertNotEqual(whole_history(self.tmp / "err/public"), whole_history(self.tmp / "clean/public"))


if __name__ == "__main__":
    unittest.main()
