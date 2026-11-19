"""Separate trusted step: fixed answer vs truth, controlled truth distortions (SPEC §10, VER-01)."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

import cases  # noqa: E402
from auditlab.verifier import verify_report  # noqa: E402
from auditlab.verifier.crosscheck import compare, crosscheck, reduce_counterexample  # noqa: E402

FIXTURES = ROOT / "fixtures" / "foundation"


def categories(discrepancies):
    return sorted({d["category"] for d in discrepancies})


class CrosscheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.public = self.tmp / "S02" / "public"
        shutil.copytree(FIXTURES / "S02" / "public", self.public)
        self.truth = json.loads((FIXTURES / "S02" / "private" / "truth.json").read_text(encoding="utf-8"))
        self.report = verify_report(self.public)

    def test_pair_agrees_and_answer_is_written_before_truth_is_read(self):
        for label in ("S01", "S02"):
            with self.subTest(label=label):
                code = ("import sys, json\n"
                        "events = []\n"
                        "sys.addaudithook(lambda e, a: events.append((str(a[0]), str(a[1]))) "
                        "if e == 'open' and isinstance(a[0], str) else None)\n"
                        "from auditlab.verifier.crosscheck import crosscheck\n"
                        "r = crosscheck(sys.argv[1], sys.argv[2], sys.argv[3])\n"
                        "print(json.dumps({'status': r['status'], 'events': events}))\n")
                out = self.tmp / f"cc-{label}"
                result = subprocess.run([sys.executable, "-c", code, str(FIXTURES / label / "public"),
                                         str(FIXTURES / label / "private" / "truth.json"), str(out)],
                                        cwd=ROOT, capture_output=True, text=True, check=True)
                data = json.loads(result.stdout)
                self.assertEqual(data["status"], "agree")
                paths = [p for p, mode in data["events"]]
                answer = next(i for i, (p, mode) in enumerate(data["events"])
                              if p.endswith("answer.json") and "w" in mode)
                truth = next(i for i, p in enumerate(paths) if p.endswith("truth.json"))
                self.assertLess(answer, truth)

    def distorted(self, mutate):
        truth = copy.deepcopy(self.truth)
        mutate(truth["issues"][0])
        return compare(self.report, truth, self.public)

    def test_controlled_truth_distortions_are_detected_by_kind(self):
        # TEST DISTORTIONS (intentional, not development defects).
        def amount(issue): issue["expected_amount_minor"] *= 100
        def currency(issue): issue["currency"] = "USD"
        def affected(issue): issue["affected_ids"] = ["invoice/I5", "payment/P9"]
        def unknown_ref(issue): issue["evidence_sets"] = [["payments/D9", "ledger/R44", "ledger/R5", "policy/allocation"]]
        def foreign_ref(issue): issue["evidence_sets"] = [["payments/D9", "ledger/R6", "ledger/R5", "policy/allocation"]]
        def insufficient(issue): issue["evidence_sets"] = [["payments/D9", "ledger/R4"]]
        def oversize(issue): issue["evidence_sets"] = [["ledger/R%d" % i for i in range(1, 9)] + ["payments/D9", "invoices/D4", "policy/allocation"]]
        expected = {amount: ["amount_mismatch"], currency: ["currency_mismatch"],
                    affected: ["affected_id_unknown", "affected_ids_mismatch"],
                    unknown_ref: ["evidence_ref_unknown", "evidence_set_differs"],
                    foreign_ref: ["evidence_set_differs"], insufficient: ["evidence_insufficient"],
                    oversize: ["evidence_set_differs", "evidence_set_size"]}
        for mutate, wanted in expected.items():
            with self.subTest(distortion=mutate.__name__):
                self.assertEqual(categories(self.distorted(mutate)), wanted)

    def test_missing_and_extra(self):
        truth = copy.deepcopy(self.truth)
        truth["issues"] = []
        self.assertEqual(categories(compare(self.report, truth, self.public)), ["extra_in_verifier"])
        clean = verify_report(FIXTURES / "S01" / "public")
        self.assertEqual(categories(compare(clean, {**self.truth, "episode_id": "S01"}, FIXTURES / "S01" / "public")),
                         ["missing_in_verifier"])

    def test_counterexample_is_reduced_on_a_copy(self):
        # S02 shape plus unrelated clean distractors; truth distorted (amount x100) on purpose.
        case = cases.allocation_case("2026-12-01T00:01:00+05:00", "BIG")
        for n in range(5):
            inv = f"I{20 + n}"
            case.invoice(f"D{20 + n}", inv, "2026-11-0%dT10:00:00+05:00" % (n + 2), 100_000 * (n + 1), entity=f"C{n}")
            case.journal("2026-11-0%dT10:00:00+05:00" % (n + 2), ("AR", 100_000 * (n + 1), 0),
                         ("REVENUE", 0, 100_000 * (n + 1)), entity=f"C{n}", invoice=inv)
        case.rate("2026-11-02", "USD", "KZT", "500")
        public = case.write(self.tmp / "big")
        before = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(public.iterdir()))).hexdigest()
        truth = copy.deepcopy(self.truth)
        truth["episode_id"] = "BIG"
        truth["issues"][0]["expected_amount_minor"] = 200_000_000
        result = crosscheck(public, self.write_truth(truth), self.tmp / "cc-big", engine="sqlite")
        self.assertEqual(result["status"], "disagree")
        self.assertEqual(categories(result["discrepancies"]), ["amount_mismatch"])
        reduced = result["counterexample"]
        self.assertLess(reduced["ledger_rows"], 16)
        self.assertLessEqual(reduced["documents"], 2)
        self.assertEqual(reduced["fx_rows"], 0)
        after = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(public.iterdir()))).hexdigest()
        self.assertEqual(before, after, "original episode must stay untouched")
        # The stored counterexample reproduces the same discrepancy on its own.
        small = Path(reduced["reduced_public"])
        again = compare(verify_report(small, "sqlite"), truth, small)
        self.assertEqual(categories(again), ["amount_mismatch"])

    def write_truth(self, truth):
        path = self.tmp / f"truth-{len(list(self.tmp.iterdir()))}.json"
        path.write_text(json.dumps(truth), encoding="utf-8")
        return path

    def test_no_reduction_without_discrepancy(self):
        with self.assertRaises(ValueError):
            reduce_counterexample(self.public, self.truth, self.tmp / "none")


if __name__ == "__main__":
    unittest.main()
