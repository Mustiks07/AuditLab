"""SFT dataset building, selection guards and equal-budget sampling (synthetic attempts only)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from training_fixtures import HIDDEN_REASONING, api_agent, make_attempt, make_curriculum  # noqa: E402

from auditlab.research.training.dataset import (DatasetError, build_sft, equal_budget, sample_budget,  # noqa: E402
                                                sha256_file, validate_example)


class DatasetTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.attempts = self.root / "attempts"

    def tearDown(self):
        self.temp.cleanup()

    def test_split_guard_refuses_forbidden_splits(self):
        make_attempt(self.attempts, 1)
        for split in ("test", "final", "holdout"):
            with self.assertRaises(DatasetError):
                build_sft([self.attempts], self.root / f"out-{split}", allowed_splits=["train", split])

    def test_selection_filters(self):
        make_attempt(self.attempts, 1)
        make_attempt(self.attempts, 2, test_double=True)
        make_attempt(self.attempts, 3, ret=0.2)
        make_attempt(self.attempts, 4, status="timeout")
        make_attempt(self.attempts, 5, split="test")
        make_attempt(self.attempts, 6, split="final")
        make_attempt(self.attempts, 7, ret=None)
        make_attempt(self.attempts, 8, split="dev")
        manifest = build_sft([self.attempts], self.root / "out", min_return=0.5)
        self.assertEqual([s["attempt_id"] for s in manifest["source_attempts"]], ["att-001-train"])
        reasons = {r["attempt_id"]: r["reason"] for r in manifest["rejected"]}
        self.assertIn("test double", reasons["att-002-train"])
        self.assertIn("below threshold", reasons["att-003-train"])
        self.assertIn("not completed", reasons["att-004-train"])
        self.assertIn("forbidden split", reasons["att-005-test"])
        self.assertIn("forbidden split", reasons["att-006-final"])
        self.assertIn("no trusted official return", reasons["att-007-train"])
        self.assertIn("not in allowed", reasons["att-008-dev"])
        self.assertTrue(manifest["split_check"]["passed"])
        self.assertEqual(manifest["kind"], "weights")

    def test_curriculum_membership(self):
        make_attempt(self.attempts, 1)
        make_attempt(self.attempts, 2)
        curriculum = make_curriculum(self.root / "curriculum.json", ["ep-001-train"])
        manifest = build_sft([self.attempts], self.root / "out", curriculum_ref=curriculum)
        self.assertEqual(manifest["counts"]["examples"], 1)
        self.assertEqual(manifest["selection_rule"]["curriculum"]["sha256"], sha256_file(curriculum))
        self.assertIn("not in the curriculum", manifest["rejected"][0]["reason"])

    def test_example_format_and_manifest_hashes(self):
        directory = make_attempt(self.attempts, 1)
        manifest = build_sft([self.attempts], self.root / "out")
        text = (self.root / "out" / "dataset.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(HIDDEN_REASONING, text)
        self.assertNotIn("signature", text)
        example = json.loads(text.splitlines()[0])
        roles = [m["role"] for m in example["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "assistant"])
        first = example["messages"][2]
        self.assertNotIn("content", first)  # empty assistant text is dropped
        self.assertEqual(first["tool_calls"][0]["function"], {"name": "query", "arguments": "{\"table\": \"ledger\"}"})
        self.assertEqual(example["messages"][3], {"role": "tool", "tool_call_id": "toolu_1_1", "content": "row 1: 100.00"})
        self.assertEqual(example["messages"][-1]["content"], "Submitting the confirmed finding.")
        self.assertIs(example["parallel_tool_calls"], False)
        self.assertEqual(example["tools"][0]["type"], "function")
        self.assertEqual(validate_example(example), [])
        self.assertEqual(manifest["dataset"]["sha256"], sha256_file(self.root / "out" / "dataset.jsonl"))
        source = manifest["source_attempts"][0]
        self.assertEqual(source["files_sha256"]["transcript.jsonl"], sha256_file(directory / "transcript.jsonl"))
        self.assertEqual(source["files_sha256"]["trusted/evaluation.json"],
                         sha256_file(self.attempts / "trusted" / "att-001-train" / "evaluation.json"))
        self.assertEqual(source["files_sha256"]["steps.jsonl"], sha256_file(directory / "steps.jsonl"))
        self.assertIn("estimate", manifest["token_estimate"]["method"])
        self.assertEqual(manifest["teacher_sources"], {"oracle_demonstration": 1})
        self.assertEqual(source["teacher_source"], "oracle_demonstration")

    def test_layout_partial_dirs_and_mismatched_evaluation(self):
        make_attempt(self.attempts, 1)
        make_attempt(self.attempts, 2)
        (self.attempts / "attempts" / "att-002-train").rename(self.attempts / "attempts" / "att-002-train.partial")
        make_attempt(self.attempts, 3)
        evaluation = self.attempts / "trusted" / "att-003-train" / "evaluation.json"
        evaluation.write_text(json.dumps({"attempt_id": "someone-else", "official": {"return": 9.0}}), encoding="utf-8")
        manifest = build_sft([self.attempts], self.root / "out")
        self.assertEqual([s["attempt_id"] for s in manifest["source_attempts"]], ["att-001-train"])
        self.assertEqual([r["attempt_id"] for r in manifest["rejected"]], ["att-003-train"])
        with self.assertRaises(DatasetError):
            build_sft([self.attempts / "attempts"], self.root / "out2")  # not a ROOT

    def test_teacher_source_guard(self):
        make_attempt(self.attempts, 1)
        make_attempt(self.attempts, 2, agent=api_agent("anthropic", "claude-opus-5-5"))
        make_attempt(self.attempts, 3, agent=api_agent("openai_compatible", "student-base"))
        make_attempt(self.attempts, 4, agent=api_agent("openai_compatible", "ft:student-base:org:abc"))
        make_attempt(self.attempts, 5, agent={"kind": "baseline_policy", "policy": "p", "test_double": False})
        manifest = build_sft([self.attempts], self.root / "default", self_models=["student-base"])
        self.assertEqual(manifest["teacher_sources"], {"oracle_demonstration": 1, "self": 2})
        reasons = {r["attempt_id"]: r["reason"] for r in manifest["rejected"]}
        self.assertIn("third_party_model", reasons["att-002-train"])
        self.assertIn("baseline_policy", reasons["att-005-train"])
        with self.assertRaises(DatasetError):
            build_sft([self.attempts], self.root / "x", allowed_sources=["oracle_demonstration", "third_party_model"])
        with self.assertRaises(DatasetError):
            build_sft([self.attempts], self.root / "x", allowed_sources=["claude"])
        ack = "J. Reviewer, 2026-11-21"
        manifest = build_sft([self.attempts], self.root / "ack", allowed_sources=["third_party_model"],
                             teacher_terms_acknowledged=ack)
        self.assertEqual(manifest["teacher_sources"], {"third_party_model": 3})  # without self_models, all API teachers
        self.assertEqual(manifest["teacher_policy"]["teacher_terms_acknowledged"], ack)
        self.assertIn({"source": "third_party_model", "provider": "anthropic", "model": "claude-opus-5-5"},
                      manifest["teachers"])

    def test_validator_catches_unanswered_calls_and_bad_roles(self):
        call = {"id": "c1", "type": "function", "function": {"name": "query", "arguments": "{}"}}
        tools = [{"type": "function", "function": {"name": "query", "parameters": {}}}]
        bad = {"messages": [{"role": "user", "content": "x"}, {"role": "assistant", "tool_calls": [call]},
                            {"role": "user", "content": "y"}, {"role": "assistant", "content": "z"}], "tools": tools}
        self.assertTrue(any("not answered" in p for p in validate_example(bad)))
        orphan = {"messages": [{"role": "user", "content": "x"}, {"role": "tool", "tool_call_id": "zz", "content": "r"},
                               {"role": "assistant", "content": "z"}], "tools": tools}
        self.assertTrue(any("unknown" in p for p in validate_example(orphan)))
        unknown_tool = {"messages": [{"role": "user", "content": "x"}, {"role": "assistant", "tool_calls": [
            dict(call, function={"name": "rm", "arguments": "{}"})]}], "tools": tools}
        self.assertTrue(any("not in tools" in p for p in validate_example(unknown_tool)))
        self.assertTrue(any("exceeds" in p for p in validate_example(
            {"messages": [{"role": "user", "content": "x" * 400}, {"role": "assistant", "content": "y"}], "tools": []},
            max_example_tokens=50)))

    def test_equal_budget_sampling_is_deterministic(self):
        for n in range(1, 13):
            make_attempt(self.attempts / "a", n)
        for n in range(20, 27):
            make_attempt(self.attempts / "b", n)
        build_sft([self.attempts / "a"], self.root / "arm-a")
        build_sft([self.attempts / "b"], self.root / "arm-b")
        first = sample_budget(self.root / "arm-a", self.root / "s1", seed=11, max_examples=5)
        second = sample_budget(self.root / "arm-a", self.root / "s2", seed=11, max_examples=5)
        self.assertEqual(first["dataset"]["sha256"], second["dataset"]["sha256"])
        self.assertEqual(len(first["sampling"]["cut"]), 7)
        self.assertEqual(first["sampling"]["parent_sha256"], json.loads(
            (self.root / "arm-a" / "dataset_manifest.json").read_text())["dataset"]["sha256"])
        arms = equal_budget([self.root / "arm-a", self.root / "arm-b"], [self.root / "eq-a", self.root / "eq-b"], seed=3)
        self.assertEqual([a["counts"]["examples"] for a in arms], [7, 7])
        self.assertEqual(len({a["sampling"]["max_tokens"] for a in arms}), 1)
        self.assertTrue(all(a["token_estimate"]["total"] <= a["sampling"]["max_tokens"] for a in arms))
        with self.assertRaises(DatasetError):
            sample_budget(self.root / "arm-a", self.root / "s3", seed=1)


if __name__ == "__main__":
    unittest.main()
