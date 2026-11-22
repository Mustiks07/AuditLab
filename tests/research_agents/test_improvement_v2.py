"""Agent-improvement v2: content diagnosis, frozen prompt variants A/B/C and the frozen replication set."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from auditlab.research.agents import experiment  # noqa: E402
from auditlab.research.agents.agent import LLMAuditor, prompt_hash, system_prompt  # noqa: E402
from auditlab.research.agents.diagnostics import diagnose  # noqa: E402
from auditlab.research.agents.execute import InProcessBridge, run_attempt  # noqa: E402
from auditlab.research.agents.prompts import ADDITIONS, PROVENANCE, prompt_set_record  # noqa: E402
from auditlab.research.agents.providers import ProviderError, ScriptedProvider, scripted_turn  # noqa: E402
from auditlab.research.agents.replication import check as check_replication  # noqa: E402

REPLICATION = ROOT / "docs" / "research" / "agent-improvement-v2" / "replication-922101"
TRUTH = {"episode_id": "E", "issues": [{"issue_id": "X1", "type": "payment_allocation",
                                        "affected_ids": ["invoice/I1", "payment/P1"], "expected_amount_minor": 2000,
                                        "currency": "USD", "evidence_sets": [["payments/D1", "ledger/R1"]]}]}


def finding(**changes):
    base = {"type": "payment_allocation", "affected_ids": ["payment/P1", "invoice/I1"], "claimed_amount_minor": 2000,
            "currency": "USD", "evidence_ids": ["payments/D1", "ledger/R1"], "explanation": "x"}
    return {**base, **changes}


class Diagnosis(unittest.TestCase):
    def labels(self, findings, evaluation, truth=TRUTH):
        return [f["label"] for f in diagnose({"episode_id": "E", "findings": findings}, truth, evaluation)["findings"]]

    def test_each_finding_gets_a_reason(self):
        evaluation = {"matches": [[0, "X1"]], "unmatched": {"1": "duplicate", "2": "evidence_incomplete",
                                                             "3": "unavailable_evidence", "4": "no_matching_issue",
                                                             "5": "no_matching_issue", "6": "no_matching_issue",
                                                             "7": "no_matching_issue"}}
        findings = [finding(), finding(), finding(evidence_ids=["payments/D1"]), finding(evidence_ids=["x/y"]),
                    finding(claimed_amount_minor=2001), finding(currency="KZT"), finding(affected_ids=["payment/P1"]),
                    finding(type="duplicate_invoice", affected_ids=["invoice/I9"])]
        self.assertEqual(self.labels(findings, evaluation),
                         ["correct", "duplicate", "evidence_incomplete", "evidence_unavailable", "wrong_amount",
                          "wrong_currency", "wrong_entities", "unrelated_claim"])

    def test_misses_and_clean_accusations(self):
        missed = diagnose({"episode_id": "E", "findings": []}, TRUTH, {"matches": [], "unmatched": {}})
        self.assertEqual((missed["missed_by_type"], missed["clean"]), ({"payment_allocation": 1}, False))
        clean = {"episode_id": "E", "issues": []}
        self.assertEqual(self.labels([finding()], {"matches": [], "unmatched": {"0": "no_matching_issue"}}, clean),
                         ["false_accusation_on_clean"])
        self.assertEqual(diagnose(None, TRUTH, None)["submitted_findings"], 0)


class PromptVariants(unittest.TestCase):
    PROVIDER = {"provider": "openai_compatible", "model": "m"}

    def test_comparable_context_and_frozen_hashes(self):
        self.assertEqual(ADDITIONS["A"], "")
        b, c = len(ADDITIONS["B"]), len(ADDITIONS["C"])
        self.assertLessEqual(abs(b - c) / max(b, c), 0.10)  # comparable context budget for B and C
        self.assertEqual(prompt_hash("tools", self.PROVIDER, 4096), prompt_hash("tools", self.PROVIDER, 4096, "A"))
        hashes = {v: prompt_hash("tools", self.PROVIDER, 4096, v) for v in "ABC"}
        self.assertEqual(len(set(hashes.values())), 3)
        record = prompt_set_record()
        self.assertEqual(set(record["variants"]), {"A", "B", "C"})
        with self.assertRaises(ValueError):
            system_prompt("tools", "D")

    def test_c_is_built_without_dev_or_final_values(self):
        truth = json.loads((REPLICATION / "original_error" / "private" / "truth.json").read_text(encoding="utf-8"))
        manifest = json.loads((REPLICATION / "original_error" / "public" / "manifest.json").read_text(encoding="utf-8"))
        dev_ids = [manifest["episode_id"], *truth["issues"][0]["affected_ids"], *truth["issues"][0]["evidence_sets"][0]]
        dev_ids = [v for v in dev_ids if not v.startswith("policy/")]  # rule names are public and shared by all episodes
        self.assertGreaterEqual(len(dev_ids), 4)
        for value in dev_ids:
            self.assertNotIn(value.split("/", 1)[-1], ADDITIONS["C"])
            self.assertNotIn(value.split("/", 1)[-1], ADDITIONS["B"])
        self.assertNotIn(str(truth["issues"][0]["expected_amount_minor"]), ADDITIONS["C"])
        self.assertIn("train", PROVENANCE["C"]["worked_example"])

    def test_variant_reaches_the_agent_and_its_record(self):
        agent = LLMAuditor(ScriptedProvider([]), "tools", prompt_variant="C")
        self.assertIn("Procedure for designated payments", agent.system)
        self.assertEqual(agent.describe()["prompt_variant"], "C")


class ReplicationSet(unittest.TestCase):
    def test_frozen_set_is_unchanged_and_validated(self):
        result = check_replication(REPLICATION)
        self.assertEqual(result["status"], "passed", result["changed"])
        manifest = json.loads((REPLICATION / "replication-set.json").read_text(encoding="utf-8"))
        self.assertEqual(set(manifest["conditions"]), {"original_error", "corrected_clean", "transformed_error", "transformed_clean"})
        for name in ("original_error", "corrected_clean"):
            self.assertEqual(manifest["conditions"][name]["validation"]["status"], "passed")
        error = manifest["conditions"]["original_error"]["truth_issues"]
        self.assertEqual(error, [{"currency": "KZT", "expected_amount_minor": 3024000, "type": "payment_allocation"}])
        self.assertEqual(manifest["conditions"]["corrected_clean"]["truth_issues"], [])


class ContentBreakdown(unittest.TestCase):
    def test_summary_separates_service_and_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            episodes = {}
            for name in ("original_error", "corrected_clean"):
                episodes[name] = tmp / name
                shutil.copytree(REPLICATION / name, episodes[name])
            plan = {"schema_version": "1.0", "artifact_id": "breakdown", "protocol_id": "auditlab-research-600s",
                    "purpose": "selection", "repetitions": 1, "transport": "in_process", "service": {"max_slot_retries": 0},
                    "episodes": [{"episode_ref": episodes[n].as_posix(), "family_id": "dev-922101", "split": "dev",
                                  "variant": "error" if n == "original_error" else "clean"} for n in episodes],
                    "arms": [{"name": "td", "agent": {"kind": "llm", "variant": "tools", "retry": {"max_tries": 1}},
                              "provider": {"provider": "test_double"}}]}
            path = tmp / "plan.json"
            path.write_text(json.dumps(plan))
            plan, sha = experiment.load_plan(path)
            ids = {n: json.loads((episodes[n] / "public" / "manifest.json").read_text())["episode_id"] for n in episodes}
            script = [scripted_turn("submit", {"episode_id": ids["original_error"], "findings": []}),
                      ProviderError("rate_limit", "HTTP 429", status=429)]
            summary = experiment.run_experiment(plan, sha, tmp / "out", provider_override=ScriptedProvider(script),
                                                log=lambda _: None, sleep=lambda _: None)
            arm = summary["arms"]["td"]
            self.assertEqual(arm["service_outcomes"], {"attempts": 1, "statuses": {"rate_limited": 1}})
            content = arm["content_outcomes"]
            self.assertEqual((content["attempts"], content["error_attempts_with_a_miss"], content["issues_missed"],
                              content["missed_by_type"], content["clean_variant_attempts"]),
                             (1, 1, 1, {"payment_allocation": 1}, 0))


if __name__ == "__main__":
    unittest.main()
