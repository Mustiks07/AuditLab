import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from auditlab.contracts import MAX_EVIDENCE, MAX_FINDINGS, MAX_STEPS, PAGE_LIMIT
from auditlab.environment import open_session
from auditlab.generator import generate_episode
from auditlab.policies import make_policy

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "foundation"


def run_public_policy(name, seed, public, budget=MAX_STEPS):
    manifest = json.loads((public / "manifest.json").read_text(encoding="utf-8"))
    policy = make_policy(name, seed)
    session = open_session(public)
    observation = {"manifest": manifest, "remaining_steps": budget}
    actions = []
    for remaining in range(budget - 1, -1, -1):
        action = policy.act(observation)
        actions.append(action)
        if action["action"] == "submit":
            return actions
        reply = session.inspect(action)
        if not reply["ok"]:
            raise AssertionError(reply)
        observation = {**reply, "remaining_steps": remaining}
    raise AssertionError("Policy did not submit in its runner budget")


class PublicPolicyTests(unittest.TestCase):
    def test_reconcile_finds_boundary_amount_using_only_public_documents(self):
        for episode in ("S01", "S02"):
            with self.subTest(episode=episode), TemporaryDirectory() as directory:
                public = Path(directory) / "public"
                shutil.copytree(FIXTURES / episode / "public", public)
                actions = run_public_policy("reconcile", 7, public)
                submission = actions[-1]["args"]
                self.assertEqual(submission["episode_id"], episode)
                self.assertTrue(all(action["action"] == "list_records" for action in actions[:-1]))
                findings = submission["findings"]
                if episode == "S01":
                    self.assertEqual(findings, [])
                else:
                    self.assertEqual(len(findings), 1)
                    finding = findings[0]
                    self.assertEqual(finding["type"], "payment_allocation")
                    self.assertEqual(finding["currency"], "KZT")
                    self.assertEqual(finding["claimed_amount_minor"], 2_000_000)
                    self.assertCountEqual(finding["affected_ids"], ["invoice/I4", "payment/P9"])
                    self.assertCountEqual(finding["evidence_ids"], ["payments/D9", "ledger/R4", "ledger/R5",
                                                                    "policy/allocation"])

    def test_random_routes_are_reproducible_and_seeded(self):
        public = FIXTURES / "S02" / "public"
        first = run_public_policy("random", 10, public)
        self.assertEqual(first, run_public_policy("random", 10, public))
        self.assertNotEqual(first, run_public_policy("random", 11, public))
        self.assertEqual(first[-1]["action"], "submit")
        self.assertTrue(all(action["args"]["limit"] <= PAGE_LIMIT
                            for action in first if action["action"] == "list_records"))

    def test_reconcile_reports_both_issues_with_evidence_across_pages(self):
        interactions = {
            "allocation_fx_same_payment": "payment_allocation",
            "cutoff_fx_same_invoice": "period_cutoff",
        }
        for interaction, first_type in interactions.items():
            with self.subTest(interaction=interaction), TemporaryDirectory() as directory:
                episode = Path(directory) / "episode"
                generate_episode(41, {
                    "interaction": interaction,
                    "issue_counts": {first_type: 1, "fx_conversion": 1},
                    "company_size": {"ledger_rows_target": 120, "documents_target": 30},
                }, episode)
                actions = run_public_policy("reconcile", 7, episode / "public")
                truth = json.loads((episode / "private" / "truth.json").read_text(encoding="utf-8"))
                self.assertTrue(any(action["args"].get("cursor") for action in actions))
                matched = {finding["type"]: finding for finding in actions[-1]["args"]["findings"]}
                self.assertEqual(set(matched), {first_type, "fx_conversion"})
                for issue in truth["issues"]:
                    finding = matched[issue["type"]]
                    self.assertEqual(finding["claimed_amount_minor"], issue["expected_amount_minor"])
                    self.assertEqual(finding["currency"], issue["currency"])
                    self.assertCountEqual(finding["affected_ids"], issue["affected_ids"])
                    self.assertTrue(any(set(group) <= set(finding["evidence_ids"])
                                        for group in issue["evidence_sets"]))

    def test_flag_all_submits_bounded_suspicions_even_on_clean_company(self):
        actions = run_public_policy("flag_all", 7, FIXTURES / "S01" / "public")
        self.assertEqual(actions[-1]["action"], "submit")
        findings = actions[-1]["args"]["findings"]
        self.assertLessEqual(len(findings), MAX_FINDINGS)
        self.assertCountEqual([finding["type"] for finding in findings],
                              ["duplicate_invoice", "payment_allocation"])
        for finding in findings:
            self.assertIs(type(finding["claimed_amount_minor"]), int)
            self.assertLessEqual(len(finding["evidence_ids"]), MAX_EVIDENCE)

    def test_all_policies_reserve_the_last_runner_step_for_submission(self):
        public = FIXTURES / "S02" / "public"
        for name in ("random", "reconcile", "flag_all"):
            with self.subTest(name=name):
                actions = run_public_policy(name, 3, public, budget=2)
                self.assertLessEqual(len(actions), 2)
                self.assertEqual(actions[-1]["action"], "submit")

    def test_unknown_policy_is_rejected(self):
        with self.assertRaises(ValueError):
            make_policy("oracle", 7)

    def test_largest_supported_snapshot_fits_reconcile_budget(self):
        with TemporaryDirectory() as directory:
            episode = Path(directory) / "episode"
            generate_episode(99, {
                "company_size": {"ledger_rows_target": 2_000, "documents_target": 600},
                "issue_counts": {kind: 5 for kind in (
                    "duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff",
                )},
            }, episode)
            actions = run_public_policy("reconcile", 7, episode / "public")
            self.assertLessEqual(len(actions), MAX_STEPS)
            self.assertEqual(len(actions[-1]["args"]["findings"]), 20)
