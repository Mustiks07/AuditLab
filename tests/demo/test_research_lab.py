"""Financial laboratory projection: related situations, error dossier, results map, export of families.

Data: the labelled development fixture (real open companies, scripted actions, real evaluator) plus
candidate artifacts written by the test. Nothing here is presented as a measurement.
Run: python -m unittest discover -s tests/demo -v
"""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from auditlab.demo import engines, research, research_fixtures, research_lab

REAL = engines.runner()[0] is not None

try:
    from fastapi.testclient import TestClient
    from auditlab.demo.paths import DemoPaths
    from auditlab.demo.server import create_app
except ImportError:  # pragma: no cover
    TestClient = None


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def candidate(artifact_id, snapshot, **fields):
    return {"schema_version": "1.0", "artifact_kind": "candidate", "artifact_id": artifact_id, "source_sha": "abc",
            "protocol_id": "p1", "provenance": {"kind": "dev_fixture"}, "family_id": "family-b", "split": "train",
            "snapshot_ref": f"snapshots/{snapshot}/public", **fields}


def add_lab_artifacts(root: Path) -> None:
    design = {"stratum": "period_boundary", "issue_types": ["payment_allocation"], "clean": False,
              "settlement_timing": "after", "currencies": ["KZT"]}
    write(root / "candidates/cand-b.json", candidate("cand-b", "company-b", role="original", design=design,
                                                     validation_id="val-company-b"))
    write(root / "candidates/cand-b-renamed.json", candidate(
        "cand-b-renamed", "company-b-renamed", role="variant", parent_id="cand-b", transformation="id_order",
        change={"parameter": "ids_and_order"}, design=design, validation_id="val-company-b-renamed",
        expected=[{"type": "payment_allocation", "claimed_amount_minor": 2000000, "currency": "KZT"}], expected_check="passed"))
    write(root / "candidates/cand-b-fixed.json", candidate(
        "cand-b-fixed", "clean", role="variant", parent_id="cand-b", transformation="fixed",
        change={"parameter": "settlement_timing", "from": "after", "to": "before"},
        design={**design, "issue_types": [], "clean": True, "settlement_timing": "before"}, validation_id="val-clean",
        expected=[], expected_check="passed"))
    # Evaluator rejects a claim whose type and amount match the independent result: wrong evidence.
    base = json.loads((root / "experiments/exp-memory-notes/attempts/att-b-notes-1.json").read_text(encoding="utf-8"))
    wrong = copy.deepcopy(base)
    wrong.update(artifact_id="att-b-wrong-evidence", repetition=9,
                 evaluation={**base["evaluation"], "tp": 0, "fp": 1, "fn": 2, "reward": -1.0})
    wrong["submission"]["findings"][0]["evidence_ids"] = ["policy/allocation"]
    write(root / "experiments/exp-memory-notes/attempts/att-b-wrong-evidence.json", wrong)


@unittest.skipUnless(REAL, "needs the merged runner, evaluator, verifier and generator")
class Laboratory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        research_fixtures.build(cls.tmp / "fx")
        add_lab_artifacts(cls.tmp / "fx")
        cls.store = research.ResearchStore([cls.tmp / "fx"])
        cls.lab = research_lab.lab(research.Projection(cls.store))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def outcome(self, attempt_id):
        return self.lab.dossier(attempt_id)["outcome"]

    def test_outcomes_are_classified_from_evaluator_and_independent_result(self):
        wrong = self.outcome("att-b-plain-1")
        self.assertEqual(wrong["primary"], "wrong_amount")
        self.assertEqual(wrong["claims"][0]["reference"]["claimed_amount_minor"], 2000000)
        self.assertEqual(self.outcome("att-c-plain-1")["primary"], "false_accusation")
        self.assertEqual(self.outcome("att-a-notes-1")["primary"], "miss")
        self.assertEqual(self.outcome("att-b-wrong-evidence")["primary"], "wrong_evidence")
        technical = self.outcome("att-stop-provider-error")
        self.assertEqual((technical["primary"], technical["claims"]), ("technical", []))
        self.assertIsNone(technical.get("evaluation"))

    def test_family_joins_attempts_by_public_content_and_reports_unavailable_slots(self):
        view = self.lab.family("cand-b-renamed")
        self.assertEqual(view["original"]["candidate_id"], "cand-b")
        self.assertEqual([v["transformation"] for v in view["variants"]], ["id_order", "fixed"])
        self.assertEqual(view["unavailable"], ["lawful_refund", "currency", "boundary"])
        renamed = view["variants"][0]
        self.assertEqual([a["artifact_id"] for a in renamed["attempts"]], ["att-n-plain-1"])
        summary = view["original"]["situation"]["designated"]["summary"]
        self.assertEqual((summary["received_minor"], summary["allocated_minor"], summary["residual_minor"]),
                         (5000000, 3000000, 2000000))
        self.assertEqual(view["variants"][1]["expected"], [])
        self.assertEqual(view["original"]["reference"]["source"], "independent")

    def test_dossier_lists_evidence_repeats_reduction_and_control(self):
        d = self.lab.dossier("att-b-plain-1")
        missing = {row["key"] for row in d["evidence"] if not row["received"]}
        # The scripted agent never listed the ledger: both settlement rows of the residual are missing.
        self.assertTrue({"ledger/R53432EC1D005F5311538DE28", "ledger/RB74667B32C20460A9F0BCE8C"} <= missing, missing)
        self.assertEqual({r["artifact_id"] for r in d["repeats"]} >= {"att-b-plain-1", "att-b-plain-2", "att-b-plain-3"}, True)
        self.assertIsNotNone(d["reduced"])
        self.assertTrue(d["control"]["available"])
        self.assertEqual(d["control"]["candidate_id"], "cand-b-fixed")
        self.assertEqual([a["outcome"]["primary"] for a in d["control"]["attempts"]], ["false_accusation"])

    def test_map_marks_missing_cells_not_checked_and_partial_runs(self):
        m = self.lab.results_map()
        clean = next(r for r in m["dimensions"]["clean"] if r["value"] == "clean")
        self.assertEqual(clean["cells"]["exp-memory-notes/notes"]["state"], "not_checked")
        self.assertEqual(clean["cells"]["exp-memory-notes/notes"]["attempts"], 0)
        stop = next(r for r in m["dimensions"]["violation"] if r["value"] == "payment_allocation")
        self.assertEqual(stop["cells"]["exp-budget-stop/plain"]["state"], "partial")
        self.assertEqual(stop["cells"]["exp-budget-stop/plain"]["outcomes"]["technical"], 4)
        self.assertEqual(sorted(e["experiment_id"] for e in m["incomplete"]), ["exp-budget-stop", "exp-model-unavailable"])

    def test_before_after_shows_regressions_and_not_comparable(self):
        m = self.lab.results_map(before="exp-memory-notes/notes", after="exp-memory-notes/plain")
        r = m["regression"]
        self.assertEqual((r["before"], r["after"]), ("exp-memory-notes/notes", "exp-memory-notes/plain"))
        self.assertEqual(sum(r["counts"].values()), len(r["items"]))
        self.assertGreaterEqual(r["counts"]["not_comparable"], 1, "companies attempted by one variant only")
        reverse = self.lab.results_map(before="exp-budget-stop/plain", after="exp-memory-notes/plain")["regression"]
        stopped = [i for i in reverse["items"] if i["before"] == "unavailable"]
        self.assertTrue(stopped and all(i["change"] == "not_comparable" for i in stopped))

    def test_private_truth_never_leaves_the_projection(self):
        dump = json.dumps([self.lab.overview(), self.lab.family("cand-b"), self.lab.dossier("att-b-plain-1"),
                           self.lab.results_map()], ensure_ascii=False)
        for word in ("evidence_sets", "trusted", "truth.json", "\"seed\""):
            self.assertNotIn(word, dump)


@unittest.skipUnless(REAL and TestClient is not None, "needs FastAPI test client and engines")
class LabApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        research_fixtures.build(cls.tmp / "fx")
        add_lab_artifacts(cls.tmp / "fx")
        cls.client = TestClient(create_app(DemoPaths(cls.tmp / "demo").ensure(), build_samples=False,
                                           research_roots=[cls.tmp / "fx"]))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_routes(self):
        self.assertEqual(self.client.get("/research/lab").json()["families"][0]["candidate_id"], "cand-b")
        self.assertEqual(len(self.client.get("/research/lab/families/cand-b").json()["variants"]), 2)
        self.assertEqual(self.client.get("/research/lab/dossiers/att-c-plain-1").json()["outcome"]["primary"], "false_accusation")
        reply = self.client.get("/research/lab/map", params={"experiment": "exp-memory-notes"}).json()
        self.assertTrue(all(c["id"].startswith("exp-memory-notes/") for c in reply["columns"]))
        self.assertEqual(self.client.get("/research/lab/families/nope").status_code, 404)
        self.assertEqual(self.client.get("/research/lab/dossiers/nope").status_code, 404)


class Export(unittest.TestCase):
    def test_status_detail_becomes_public_error(self):
        from auditlab.research.console_export import _status_error
        self.assertEqual(_status_error({"status": "provider_error", "status_detail": "rate_limit: HTTP 429"}),
                         {"code": "rate_limit", "message": "HTTP 429"})
        self.assertIsNone(_status_error({"status": "completed", "status_detail": "x"}))
        self.assertEqual(_status_error({"status": "timeout", "status_detail": "Wall clock exceeded"}),
                         {"code": "timeout", "message": "Wall clock exceeded"})

    @unittest.skipUnless(REAL, "needs open snapshots")
    def test_publish_families_exports_open_candidates_and_variants_only(self):
        from auditlab.research.artifacts import validate
        from auditlab.research.console_export import publish_families
        tmp = Path(tempfile.mkdtemp())
        try:
            src = research_fixtures.COMPANY_B / "public"
            root = tmp / "search"
            def snapshot(path):
                shutil.copytree(src, path / "public")
            def receipt(key, split):
                snapshot(root / "candidates" / key / "snapshot")
                write(root / "candidates" / key / "candidate.json", {"candidate": {"candidate_id": key, "seed": 7, "stratum": "period_boundary",
                      "config": {"config_version": "1.1", "settlement_timing": "after", "currencies": ["KZT"], "base_currency": "KZT",
                                 "issue_counts": {"payment_allocation": 1}}}})
            receipt("train-a", "train"); receipt("test-a", "test")
            answer = {"findings": [{"type": "payment_allocation", "claimed_amount_minor": 2000000, "currency": "KZT",
                                    "affected_ids": ["payment/P1"], "evidence_ids": ["policy/allocation"]}], "arithmetic": ["a"]}
            for name in ("baseline-validation", "id_order"):
                write(root / "pairs/train-a" / name / "validation.json", {"status": "passed", "checks": {}})
                write(root / "pairs/train-a" / name / "answer_before_truth.json", answer)
            snapshot(root / "pairs/train-a/snapshots/id_order")
            write(root / "experiment.json", {"source_sha": "s", "protocol_id": "p", "partition": {"members": [
                {"candidate_id": "train-a", "family_id": "f1", "split": "train"},
                {"candidate_id": "test-a", "family_id": "f2", "split": "test"}]},
                "paired": [{"candidate_id": "train-a-id_order", "parent": "train-a", "kind": "id_order", "reason": "r",
                            "status": "passed", "config": {"settlement_timing": "after", "issue_counts": {"payment_allocation": 1}},
                            "expected": [{**answer["findings"][0]}]}]})
            result = publish_families(root, tmp / "console")
            self.assertEqual(result["candidates"], 2)
            files = sorted(p.name for p in (tmp / "console/candidates").iterdir())
            self.assertEqual(files, ["train-a-id_order.json", "train-a.json"])
            variant = json.loads((tmp / "console/candidates/train-a-id_order.json").read_text(encoding="utf-8"))
            validate(variant)
            self.assertEqual(variant["expected"], [{"type": "payment_allocation", "claimed_amount_minor": 2000000, "currency": "KZT"}])
            text = (tmp / "console").as_posix()
            dump = "".join(p.read_text(encoding="utf-8") for p in (tmp / "console").rglob("*.json") if "snapshots" not in p.parts)
            self.assertNotIn("\"seed\"", dump)
            self.assertNotIn(str(root), dump)
            self.assertFalse((tmp / "console/snapshots/test-a").exists(), text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
