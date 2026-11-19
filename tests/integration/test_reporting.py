"""Regression checks for false green, partial results, and immutable attempts."""
import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run


class ReportingTests(unittest.TestCase):
    def test_missing_and_failed_checks_have_distinct_exit_codes(self):
        pending = [{"status": "passed"}, {"status": "not_checked"}]
        self.assertEqual(run.exit_code(pending), 0)
        self.assertEqual(run.exit_code(pending, True), 2)
        self.assertEqual(run.exit_code(pending + [{"status": "failed"}], True), 1)

    def test_worker_records_failure_skip_and_import_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tests = root / "tests" / "sample"
            tests.mkdir(parents=True)
            (tests / "test_sample.py").write_text(
                'import unittest\n'
                'class Example(unittest.TestCase):\n'
                ' def test_failure(self): self.assertEqual(1, 2)\n'
                ' @unittest.skip("component absent")\n'
                ' def test_missing(self): pass\n'
                ' def test_broken_dependency(self): import nonexistent_auditlab_dependency\n')
            output = root / "report"
            output.mkdir()
            result = run.run_suite(root, "sample", output, 10)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["tests_run"], 3)
            self.assertEqual({r["kind"] for r in result["records"]},
                             {"test_failure", "missing_component", "infrastructure_error"})
            self.assertIn("AssertionError", (output / "sample.log").read_text())

    def test_empty_suite_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "tests" / "empty"
            directory.mkdir(parents=True)
            (directory / "test_empty.py").write_text("# no tests\n")
            result = run.run_suite(root, "empty", root, 10)
            self.assertEqual(result["status"], "not_checked")
            self.assertEqual(result["tests_run"], 0)

    def test_timeout_is_infrastructure_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "tests" / "slow"
            directory.mkdir(parents=True)
            (directory / "test_slow.py").write_text("import time\ntime.sleep(5)\n")
            result = run.run_suite(root, "slow", root, 0.2)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["kind"], "infrastructure_error")

    def test_all_planned_runs_remain_in_denominator(self):
        plan = run.rehearsal_plan()
        self.assertEqual(len(plan["episodes"]), 80)
        self.assertEqual(len({r["id"] for r in plan["episodes"]}), 80)
        self.assertEqual(len(plan["pairs"]), 20)
        self.assertEqual(len(run.acceptance_plan()), 24)
        self.assertTrue(all(r["status"] == "not_checked" for r in plan["episodes"] + plan["pairs"]))

    def test_interrupted_report_persists_and_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "attempt"
            args = argparse.Namespace(output=output, suite="quick", timeout=10, require_complete=False)
            with patch.object(run, "run_suite", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    run.verify(args)
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(report["status"], "in_progress")
            with self.assertRaises(FileExistsError):
                run.verify(args)
            self.assertEqual(json.loads((output / "report.json").read_text()), report)


if __name__ == "__main__":
    unittest.main()
