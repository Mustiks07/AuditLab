"""Acceptance must reflect executed checks, not green suite labels or stale receipts."""
import copy
import unittest

import run
from auditlab.release.acceptance import GROUPS, REQUIRED, matrix

REVISION = "a" * 40


def receipt():
    suites = {}
    for requirements in REQUIRED.values():
        for suite, test in requirements:
            suites.setdefault(suite, set()).add(test)
    return {"commit": REVISION, "end_commit": REVISION, "dirty": False, "end_dirty": False,
            "run_id": "current", "checks": [{"id": suite, "status": "passed", "tests_run": len(tests),
                "evidence_revision": REVISION, "evidence_run_id": "current",
                "records": [{"id": test, "status": "passed"} for test in sorted(tests)]}
                for suite, tests in sorted(suites.items())]}


def groups(report):
    return {row["id"]: row for row in matrix(report, REVISION)}


class AcceptanceEvidenceTests(unittest.TestCase):
    def test_empty_and_green_suite_names_cannot_certify_groups(self):
        self.assertEqual(len(matrix()), 24)
        self.assertTrue(all(row["status"] == "not_checked" for row in matrix()))
        report = receipt()
        for suite in report["checks"]:
            suite["records"] = []
        self.assertTrue(all(row["status"] == "not_checked" for row in groups(report).values()))

    def test_explicit_complete_test_receipts_pass_only_covered_groups(self):
        result = groups(receipt())
        for group in ("API-01", "API-02", "API-03", "API-04", "API-05", "ISO-02", "ISO-03",
                      "DAT-02", "DAT-03", "DAT-05", "MAT-01", "MAT-02", "MAT-03", "MAT-04", "VER-01", "REP-03", "UI-01"):
            self.assertEqual(result[group]["status"], "passed", result[group])
        for group in ("DAT-01", "DAT-04", "REP-01", "REP-02", "OPS-01", "OPS-02"):
            self.assertEqual(result[group]["status"], "not_checked", group)
        self.assertEqual({g for values in GROUPS.values() for g in values}, set(REQUIRED))

    def test_skip_missing_duplicate_failure_and_subtest_failure_are_not_passes(self):
        suite_name, test_id = REQUIRED["ISO-01"][0]
        for defect, expected in (("skip", "not_checked"), ("missing", "not_checked"), ("duplicate", "not_checked"),
                                 ("failed", "failed"), ("subtest", "failed"), ("skipped_subtest", "not_checked")):
            with self.subTest(defect=defect):
                report = receipt()
                suite = next(s for s in report["checks"] if s["id"] == suite_name)
                row = next(r for r in suite["records"] if r["id"] == test_id)
                if defect == "skip": row["status"] = "not_checked"
                elif defect == "missing": suite["records"].remove(row)
                elif defect == "duplicate": suite["records"].append(dict(row))
                elif defect == "failed": row["status"] = "failed"
                else: suite["records"].append({"id": test_id + " (probe='truth')", "status": "not_checked" if defect == "skipped_subtest" else "failed"})
                self.assertEqual(groups(report)["ISO-01"]["status"], expected)

    def test_changed_checkout_dirty_tree_and_old_suite_receipts_cannot_certify(self):
        for defect in ("commit", "end_commit", "dirty", "end_dirty", "run_id", "suite_revision", "suite_run"):
            with self.subTest(defect=defect):
                report = receipt()
                if defect in ("commit", "end_commit"): report[defect] = "b" * 40
                elif defect in ("dirty", "end_dirty"): report[defect] = True
                elif defect == "run_id": report[defect] = "other"
                else:
                    for suite in report["checks"]:
                        suite["evidence_revision" if defect == "suite_revision" else "evidence_run_id"] = "old"
                self.assertTrue(all(r["status"] != "passed" for r in groups(report).values()))

    def test_archive_without_git_can_bind_its_expected_source_revision(self):
        from unittest.mock import patch
        import subprocess
        report = receipt()
        report["dirty"] = report["end_dirty"] = None
        with patch.object(run.subprocess, "check_output", side_effect=subprocess.CalledProcessError(128, "git")), \
                patch.dict(run.os.environ, {"AUDITLAB_SOURCE_REVISION": REVISION}):
            self.assertEqual(run.git_value("rev-parse", "HEAD"), REVISION)
            self.assertIsNone(run.git_value("status", "--porcelain"))
            rows = run.acceptance_plan(report, run.git_value("rev-parse", "HEAD"))
        self.assertEqual(next(r for r in rows if r["id"] == "API-01")["status"], "passed")
        self.assertEqual(run.exit_code(rows, False), 0)
        self.assertEqual(run.exit_code(rows, True), 2)

    def test_twenty_pairs_require_bound_detailed_distinct_receipts(self):
        report = receipt()
        report["rehearsal"] = {"evidence_revision": REVISION, "evidence_run_id": "current", "pairs": [
            {"id": f"pair-{i:02d}", "status": "passed", "oracle_score_agrees": True,
             "transformed_answer_agrees": True, "replay": {"status": "passed"}} for i in range(1, 21)]}
        self.assertEqual(groups(report)["REP-02"]["status"], "passed")
        for defect in ("missing", "duplicate", "skipped", "detail", "stale"):
            altered = copy.deepcopy(report)
            rows = altered["rehearsal"]["pairs"]
            if defect == "missing": rows.pop()
            elif defect == "duplicate": rows[-1] = dict(rows[0])
            elif defect == "skipped": rows[0]["status"] = "not_checked"
            elif defect == "detail": rows[0].pop("oracle_score_agrees")
            else: altered["rehearsal"]["evidence_run_id"] = "previous"
            self.assertNotEqual(groups(altered)["REP-02"]["status"], "passed", defect)

    def test_source_book_receipt_requires_all_snapshot_hash_bindings(self):
        report = receipt()
        files = {name: str(i) * 64 for i, name in enumerate(("manifest.json", "policy.json", "ledger.csv", "invoices.jsonl", "payments.jsonl", "fx.csv"))}
        source = {"status": "passed", "source_findings": 0, "source_files_sha256": files,
                  "target_files_sha256": files, "transition_sha256": "7" * 64,
                  "provenance_sha256": "8" * 64, "truth_sha256": "9" * 64}
        hashes = {prefix + name: digest for prefix in ("public/", "private/source_book/") for name, digest in files.items()}
        hashes.update({"private/source_transition.json": "7" * 64, "private/provenance.json": "8" * 64,
                       "private/truth.json": "9" * 64})
        report["rehearsal"] = {"evidence_revision": REVISION, "evidence_run_id": "current", "datasets": [
            {"id": f"internal-{i:02d}", "status": "passed", "source_book": copy.deepcopy(source),
             "snapshot_sha256": dict(hashes)} for i in range(1, 21)]}
        self.assertEqual(groups(report)["DAT-01"]["status"], "passed")
        report["rehearsal"]["datasets"][0]["snapshot_sha256"]["private/source_book/ledger.csv"] = "0" * 64
        self.assertEqual(groups(report)["DAT-01"]["status"], "not_checked")

    def test_incomplete_policy_summary_renders_without_a_fabricated_f1(self):
        from auditlab.release.rehearsal import pending_plan, summarize
        rehearsal = pending_plan()
        rehearsal["summary"] = summarize(rehearsal["episodes"])
        report = {"commit": REVISION, "python": "3.11", "dirty": False, "checks": [],
                  "acceptance": matrix(), "rehearsal": rehearsal}
        rendered = run.render(report)
        self.assertIn("не проверено", rendered)
        self.assertNotIn("None", rendered)
        self.assertIn("0 / 20", rendered)
        self.assertIn("Официальный адаптер", rendered)


if __name__ == "__main__":
    unittest.main()
