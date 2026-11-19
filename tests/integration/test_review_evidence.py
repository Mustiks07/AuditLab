"""A green report from an old PR revision must never approve the current one."""
from copy import deepcopy
import unittest

from tools.review_evidence import SUITES, validate


class ReviewEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.head, self.base, self.merge = "a" * 40, "b" * 40, "c" * 40
        self.context = dict(format_version="1.0", kind="pull_request_merge",
                            head_sha=self.head, base_sha=self.base, checkout_sha=self.merge,
                            tree_sha="d" * 40, parents=[self.base, self.head], dirty=False)
        self.report = dict(commit=self.merge, dirty=False, suite="quick", exit_code=0,
                           status="incomplete", checks=[dict(id=s, status="passed", tests_run=1,
                           records=[dict(id=s + ".test", status="passed")]) for s in sorted(SUITES)])

    def check(self, **kwargs):
        return validate(self.context, self.report, kwargs.get("head", self.head),
                        kwargs.get("base", self.base))

    def test_complete_tests_do_not_claim_issue_acceptance(self):
        result = self.check()
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["tests"], 8)
        self.assertIn("require review", result["scope"])

    def test_moved_head_or_base_rejects_old_report(self):
        self.assertEqual(self.check(head="e" * 40)["exit_code"], 1)
        self.assertEqual(self.check(base="e" * 40)["exit_code"], 1)
        self.assertEqual(self.check(base=None)["exit_code"], 1)

    def test_report_from_another_checkout_fails(self):
        self.report["commit"] = self.head
        self.assertEqual(self.check()["exit_code"], 1)

    def test_dirty_checkout_or_failed_process_fails(self):
        original = deepcopy(self.report)
        for key, value in (("dirty", True), ("dirty", None), ("exit_code", 1), ("suite", "rehearsal")):
            with self.subTest(key=key, value=value):
                self.report = {**original, key: value}
                self.assertEqual(self.check()["exit_code"], 1)

    def test_missing_skipped_duplicate_or_unexecuted_tests_fail(self):
        original = deepcopy(self.report)
        for mutation in ("missing", "skipped", "duplicate", "empty", "wrong_count"):
            with self.subTest(mutation=mutation):
                self.report = deepcopy(original)
                row = self.report["checks"][0]
                if mutation == "missing": self.report["checks"].pop()
                elif mutation == "skipped": row["records"][0]["status"] = "not_checked"
                elif mutation == "duplicate":
                    row["records"] *= 2
                    row["tests_run"] = 2
                elif mutation == "empty": row.update(records=[], tests_run=0)
                else: row["tests_run"] = 99
                self.assertEqual(self.check()["exit_code"], 1)

    def test_direct_revision_requires_matching_report_and_no_pr_base(self):
        self.context.update(kind="revision", checkout_sha=self.head, base_sha=None)
        self.report["commit"] = self.head
        self.assertEqual(self.check(base=None)["exit_code"], 0)
        self.assertEqual(self.check()["exit_code"], 1)
