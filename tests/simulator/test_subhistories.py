"""A copied commercial story must remain visible behind unrelated company background."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "verifier"))
from cases import Case
from auditlab.release.transforms import transform_public
from datasets.audit_families import fingerprints
from datasets.audit_subhistories import check_subhistories


def company(root, core_amount, background_amount):
    case = Case("SUBHISTORY")
    issued, paid = "2026-11-05T10:00:00+05:00", "2026-11-07T10:00:00+05:00"
    case.invoice("D1", "I1", issued, core_amount)
    case.journal(issued, ("AR", core_amount, 0), ("REVENUE", 0, core_amount), invoice="I1")
    case.payment("D2", "P1", paid, core_amount, ["I1"])
    case.journal(paid, ("CASH", core_amount, 0), ("UNAPPLIED", 0, core_amount), invoice="I1", payment="P1")
    case.journal(paid, ("UNAPPLIED", core_amount, 0), ("AR", 0, core_amount), invoice="I1", payment="P1")
    case.invoice("D3", "I2", issued, background_amount, entity="BACKGROUND")
    case.journal(issued, ("AR", background_amount, 0), ("REVENUE", 0, background_amount),
                 invoice="I2", entity="BACKGROUND")
    return case.write(root)


class CommercialSubhistoryTests(unittest.TestCase):
    def test_copied_story_survives_different_background_and_id_permutation(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            a = company(root / "a", 5000, 1000)
            b = company(root / "b", 5000, 2000)
            renamed = root / "renamed"
            transform_public(b, renamed, 92)
            self.assertNotEqual(fingerprints(a)["family"], fingerprints(renamed)["family"])
            report = check_subhistories({"train": {"a": a}, "dev": {"renamed": renamed}})
            for view in report["views"].values():
                self.assertEqual(view["shared_families"], 1)
                self.assertEqual(view["episode_pairs"], {"train/dev": [("a", "renamed")]})

    def test_same_rule_and_structure_with_different_terms_is_not_a_copy(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            a = company(root / "a", 5000, 1000)
            b = company(root / "b", 5001, 2000)
            report = check_subhistories({"train": {"a": a}, "dev": {"b": b}})
            self.assertTrue(all(v["shared_families"] == 0 for v in report["views"].values()))

    def test_empty_companies_do_not_create_a_shared_commercial_history(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            a, b = Case("EMPTY-A").write(root / "a"), Case("EMPTY-B").write(root / "b")
            report = check_subhistories({"train": {"a": a}, "dev": {"b": b}})
            for view in report["views"].values():
                self.assertEqual(view["units_per_split"], {"train": 0, "dev": 0})
                self.assertEqual(view["shared_families"], 0)

    def test_posting_defect_changes_book_but_not_primary_story(self):
        foundation = ROOT / "fixtures" / "foundation"
        report = check_subhistories({"train": {"clean": foundation / "S01" / "public"},
                                    "dev": {"late": foundation / "S02" / "public"}})
        self.assertEqual(report["views"]["counterparty"]["shared_families"], 1)
        self.assertEqual(report["views"]["invoice"]["shared_families"], 1)
        self.assertEqual(report["views"]["posted_counterparty"]["shared_families"], 0)


if __name__ == "__main__":
    unittest.main()
