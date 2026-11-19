import json
import re
import shutil
import unittest
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from auditlab.contracts import MAX_EVIDENCE, MAX_EXPLANATION, MAX_FINDINGS, MAX_STEPS
from datasets.build import build_datasets, load_recipe

from test_policies import run_public_policy


SHIPPED = Path(__file__).resolve().parents[2] / "datasets" / "open"
PUBLIC_FILES = {"manifest.json", "ledger.csv", "invoices.jsonl", "payments.jsonl", "fx.csv", "policy.json"}
MANIFEST_FIELDS = {
    "schema_version", "episode_id", "base_currency", "period_start", "period_end", "timezone",
    "ledger_rows", "document_count", "currency_exponents", "rounding",
}
ISSUE_TYPES = {"duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff"}


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class DatasetTests(unittest.TestCase):
    def test_fresh_build_reproduces_every_shipped_file(self):
        with TemporaryDirectory() as directory:
            replay = Path(directory) / "replay"
            build_datasets(replay)
            relative_paths = {path.relative_to(SHIPPED) for path in SHIPPED.rglob("*") if path.is_file()}
            self.assertEqual(len(relative_paths), 160)
            self.assertEqual(relative_paths, {path.relative_to(replay) for path in replay.rglob("*") if path.is_file()})
            for path in relative_paths:
                with self.subTest(path=path):
                    self.assertEqual((SHIPPED / path).read_bytes(), (replay / path).read_bytes())
            with self.assertRaises(FileExistsError):
                build_datasets(replay)

    def test_matrix_and_public_metadata(self):
        recipe = load_recipe()
        families = {}
        seeds = {}
        episode_ids = set()
        for split in ("train", "dev"):
            entries = recipe["splits"][split]
            families[split] = {entry["family"] for entry in entries}
            seeds[split] = {entry["seed_hex"] for entry in entries}
            self.assertEqual(len(families[split]), 9)
            self.assertEqual(len(seeds[split]), 9)
            self.assertTrue(all(re.fullmatch(r"[0-9a-f]{32}", seed) for seed in seeds[split]))
            self.assertEqual(Counter(entry["category"] for entry in entries),
                             {"clean": 2, "single": 4, "interaction": 2, "boundary": 2})
            seed_labels = {s for entry in entries for s in (entry["seed_hex"], entry["family"])}
            counts = Counter()
            issues_by_type = Counter()
            episodes = list((SHIPPED / split).iterdir())
            self.assertEqual(len(episodes), 10)
            for episode in episodes:
                with self.subTest(episode=episode.name):
                    public = episode / "public"
                    manifest = load_json(public / "manifest.json")
                    self.assertEqual({path.name for path in public.iterdir()}, PUBLIC_FILES)
                    self.assertEqual(manifest.keys(), MANIFEST_FIELDS)
                    self.assertEqual(manifest["episode_id"], episode.name)
                    self.assertRegex(episode.name, r"^E[0-9A-F]{32}$")
                    self.assertNotIn(episode.name, episode_ids)
                    episode_ids.add(episode.name)
                    self.assertEqual((manifest["ledger_rows"], manifest["document_count"]), (28, 9))
                    corpus = "\n".join(path.read_text(encoding="utf-8") for path in public.iterdir())
                    for marker in seed_labels | ISSUE_TYPES | {"seed", "interaction", "issue_counts", "expected_amount_minor",
                                                               "evidence_sets", "provenance", "clean_distractors"}:
                        self.assertNotIn(marker, corpus)
                    issues = load_json(episode / "private" / "truth.json")["issues"]
                    counts[len(issues)] += 1
                    issues_by_type.update(issue["type"] for issue in issues)
            self.assertEqual(counts, {0: 2, 1: 5, 2: 3})
            self.assertEqual(issues_by_type.keys(), ISSUE_TYPES)
            self.assertEqual(issues_by_type["fx_conversion"], 3)
        self.assertFalse(families["train"] & families["dev"])
        self.assertFalse(seeds["train"] & seeds["dev"])

    def test_three_ordinary_policies_on_all_public_episodes(self):
        for episode in sorted(SHIPPED.glob("*/*")):
            with self.subTest(episode=episode.name), TemporaryDirectory() as directory:
                public = Path(directory) / "public"
                shutil.copytree(episode / "public", public)
                for name in ("reconcile", "random", "flag_all"):
                    actions = run_public_policy(name, 17, public)
                    self.assertEqual(actions, run_public_policy(name, 17, public))
                    self.assertLessEqual(len(actions), MAX_STEPS)
                    submit = actions[-1]["args"]
                    self.assertEqual(submit["episode_id"], episode.name)
                    findings = submit["findings"]
                    self.assertLessEqual(len(findings), MAX_FINDINGS)
                    for finding in findings:
                        self.assertIn(finding["type"], ISSUE_TYPES)
                        self.assertIs(type(finding["claimed_amount_minor"]), int)
                        self.assertGreaterEqual(finding["claimed_amount_minor"], 0)
                        self.assertLessEqual(len(finding["evidence_ids"]), MAX_EVIDENCE)
                        self.assertLessEqual(len(finding["explanation"]), MAX_EXPLANATION)
                    if name == "reconcile":
                        reconciled = findings
                truth = load_json(episode / "private" / "truth.json")["issues"]
                matches = {(finding["type"], tuple(sorted(finding["affected_ids"]))): finding for finding in reconciled}
                self.assertEqual(len(reconciled), len(truth))
                for issue in truth:
                    finding = matches[(issue["type"], tuple(sorted(issue["affected_ids"])))]
                    self.assertEqual(finding["claimed_amount_minor"], issue["expected_amount_minor"])
                    self.assertEqual(finding["currency"], issue["currency"])
                    self.assertTrue(any(set(group) <= set(finding["evidence_ids"]) for group in issue["evidence_sets"]))
