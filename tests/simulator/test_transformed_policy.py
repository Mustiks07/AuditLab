"""Ordinary reconcile must rediscover evidence after the release ID/order transform."""

import csv
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from auditlab.contracts import MAX_STEPS, PAGE_LIMIT
from auditlab.environment import open_session
from auditlab.generator import generate_episode
from auditlab.policies import make_policy
from auditlab.release.rehearsal import selection_manifest
from auditlab.release.transforms import comparable, read_json, transform_public, transform_truth, write_json
from auditlab.runner import make_env
from auditlab.runner.episode import run_episode

DATASETS = Path(__file__).resolve().parents[2] / "datasets" / "open"
KEY_FIELDS = {"ledger": "row_id", "invoices": "doc_id", "payments": "doc_id", "policy": "id"}


def observed_references(trajectory):
    references = set()
    for row in trajectory:
        name, args = row["action"]["action"], row["action"]["args"]
        result = row["observation"]["result"]
        if name == "list_records":
            table = args["table"]
            for record in result["records"]:
                key = ("/".join(record[field] for field in ("date", "source_currency", "target_currency"))
                       if table == "fx" else record[KEY_FIELDS[table]])
                references.add(f"{table}/{key}")
        elif name == "get_record":
            references.add(f"{args['table']}/{args['id']}")
        elif name == "get_document":
            table = "payments" if "payment_id" in result else "invoices"
            references.add(f"{table}/{result['doc_id']}")
    return references


def ledger_order(public):
    with (public / "ledger.csv").open(encoding="utf-8", newline="") as stream:
        return [f"ledger/{row['row_id']}" for row in csv.DictReader(stream)]


class TransformedPolicyTests(unittest.TestCase):
    def run_reconcile(self, snapshot, export, truth, policy_seed):
        env = make_env("train", evaluator="inprocess")
        try:
            _, _, terminated, truncated, info = run_episode(env, make_policy("reconcile", policy_seed), 0, {
                "snapshot_dir": str(snapshot), "export_dir": str(export), "policy_name": "reconcile",
            })
        finally:
            env.close()
        self.assertEqual((terminated, truncated, info["status"]), (True, False, "submitted"))
        run = read_json(export / "run.json")
        self.assertEqual((run["status"], run["outcome"]), ("completed", "submitted"))
        score = read_json(export / "score.json")
        self.assertEqual(score, info["score"])
        self.assertEqual((score["tp"], score["fp"], score["fn"]), (len(truth["issues"]), 0, 0))
        submission = read_json(export / "submission.json")
        self.assertEqual(submission["episode_id"], truth["episode_id"])
        rows = [json.loads(line) for line in (export / "trajectories.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[-1]["action"], {"action": "submit", "args": submission})
        self.assertLessEqual(len(rows), MAX_STEPS)
        self.assertEqual(len(rows), run["inspection_steps"] + 1)
        self.assertEqual([row["observation"]["remaining_steps"] for row in rows],
                         list(range(MAX_STEPS - 1, MAX_STEPS - len(rows) - 1, -1)))
        self.assertTrue(all(row["observation"]["ok"] for row in rows))
        self.assertAlmostEqual(score["reward"], 1 - 0.002 * run["inspection_steps"])
        observed = observed_references(rows[:-1])
        for finding in submission["findings"]:
            self.assertLessEqual(set(finding["evidence_ids"]), observed)
        return submission["findings"], {key: value for key, value in score.items() if key != "reward"}

    def assert_reconcile_pair(self, original, root, transform_seed, policy_seed):
        renamed = root / "renamed"
        mapping = transform_public(original / "public", renamed / "public", transform_seed)
        reference = read_json(original / "private" / "truth.json")
        transformed_reference = transform_truth(reference, mapping)
        (renamed / "private").mkdir()
        write_json(renamed / "private" / "truth.json", transformed_reference)
        inverse = {new: old for old, new in mapping["references"].items()}

        self.assertNotEqual(reference["episode_id"], transformed_reference["episode_id"])
        restored_order = [inverse[key] for key in ledger_order(renamed / "public")]
        self.assertNotEqual(ledger_order(original / "public"), restored_order)

        before, score_before = self.run_reconcile(original, root / "before", reference, policy_seed)
        after, score_after = self.run_reconcile(renamed, root / "after", transformed_reference, policy_seed)
        self.assertEqual(comparable(before), comparable(after, inverse))
        self.assertEqual(score_before, score_after)

        first_page = {"action": "list_records", "args": {"table": "ledger", "cursor": None, "limit": PAGE_LIMIT}}
        old_page = open_session(original / "public").inspect(first_page)["result"]
        new_session = open_session(renamed / "public")
        new_page = new_session.inspect(first_page)["result"]
        self.assertNotEqual([f"ledger/{row['row_id']}" for row in old_page["records"]],
                            [inverse[f"ledger/{row['row_id']}"] for row in new_page["records"]])
        self.assertIsNotNone(old_page["next_cursor"])
        stale = new_session.inspect({"action": "list_records", "args": {
            "table": "ledger", "cursor": old_page["next_cursor"], "limit": PAGE_LIMIT,
        }})
        self.assertEqual(stale["error"]["code"], "invalid_cursor")

    def test_reconcile_rediscovers_evidence_on_open_train_and_dev_pairs(self):
        originals = [path for split in ("train", "dev") for path in sorted((DATASETS / split).iterdir())
                     if (path / "public" / "manifest.json").is_file()]
        self.assertEqual(len(originals), 20)
        for seed, original in enumerate(originals, start=917):
            with self.subTest(split=original.parent.name, episode=original.name), TemporaryDirectory() as temp:
                self.assert_reconcile_pair(original, Path(temp), seed, policy_seed=314159)

    def test_reconcile_rediscovers_evidence_on_frozen_internal_pairs(self):
        selection = selection_manifest()
        self.assertEqual(len(selection["episodes"]), 20)
        for entry in selection["episodes"]:
            with self.subTest(episode=entry["id"]), TemporaryDirectory() as temp:
                original = Path(temp) / "original"
                generate_episode(entry["seed"], entry["config"], original)
                self.assert_reconcile_pair(original, Path(temp), entry["pair_seed"], selection["policy_seed"])
