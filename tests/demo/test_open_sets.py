"""Strict summary of the open train/dev policy runs (HANDOFF section 5a).

Run: python -m unittest discover -s tests/demo -v
"""
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from auditlab.demo import open_sets

ROOT = Path(__file__).resolve().parents[2]
DATASETS = ROOT / "datasets" / "open"


def good(split, episode, policy, tp=1, fp=0, fn=0, reward=0.982):
    result = {"status": "submitted", "reward": reward, "inspection_steps": 9,
              "score": {"tp": tp, "fp": fp, "fn": fn, "f1": 1.0, "reward": reward}}
    return open_sets.record(split, episode, policy, 0, json.dumps(result) + "\n", "")


def full_run():
    episodes = open_sets.expected_episodes(DATASETS)
    return [good(split, episode, policy) for split in open_sets.SPLITS for episode in episodes[split]
            for policy in open_sets.POLICIES], episodes


def lines(rows):
    return [json.dumps(row) for row in rows]


class Summary(unittest.TestCase):
    def test_complete_run_is_accepted(self):
        rows, episodes = full_run()
        report = open_sets.summarize(lines(rows), episodes)
        self.assertEqual((report["status"], report["exit_code"], report["rows"]), ("complete", 0, 80))
        self.assertEqual(report["policies"]["train/reconcile"]["micro"], {"tp": 10, "fp": 0, "fn": 0})
        self.assertEqual(report["policies"]["dev/oracle"]["mean_reward"], 0.982)

    def test_integrator_counterexample_has_no_overall_mean(self):
        # submitted reward=1 TP=1, and an infrastructure_error without score/reward, for one policy.
        ok = open_sets.record("train", "E1", "reconcile", 0, json.dumps(
            {"status": "submitted", "reward": 1, "score": {"tp": 1, "fp": 0, "fn": 0}}), "")
        broken = open_sets.record("train", "E2", "reconcile", 2, json.dumps({"status": "infrastructure_error"}), "boom")
        report = open_sets.summarize(lines([ok, broken]), {"train": ["E1", "E2"], "dev": []})
        row = report["policies"]["train/reconcile"]
        self.assertNotEqual(report["exit_code"], 0)
        self.assertEqual(report["status"], "не проверено")
        self.assertIsNone(row["mean_reward"])
        self.assertIsNone(row["micro"])
        self.assertEqual(row["measured_only"], {"measured": 1, "planned": 2, "tp": 1, "fp": 0, "fn": 0, "mean_reward": 1.0})
        self.assertIn("кодом 2", row["failed"][0]["problem"])

    def test_failed_process_is_an_error_even_with_a_valid_json_line(self):
        rows, episodes = full_run()
        rows[0] = open_sets.record(rows[0]["split"], rows[0]["episode"], rows[0]["policy"], 1,
                                   json.dumps({"status": "submitted", "reward": 1, "score": {"tp": 1, "fp": 0, "fn": 0}}), "")
        report = open_sets.summarize(lines(rows), episodes)
        self.assertEqual(report["exit_code"], 1)
        name = f"{rows[0]['split']}/{rows[0]['policy']}"
        self.assertIsNone(report["policies"][name]["mean_reward"])
        self.assertEqual(report["policies"][name]["measured_only"]["measured"], 9)

    def test_missing_score_or_reward_is_not_counted_as_zero(self):
        rows, episodes = full_run()
        rows[5]["result"].pop("score")
        rows[6]["result"].pop("reward")
        report = open_sets.summarize(lines(rows), episodes)
        self.assertEqual(report["exit_code"], 1)
        problems = [f["problem"] for row in report["policies"].values() for f in row["failed"]]
        self.assertIn("нет score с целыми tp/fp/fn", problems)
        self.assertIn("нет reward", problems)

    def test_unparsed_output_and_no_output(self):
        self.assertEqual(open_sets.record("train", "E", "random", 0, "Traceback ...\n", "")["parse_error"][:4], "last")
        self.assertEqual(open_sets.record("train", "E", "random", 0, "", "")["parse_error"], "no output")
        self.assertEqual(open_sets.record("train", "E", "random", 0, "[1]\n", "")["result"], None)

    def test_empty_file(self):
        report = open_sets.summarize([], open_sets.expected_episodes(DATASETS))
        self.assertEqual((report["status"], report["exit_code"]), ("не проверено", 1))
        self.assertIn("файл результатов пуст", report["problems"])
        self.assertTrue(all(row["mean_reward"] is None for row in report["policies"].values()))

    def test_missing_line(self):
        rows, episodes = full_run()
        gone = rows.pop(3)
        report = open_sets.summarize(lines(rows), episodes)
        row = report["policies"][f"{gone['split']}/{gone['policy']}"]
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(row["missing"], [gone["episode"]])
        self.assertIsNone(row["mean_reward"])

    def test_missing_line_without_expected_ids(self):
        rows, _ = full_run()
        rows = [r for r in rows if r["episode"] != rows[0]["episode"]]  # a whole company is absent
        report = open_sets.summarize(lines(rows), None)
        self.assertNotEqual(report["exit_code"], 0)
        self.assertTrue(any("компаний 9" in p for p in report["problems"]))

    def test_repeated_line(self):
        rows, episodes = full_run()
        rows.append(dict(rows[7]))
        report = open_sets.summarize(lines(rows), episodes)
        self.assertEqual(report["exit_code"], 1)
        self.assertTrue(any(p.startswith("повтор строки") for p in report["problems"]))
        row = report["policies"][f"{rows[7]['split']}/{rows[7]['policy']}"]
        self.assertIsNone(row["mean_reward"])
        self.assertEqual(row["measured"], 9)

    def test_unexpected_episode_and_bad_line(self):
        rows, episodes = full_run()
        extra = good("dev", "E_NOT_IN_SET", "random")
        report = open_sets.summarize(lines(rows + [extra]) + ["{not json"], episodes)
        self.assertEqual(report["exit_code"], 1)
        self.assertTrue(any("неожиданная" in p for p in report["problems"]))
        self.assertTrue(any("не разобрана" in p for p in report["problems"]))

    def test_cli_prints_null_and_returns_nonzero(self):
        ok = good("train", "E1", "reconcile")
        broken = open_sets.record("train", "E2", "reconcile", 1, "", "Traceback")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.jsonl"
            path.write_text("\n".join(lines([ok, broken])) + "\n", encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = open_sets.main(["summarize", str(path), "--output", str(Path(tmp) / "r.json")])
            self.assertNotEqual(code, 0)
            self.assertIn("НЕ ПРОВЕРЕНО: mean_reward null", out.getvalue())
            self.assertIsNone(json.loads((Path(tmp) / "r.json").read_text())["policies"]["train/reconcile"]["mean_reward"])

    def test_attempt_commands_match_the_runner_cli(self):
        self.assertEqual(open_sets.attempt_command("train", "E1", "reconcile", "sha256:p", "/results")[2:6],
                         ["auditlab.runner", "policy", "--snapshot", "datasets/open/train/E1"])
        self.assertEqual(open_sets.attempt_command("dev", "E1", "oracle", "sha256:p", "/results")[3], "oracle")


class HandoffCopy(unittest.TestCase):
    def test_handoff_embeds_the_shipped_tool_byte_for_byte(self):
        text = (ROOT / "docs" / "demo" / "HANDOFF.md").read_text(encoding="utf-8")
        block = re.search(r"cat > open_sets\.py <<'OPEN_SETS_PY'\n(.*?)\nOPEN_SETS_PY\n", text, re.S)
        self.assertIsNotNone(block, "HANDOFF section 5a must embed open_sets.py for RC3")
        shipped = (ROOT / "auditlab" / "demo" / "open_sets.py").read_text(encoding="utf-8")
        self.assertEqual(block.group(1) + "\n", shipped)


if __name__ == "__main__":
    unittest.main()
