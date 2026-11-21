from hashlib import sha256
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

from auditlab.release.rehearsal import PUBLIC_FILES
from auditlab.release.transforms import transform_public
from auditlab.research.curriculum.export import (
    CurriculumExample,
    FamilyMember,
    export_curriculum,
    extend_family_plan,
    plan_splits,
    snapshot_sha256,
)


ROOT = Path(__file__).resolve().parents[2]
S01 = ROOT / "fixtures/foundation/S01"
S02 = ROOT / "fixtures/foundation/S02"
DEV = ROOT / "datasets/open/dev/E15BFA18342414E20C536E2F217B94A1A"
TEST = ROOT / "datasets/open/dev/E1ADBA2ED9E09A7CE01C5B3A8D7CBE13E"
META = {"artifact_id": "curriculum-smoke", "source_sha": "62" * 20, "protocol_id": "smoke-v1"}


def example(member, teacher=None):
    return CurriculumExample(member.candidate_id, {"seed": 94, "generator": "fixture", "config": {}},
                             {"status": "passed", "snapshot_sha256": snapshot_sha256(member.snapshot / "public"),
                              "independent_calculation": {"evidence": ["invoices/D4"]}},
                             {"attempts": 2, "cost_usd": None}, teacher)


class CurriculumExportTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)

    def test_freezes_partition_before_search_and_groups_transforms(self):
        renamed = self.path / "renamed"
        transform_public(S01 / "public", renamed / "public", 6111)
        train = FamilyMember("t-base", "cutoff-1", "train", S01)
        dev = FamilyMember("d-base", "dev-1", "dev", DEV)
        test = FamilyMember("held-out", "test-1", "test", TEST)
        changed = FamilyMember("t-rename", "cutoff-1", "train", renamed, parent="t-base")
        fixed = FamilyMember("t-fixed", "cutoff-1", "train", S02, parent="t-base")

        plan = plan_splits([train, dev, test])
        self.assertEqual(plan.sha256, plan_splits([test, dev, train]).sha256)
        self.assertEqual(plan.assert_searchable(train.candidate_id), train)
        plan.assert_search_family(train.family_id)
        for holdout in (dev, test):
            with self.assertRaisesRegex(ValueError, "Search requires"):
                plan.assert_searchable(holdout.candidate_id)
            with self.assertRaisesRegex(ValueError, "Search requires"):
                plan.assert_search_family(holdout.family_id)

        updated = extend_family_plan(plan, [changed, fixed])
        manifest = updated.manifest()
        self.assertEqual(updated.sha256, plan.sha256)
        self.assertNotEqual(manifest["membership_sha256"], plan.manifest()["membership_sha256"])
        self.assertEqual(manifest["frozen_count"], 3)
        rows = {row["candidate_id"]: row for row in manifest["members"]}
        self.assertEqual(rows["t-base"]["public_family_sha256"], rows["t-rename"]["public_family_sha256"])
        self.assertEqual(rows["t-base"]["public_family_sha256"], rows["t-fixed"]["public_family_sha256"])
        self.assertEqual(rows["t-base"]["public_book_sha256"], rows["t-rename"]["public_book_sha256"])
        self.assertNotEqual(rows["t-base"]["public_book_sha256"], rows["t-fixed"]["public_book_sha256"])
        self.assertNotEqual(rows["t-base"]["snapshot_sha256"], rows["t-rename"]["snapshot_sha256"])
        self.assertIn("dev/test", manifest["partial_overlap"]["shared_distinct_complete_chains"])

    def test_rejects_cross_split_renames_and_clean_pairs_even_under_new_labels(self):
        renamed = self.path / "renamed"
        transform_public(S01 / "public", renamed / "public", 7112)
        original = FamilyMember("original", "declared-train", "train", S01)
        for snapshot in (renamed, S02):
            with self.subTest(snapshot=snapshot.name), self.assertRaisesRegex(ValueError, "Cross-split economic"):
                plan_splits([original, FamilyMember("alias", "declared-test", "test", snapshot)])

    def test_rejects_two_different_finals_from_one_source_book(self):
        train, dev = self.path / "train", self.path / "dev"
        shutil.copytree(DEV / "public", train / "public")
        shutil.copytree(TEST / "public", dev / "public")
        shutil.copytree(S01 / "public", train / "private/source_book")
        transform_public(S01 / "public", dev / "private/source_book", 8112)
        self.assertNotEqual(snapshot_sha256(train / "public"), snapshot_sha256(dev / "public"))
        with self.assertRaisesRegex(ValueError, "Cross-split economic"):
            plan_splits([FamilyMember("source-a", "family-a", "train", train),
                         FamilyMember("source-b", "family-b", "dev", dev)])

    def test_reports_exact_repeated_invoice_without_equating_whole_families(self):
        combined = self.path / "combined/public"
        shutil.copytree(S01 / "public", combined)
        other_party = self.path / "other"
        transform_public(S02 / "public", other_party, 8212)
        for name in ("ledger.csv", "invoices.jsonl", "payments.jsonl"):
            rows = (other_party / name).read_bytes().splitlines()
            if name == "ledger.csv":
                rows = rows[1:]
            path = combined / name
            path.write_bytes(path.read_bytes().rstrip(b"\r\n") + b"\n" + b"\n".join(rows) + b"\n")
        path = combined / "manifest.json"
        manifest = json.loads(path.read_bytes())
        manifest["ledger_rows"] = len((combined / "ledger.csv").read_bytes().splitlines()) - 1
        manifest["document_count"] = sum(len((combined / name).read_bytes().splitlines())
                                         for name in ("invoices.jsonl", "payments.jsonl"))
        path.write_text(json.dumps(manifest), encoding="utf-8")

        plan = plan_splits([FamilyMember("original", "one-party", "train", S01),
                            FamilyMember("with-control", "two-parties", "dev", combined.parent)])
        members = plan.manifest()["members"]
        self.assertNotEqual(members[0]["public_family_sha256"], members[1]["public_family_sha256"])
        overlap = plan.manifest()["partial_overlap"]["shared_distinct_complete_chains"]["train/dev"]
        self.assertEqual(overlap, {"counterparty": 1, "invoice": 1, "posted_counterparty": 1})

    def test_new_roots_holdout_derivatives_and_cycles_fail(self):
        train = FamilyMember("root", "share", "train", S01)
        plan = plan_splits([train])
        for member in (FamilyMember("root2", "share", "train", S02),
                       FamilyMember("related", "share", "test", S02, "root")):
            with self.subTest(candidate_id=member.candidate_id), self.assertRaisesRegex(ValueError, "derivatives"):
                extend_family_plan(plan, [member])
        with self.assertRaisesRegex(ValueError, "cycle"):
            plan_splits([FamilyMember("left", "cycle", "train", S01, "right"),
                         FamilyMember("right", "cycle", "train", S02, "left")])

    def test_copies_only_accepted_public_bytes_and_keeps_training_metadata_outside_projection(self):
        train_root = self.path / "training"
        shutil.copytree(S01 / "public", train_root / "public")
        (train_root / "public/debug-private-label.txt").write_text("must not copy", encoding="utf-8")
        (train_root / "private").mkdir()
        (train_root / "private/truth.json").write_text("must not read as JSON", encoding="utf-8")
        renamed = self.path / "renamed"
        transform_public(S01 / "public", renamed / "public", 9112)
        train = FamilyMember("a-train", "cutoff-1", "train", train_root)
        alias = FamilyMember("z-alias", "cutoff-1", "train", renamed, "a-train")
        clean = FamilyMember("b-clean", "cutoff-1", "train", S02, "a-train")
        dev = FamilyMember("dev-example", "dev-1", "dev", DEV)
        test = FamilyMember("secret-held-out-name", "secret-held-out-family", "test", TEST)
        plan = plan_splits([train, alias, clean, dev, test])
        teacher = {"episode_id": "S01", "findings": []}
        examples = [example(alias), example(dev), example(train, teacher), example(clean)]

        path = export_curriculum(plan, examples, self.path / "handoff", **META)
        result = json.loads(path.read_bytes())
        by_id = {row["candidate_id"]: row for row in result["examples"]}
        self.assertEqual({key: result[key] for key in META}, META)
        self.assertEqual(result["schema_version"], "1.0")
        self.assertEqual(result["partition_sha256"], plan.sha256)
        self.assertEqual(result["planned_counts"], {"train": 3, "dev": 1, "test": 1})
        self.assertNotIn(b"secret-held-out", path.read_bytes())
        self.assertEqual(by_id["z-alias"]["status"], "filtered")
        self.assertEqual(by_id["z-alias"]["duplicate_of"], "a-train")
        self.assertEqual(by_id["z-alias"]["selection_cost"], {"attempts": 2, "cost_usd": None})
        self.assertEqual(by_id["b-clean"]["status"], "included")
        dev_row = by_id["dev-example"]
        self.assertIsNone(dev_row["provenance"])
        self.assertNotIn("independent_calculation", dev_row["validation"])
        self.assertEqual(len(dev_row["validation_sha256"]), 64)
        by_split = {"train": {"a-train", "b-clean"}, "dev": {"dev-example"}}
        for split, members in by_split.items():
            self.assertEqual({p.name for p in (path.parent / "public" / split).iterdir()},
                             {by_id[key]["snapshot_sha256"] for key in members})
            for candidate in members:
                row = by_id[candidate]
                copied = path.parent / row["snapshot"]
                self.assertEqual(copied.name, row["snapshot_sha256"])
                self.assertEqual({p.name for p in copied.iterdir()}, set(PUBLIC_FILES))
                self.assertEqual(snapshot_sha256(copied), row["snapshot_sha256"])
                self.assertEqual(row["validation"]["status"], "passed")
        self.assertIsNone(by_id["dev-example"]["teacher"])
        ref = by_id["a-train"]["teacher"]
        self.assertTrue(ref["path"].startswith("trusted/train/"))
        teaching = (path.parent / ref["path"]).read_bytes()
        self.assertEqual(sha256(teaching).hexdigest(), ref["sha256"])
        self.assertEqual(json.loads(teaching), teacher)
        self.assertFalse((path.parent / "trusted/dev").exists())
        self.assertEqual([episode["candidate_id"] for episode in result["episodes"]], ["a-train"])
        training_source = result["episodes"][0]
        self.assertEqual((path.parent / training_source["episode_ref"]).resolve(), train_root.resolve())
        self.assertEqual(training_source["split"], "train")
        second = export_curriculum(plan, reversed(examples), self.path / "second", **META)
        self.assertEqual(second.read_bytes(), path.read_bytes())
        with self.assertRaises(FileExistsError):
            export_curriculum(plan, examples, path.parent, **META)

    def test_rejects_stale_receipts_and_labeled_holdout_even_with_a_valid_plan(self):
        train_root = self.path / "train"
        shutil.copytree(S01 / "public", train_root / "public")
        train = FamilyMember("train", "cutoff-1", "train", train_root)
        dev = FamilyMember("dev", "dev-1", "dev", DEV)
        test = FamilyMember("test", "test-1", "test", TEST)
        plan = plan_splits([train, dev, test])
        valid = example(train)
        bad_receipt = CurriculumExample("train", {}, {"status": "passed", "snapshot_sha256": "wrong"}, {})
        output = self.path / "handoff"
        cases = (([example(test)], "planned train/dev"),
                 ([example(dev, {"findings": []})], "trusted training"),
                 ([example(train, {"episode_id": "stale", "findings": []})], "episode_mismatch"),
                 ([bad_receipt], "acceptance receipt"))
        for examples, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                export_curriculum(plan, examples, output, **META)
            self.assertFalse(output.exists())
        ledger = train_root / "public/ledger.csv"
        ledger.write_bytes(ledger.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            plan.assert_searchable("train")
        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            export_curriculum(plan, [valid], output, **META)
        self.assertFalse(output.exists())
