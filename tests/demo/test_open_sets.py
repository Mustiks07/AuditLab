"""Strict summary of the open train/dev policy runs (HANDOFF section 5a).

Run: python -m unittest discover -s tests/demo -v
"""
import copy
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
        self.assertIn("нет score", problems)
        self.assertIn("нет числового reward: None", problems)

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



class InputValues(unittest.TestCase):
    """Integrator review of c34a05c: values must be valid, not only typed, and keys must be checked first."""

    def setUp(self):
        self.episodes = open_sets.expected_episodes(DATASETS)
        self.base = [open_sets.record(s, e, p, 0, json.dumps(
            {"status": "submitted", "reward": 1, "score": {"tp": 1, "fp": 0, "fn": 0}}), "")
            for s in open_sets.SPLITS for e in self.episodes[s] for p in open_sets.POLICIES]

    def assert_rejected(self, rows, policy="train/random", reason=None, raw=None):
        report = open_sets.summarize(raw if raw is not None else [json.dumps(r) for r in rows], self.episodes)
        row = report["policies"][policy]
        self.assertEqual((report["status"], report["exit_code"]), ("не проверено", 1))
        self.assertIsNone(row["mean_reward"])
        self.assertIsNone(row["micro"])
        self.assertEqual(row["measured"], 9)
        if reason:
            self.assertTrue(any(reason in f["problem"] for f in row["failed"]) or
                            any(reason in p for p in report["problems"]), (reason, row["failed"], report["problems"]))
        return report

    def test_plain_80_rows_still_pass(self):
        report = open_sets.summarize([json.dumps(r) for r in self.base], self.episodes)
        self.assertEqual((report["status"], report["exit_code"]), ("complete", 0))

    def test_integrator_nan_reward(self):
        rows = copy.deepcopy(self.base)
        rows[0]["result"]["reward"] = float("nan")
        self.assert_rejected(rows, reason="не конечное")

    def test_infinity_from_json_1e309(self):
        raw = [json.dumps(r) for r in self.base]
        raw[0] = raw[0].replace('"reward": 1', '"reward": 1e309')
        self.assertIn("1e309", raw[0])
        self.assert_rejected(None, reason="не конечное", raw=raw)

    def test_negative_and_boolean_counts(self):
        for name, value, reason in (("tp", -10, "отрицательное"), ("fp", -1, "отрицательное"),
                                    ("fn", True, "не целое"), ("tp", 1.0, "не целое")):
            with self.subTest(name=name, value=value):
                rows = copy.deepcopy(self.base)
                rows[0]["result"]["score"][name] = value
                self.assert_rejected(rows, reason=reason)

    def test_boolean_or_out_of_range_reward(self):
        for value, reason in ((True, "нет числового reward"), ("1", "нет числового reward"), (1.5, "вне диапазона"),
                              (-2, "вне диапазона")):
            with self.subTest(value=value):
                rows = copy.deepcopy(self.base)
                rows[0]["result"]["reward"] = value
                self.assert_rejected(rows, reason=reason)

    def test_return_code_must_be_an_integer_zero(self):
        for value in (False, True, "0", None, 0.0):
            with self.subTest(value=value):
                rows = copy.deepcopy(self.base)
                rows[0]["returncode"] = value
                self.assert_rejected(rows, reason="кода возврата")

    def test_invalid_identifiers_are_malformed_rows_without_traceback(self):
        for field, value in (("episode", None), ("episode", []), ("episode", ""), ("split", 3), ("policy", {"a": 1})):
            with self.subTest(field=field, value=value):
                rows = copy.deepcopy(self.base)
                rows[0][field] = value
                report = self.assert_rejected(rows, reason="непустыми строками split, episode, policy")
                self.assertEqual(report["policies"]["train/random"]["missing"], [self.base[0]["episode"]])

    def test_findings_over_the_submit_limit(self):
        rows = copy.deepcopy(self.base)
        rows[0]["result"]["score"].update(tp=20, fp=11)
        self.assert_rejected(rows, reason="больше лимита сдачи 30")

    def test_parse_error_must_be_null_for_a_measured_attempt(self):
        for value in ("", [], {}, False):
            with self.subTest(value=value):
                rows = copy.deepcopy(self.base)
                rows[0]["parse_error"] = value
                self.assert_rejected(rows, reason="некорректное поле parse_error")

    def test_row_that_is_not_an_object(self):
        raw = [json.dumps(r) for r in self.base]
        raw[0] = json.dumps(["train", self.base[0]["episode"], "random"])
        raw.append("42")
        self.assert_rejected(None, reason="JSON-объект", raw=raw)

    def test_cli_reports_invalid_values_instead_of_crashing(self):
        rows = copy.deepcopy(self.base)
        rows[0]["episode"] = None
        rows[1]["result"]["reward"] = float("inf")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.jsonl"
            path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = open_sets.main(["summarize", str(path), "--datasets", str(DATASETS)])
        self.assertEqual(code, 1)
        self.assertIn("НЕ ПРОВЕРЕНО: mean_reward null", out.getvalue())
        self.assertIn("непустыми строками", out.getvalue())


class UnreadableInput(unittest.TestCase):
    """Bad files and lines give a diagnostic report and exit 1, never a traceback."""

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = open_sets.main(["summarize", *map(str, args)])
        return code, out.getvalue()

    def test_deeply_nested_line(self):
        report = open_sets.summarize(["[" * 200000], open_sets.expected_episodes(DATASETS))
        self.assertEqual(report["exit_code"], 1)
        self.assertTrue(any("RecursionError" in p for p in report["problems"]))

    def test_deeply_nested_child_output_is_a_parse_error(self):
        row = open_sets.record("train", "E", "random", 0, "[" * 200000, "")
        self.assertIsNone(row["result"])
        self.assertIn("RecursionError", row["parse_error"])

    def test_non_utf8_line_missing_file_directory_and_datasets(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "bad.jsonl").write_bytes(b'\xff\xfe{"split": 1}\n')
            code, out = self.run_cli(tmp / "bad.jsonl", "--datasets", DATASETS)
            self.assertEqual(code, 1)
            self.assertIn("не в кодировке UTF-8", out)
            code, out = self.run_cli(tmp / "missing.jsonl")
            self.assertEqual(code, 1)
            self.assertIn("не найден", out)
            (tmp / "dir.jsonl").mkdir()
            code, out = self.run_cli(tmp / "dir.jsonl")
            self.assertEqual(code, 1)
            self.assertIn("не прочитан", out)
            (tmp / "one.jsonl").write_text("{}\n", encoding="utf-8")
            code, out = self.run_cli(tmp / "one.jsonl", "--datasets", tmp / "no-datasets")
            self.assertEqual(code, 1)
            self.assertIn("не прочитаны", out)


class OddValues(unittest.TestCase):
    """Every field of a row replaced by odd JSON values: no exception, and never counted as measured."""

    ODD = [None, [], {}, "", "x", True, False, -1, 1.5, float("nan"), float("inf"), -float("inf"), 10 ** 30,
           [[[]]], {"tp": 1}]
    # Odd but legitimate: no parse error, the lowest reward, and a large miss count (SPEC bounds only findings).
    VALID = [(("parse_error",), None), (("result", "reward"), -1), (("result", "score", "fn"), 10 ** 30)]

    def legitimate(self, path, value):
        return any(path == p and type(value) is type(v) and value == v for p, v in self.VALID)

    def test_no_exception_and_no_false_measurement(self):
        episodes = open_sets.expected_episodes(DATASETS)
        base = [open_sets.record(s, e, p, 0, json.dumps(
            {"status": "submitted", "reward": 1, "score": {"tp": 1, "fp": 0, "fn": 0}}), "")
            for s in open_sets.SPLITS for e in episodes[s] for p in open_sets.POLICIES]
        paths = [("returncode",), ("parse_error",), ("result",), ("result", "status"), ("result", "reward"),
                 ("result", "score"), ("result", "score", "tp"), ("result", "score", "fp"),
                 ("result", "score", "fn"), ("split",), ("episode",), ("policy",)]
        for path in paths:
            for value in self.ODD:
                rows = copy.deepcopy(base)
                target = rows[0]
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] = value
                with self.subTest(path=path, value=value):
                    report = open_sets.summarize([json.dumps(r) for r in rows], episodes)  # must not raise
                    if self.legitimate(path, value):
                        self.assertEqual((report["status"], report["exit_code"]), ("complete", 0))
                        continue
                    self.assertEqual(report["status"], "не проверено")
                    self.assertNotEqual(report["exit_code"], 0)
                    self.assertIsNone(report["policies"]["train/random"]["mean_reward"])

class HandoffCopy(unittest.TestCase):
    def test_handoff_embeds_the_shipped_tool_byte_for_byte(self):
        text = (ROOT / "docs" / "demo" / "HANDOFF.md").read_text(encoding="utf-8")
        block = re.search(r"cat > open_sets\.py <<'OPEN_SETS_PY'\n(.*?)\nOPEN_SETS_PY\n", text, re.S)
        self.assertIsNotNone(block, "HANDOFF section 5a must embed open_sets.py for RC3")
        shipped = (ROOT / "auditlab" / "demo" / "open_sets.py").read_text(encoding="utf-8")
        self.assertEqual(block.group(1) + "\n", shipped)


if __name__ == "__main__":
    unittest.main()
