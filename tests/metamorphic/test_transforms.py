"""Harmless transforms (SPEC §11, REP-02/REP-03): the independent answer survives row
permutation, bijective ID renaming and rewriting timestamps as the same UTC instants."""
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "verifier"))
sys.path.insert(0, str(ROOT))

import cases  # noqa: E402
from auditlab.verifier import verify_report  # noqa: E402

BUILDERS = {
    "S02-shape": lambda: cases.allocation_case("2026-12-01T00:01:00+05:00"),
    "S01-shape": lambda: cases.allocation_case("2026-11-30T23:59:00+05:00"),
    "boundary-exact": lambda: cases.allocation_case("2026-12-01T00:00:00+05:00"),
    "split-clean": cases.split_payment_clean,
    "refund-clean": cases.refund_clean,
    "instructions": lambda: cases.instructed_split(1_000_000),
    "fx": lambda: cases.fx_payment_case(2_550_000),
    "fx-local-date": lambda: cases.fx_payment_case(2_500_000, timestamp="2026-11-30T19:30:00+00:00"),
    "cutoff-invoice": lambda: cases.cutoff_invoice_case("2026-12-01T00:00:00+05:00"),
    "cutoff-payment": lambda: cases.cutoff_payment_case("2026-12-01T00:10:00+05:00", "2026-12-01T00:20:00+05:00"),
    "duplicate-same-id": lambda: cases.duplicate_case(True),
    "duplicate-new-id": lambda: cases.duplicate_case(True, "I5"),
    "duplicate-clean": lambda: cases.duplicate_case(False),
    "equal-amounts": cases.equal_amount_different_suppliers,
    "interaction-a": lambda: cases.interaction_a(True, True),
    "interaction-b": lambda: cases.interaction_b(True, True),
}


def comparable(report, refs=None):
    """Findings with every ID mapped through refs; evidence as a set."""
    m = refs or {}
    return sorted((f["type"], tuple(sorted(m.get(a, a) for a in f["affected_ids"])),
                   f["claimed_amount_minor"], f["currency"], tuple(sorted(m.get(e, e) for e in f["evidence_ids"])))
                  for f in report["findings"])


class TransformTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_rename_and_permute_preserve_answer(self):
        for name, build in BUILDERS.items():
            for seed in (1, 2, 3):
                with self.subTest(case=name, seed=seed):
                    original = build().write(self.tmp / f"{name}-{seed}")
                    renamed = self.tmp / f"{name}-{seed}-renamed" / "public"
                    refs = cases.rename_ids(original, renamed, seed)
                    a, b = verify_report(original), verify_report(renamed)
                    self.assertEqual(comparable(a, refs), comparable(b))
                    self.assertEqual(len(a["notes"]), len(b["notes"]))
                    self.assertNotEqual(a["input_sha256"]["ledger.csv"], b["input_sha256"]["ledger.csv"])

    def test_utc_rewrite_preserves_answer(self):
        for name, build in BUILDERS.items():
            with self.subTest(case=name):
                original = build().write(self.tmp / name)
                utc = self.tmp / f"{name}-utc" / "public"
                cases.to_utc(original, utc)
                self.assertEqual(comparable(verify_report(original)), comparable(verify_report(utc)))

    def test_duplicate_is_not_chosen_by_smaller_string_id(self):
        # The original recognition (5 Nov) belongs to I4. After renaming, I4 gets the larger new ID,
        # and the answer must still name it rather than the lexicographically smaller copy.
        original = cases.duplicate_case(True, "I5").write(self.tmp / "dup")
        renamed = self.tmp / "dup-renamed" / "public"
        refs = cases.rename_ids(original, renamed, 7)
        self.assertGreater(refs["invoice/I4"], refs["invoice/I5"])
        [finding] = verify_report(renamed)["findings"]
        self.assertEqual(finding["affected_ids"], [refs["invoice/I4"]])

    def test_fixture_pair_permuted_and_renamed(self):
        for label, expected in (("S01", 0), ("S02", 2_000_000)):
            with self.subTest(label=label):
                public = self.tmp / label / "public"
                shutil.copytree(ROOT / "fixtures" / "foundation" / label / "public", public)
                renamed = self.tmp / f"{label}-renamed" / "public"
                refs = cases.rename_ids(public, renamed, 11)
                self.assertEqual(comparable(verify_report(public), refs), comparable(verify_report(renamed)))
                total = sum(f["claimed_amount_minor"] for f in verify_report(renamed)["findings"])
                self.assertEqual(total, expected)


if __name__ == "__main__":
    unittest.main()
