"""Research console projection: fixtures from real data, validation, private data, controller boundary.

Run: python -m unittest discover -s tests/demo -v
"""
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

from auditlab.demo import engines, research, research_fixtures

REAL = engines.runner()[0] is not None

try:
    from fastapi.testclient import TestClient
    from auditlab.demo.server import create_app
    from auditlab.demo.paths import DemoPaths
except ImportError:  # pragma: no cover
    TestClient = None


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")


def common(kind, artifact_id, **fields):
    return {"schema_version": "1.0", "artifact_kind": kind, "artifact_id": artifact_id, "source_sha": "abc",
            "protocol_id": "p1", **fields}


@unittest.skipUnless(REAL, "needs the merged runner, evaluator, verifier and generator")
class Fixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        research_fixtures.build(cls.tmp / "fx")
        cls.store = research.ResearchStore([cls.tmp / "fx"])
        cls.view = research.Projection(cls.store)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_artifact_is_marked_as_a_fixture(self):
        kinds = {a["_provenance"]["kind"] for items in self.store.artifacts.values() for a in items.values()}
        self.assertEqual(kinds, {"dev_fixture"})
        self.assertEqual([i["file"] for i in self.store.incompatible], ["incompatible/experiment-v2.json"])

    def test_failure_is_explained_by_observed_actions_and_independent_check(self):
        view = self.view.failure("fail-boundary-residual")
        self.assertEqual([c["result_minor"] for c in view["situation"]["calculations"]], [2000000])
        self.assertEqual([f["claimed_amount_minor"] for f in view["independent"]["findings"]], [2000000])
        failed = [a for a in view["attempts"] if a["evaluation"]["tp"] == 0]
        passed = [a for a in view["attempts"] if a["evaluation"]["tp"] == 1]
        self.assertEqual((len(failed), len(passed)), (2, 1))
        for attempt in failed:
            self.assertEqual([c["claimed_amount_minor"] for c in attempt["claims"]], [5000000])
            missing = [row["key"] for row in attempt["needed_received"] if not row["received"]]
            self.assertTrue(missing and all(k.startswith("ledger/") for k in missing), missing)
        self.assertTrue(all(row["received"] for row in passed[0]["needed_received"]))

    def test_reduction_keeps_the_error_and_the_original_is_unchanged(self):
        original = (research_fixtures.COMPANY_B / "public" / "ledger.csv").read_bytes()
        view = self.view.reduction("red-boundary-residual")
        self.assertEqual((view["original"]["ledger_rows"], view["reduced"]["ledger_rows"]), (240, 8))
        reduced = view["reduced"]["validation"]["independent"]["findings"]
        self.assertEqual([(f["type"], f["claimed_amount_minor"]) for f in reduced], [("payment_allocation", 2000000)])
        self.assertEqual(view["repeats"]["reduced"]["failed"], 2)
        relations = {f["relation"]: f for f in view["families"]}
        self.assertEqual(relations["defect_fixed"]["expected"], [])
        self.assertEqual(relations["renamed"]["expected"], [["payment_allocation", 2000000, "KZT"]])
        self.assertEqual((research_fixtures.COMPANY_B / "public" / "ledger.csv").read_bytes(), original)

    def test_operational_failures_have_no_financial_result(self):
        view = self.view.experiment("exp-budget-stop")
        statuses = {a["status"]: a for a in view["attempts"]}
        self.assertEqual(set(statuses), {"timeout", "provider_error", "invalid_response", "budget_stop"})
        for attempt in statuses.values():
            self.assertIsNone(attempt["evaluation"])
            self.assertEqual(attempt["claims"], [])
        self.assertEqual(view["experiment"]["stop_reason"]["code"], "budget_exceeded")
        detail = self.view.attempt("att-stop-provider-error")
        self.assertEqual(detail["error"]["code"], "provider_error")
        self.assertEqual(self.view.experiment("exp-model-unavailable")["experiment"]["stop_reason"]["code"], "model_unavailable")

    def test_records_resolve_to_exact_public_lines(self):
        view = self.view.failure("fail-boundary-residual")
        from auditlab.demo import service
        record = service.snapshot_record_view(self.store.snapshot(view["snapshot_id"]), "ledger/RB74667B32C20460A9F0BCE8C")
        self.assertEqual((record["file"], record["line"]), ("public/ledger.csv", 76))
        self.assertIn("RB74667B32C20460A9F0BCE8C", next(c["text"] for c in record["context"] if c["line"] == 76))

    def test_truth_is_never_projected(self):
        dump = json.dumps(self.view.overview()) + json.dumps(self.view.failure("fail-boundary-residual"))
        dump += json.dumps(self.view.reduction("red-boundary-residual"))
        self.assertNotIn("evidence_sets", dump)
        self.assertNotIn("trusted", dump)


class Validation(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def store(self):
        return research.ResearchStore([self.root])

    def test_incompatible_artifacts_are_listed_with_reasons(self):
        write(self.root / "a" / "experiment.json", common("experiment", "e1", schema_version="2.0"))
        write(self.root / "b" / "experiment.json", common("experiment", "e2", progress={"planned": -1}))
        write(self.root / "c" / "experiment.json", common("experiment", "e3", progress={"completed": True}))
        write(self.root / "d" / "experiment.json", json.dumps(common("experiment", "e5", cost={"usd": float("nan")})))
        write(self.root / "e" / "experiment.json", "{not json")
        write(self.root / "f" / "thing.json", common("unknown", "e4"))
        write(self.root / "g" / "experiment.json", common("experiment", "../bad"))
        write(self.root / "h" / "experiment.json", common("experiment", "ok"))
        write(self.root / "i" / "experiment.json", common("experiment", "ok"))
        store = self.store()
        self.assertEqual(sorted(store.artifacts["experiment"]), ["ok"])
        reasons = " | ".join(i["reason"] for i in store.incompatible)
        for text in ("версия схемы", "счётчик", "не конечное", "JSON", "тип артефакта", "artifact_id", "повтор"):
            self.assertIn(text, reasons)

    def test_private_keys_are_removed_recursively(self):
        write(self.root / "experiment.json", common("experiment", "e1", api_key="sk-secret", agents=[
            {"label": "x", "config": {"seed": 1}, "token_secret": "t"}], truth={"issues": []}))
        data = self.store().get("experiment", "e1")
        dump = json.dumps(data)
        for word in ("sk-secret", "api_key", "truth", "seed", "token_secret"):
            self.assertNotIn(word, dump)

    def test_snapshot_refs_cannot_leave_the_root_or_open_private_data(self):
        (self.root / "snapshots" / "x" / "public").mkdir(parents=True)
        (self.root / "snapshots" / "x" / "public" / "manifest.json").write_text("{}")
        (self.root / "trusted" / "public").mkdir(parents=True)
        (self.root / "trusted" / "public" / "manifest.json").write_text("{}")
        write(self.root / "attempt-1.json", common("attempt", "a1", snapshot_ref="snapshots/x/public"))
        store = self.store()
        self.assertIsNotNone(store.snapshot_id("attempt", "a1", "snapshots/x/public"))
        for ref in ("../x/public", "/etc/public", "trusted/public", "snapshots/x/private", "snapshots/x"):
            self.assertIsNone(store.snapshot_id("attempt", "a1", ref), ref)
        with self.assertRaises(research.ResearchError):
            store.snapshot("0" * 16)

    def test_hidden_evaluation_is_not_shown(self):
        write(self.root / "attempt-1.json", common("attempt", "a1", evaluation={"visibility": "hidden", "tp": 1},
                                                   submission={"findings": [{"type": "x"}]}))
        view = research.Projection(self.store()).attempt("a1")
        self.assertIsNone(view["evaluation"])
        self.assertTrue(view["evaluation_hidden"])

    def test_empty_root(self):
        view = research.Projection(research.ResearchStore([])).overview()
        self.assertEqual((view["experiments"], view["incompatible"]), ([], []))


@unittest.skipUnless(TestClient is not None and REAL, "needs FastAPI and the merged modules")
class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        research_fixtures.build(cls.tmp / "fx")
        cls.client = TestClient(create_app(DemoPaths(cls.tmp / "demo").ensure(), build_samples=False,
                                           research_roots=[cls.tmp / "fx"]))

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("auditlab.research.controller", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_overview_failure_record_and_errors(self):
        overview = self.client.get("/research/overview").json()
        self.assertEqual(len(overview["experiments"]), 3)
        self.assertEqual(overview["experiments"][0]["artifact_id"], "exp-memory-notes", "the experiment with a weakness first")
        failure = self.client.get("/research/failures/fail-boundary-residual").json()
        record = self.client.get(f"/research/snapshots/{failure['snapshot_id']}/records/ledger/RB74667B32C20460A9F0BCE8C")
        self.assertEqual(record.json()["line"], 76)
        self.assertEqual(self.client.get("/research/failures/nope").status_code, 404)
        self.assertEqual(self.client.get(f"/research/snapshots/{failure['snapshot_id']}/records/private/truth").status_code, 404)
        self.assertEqual(self.client.get("/research/snapshots/deadbeefdeadbeef/records/ledger/R1").status_code, 404)

    def test_start_without_controller_is_refused(self):
        sys.modules.pop("auditlab.research.controller", None)
        reply = self.client.post("/research/experiments/exp-memory-notes/start")
        self.assertEqual((reply.status_code, reply.json()["detail"]["code"]), (503, "controller_not_connected"))
        self.assertEqual(self.client.post("/research/experiments/nope/start").status_code, 404)
        self.assertFalse(self.client.get("/research/status").json()["controller"]["connected"])

    def test_controller_budget_access_and_secret_boundary(self):
        fake = types.ModuleType("auditlab.research.controller")
        state = {"budget_configured": False, "api_key_configured": True, "api_key": "sk-never"}
        fake.status = lambda: dict(state)
        fake.start = lambda experiment_id: {"ok": True, "experiment_id": experiment_id, "api_key": "sk-never"}
        fake.stop = lambda experiment_id: {"ok": True}
        sys.modules["auditlab.research.controller"] = fake
        try:
            status = self.client.get("/research/status").json()["controller"]
            self.assertNotIn("sk-never", json.dumps(status))
            reply = self.client.post("/research/experiments/exp-memory-notes/start")
            self.assertEqual(reply.json()["detail"]["code"], "budget_not_configured")
            state["budget_configured"] = True
            state["api_key_configured"] = False
            self.assertEqual(self.client.post("/research/experiments/exp-memory-notes/start").json()["detail"]["code"],
                             "api_key_missing")
            state["api_key_configured"] = True
            reply = self.client.post("/research/experiments/exp-memory-notes/start")
            self.assertEqual(reply.status_code, 200)
            self.assertNotIn("sk-never", reply.text)
        finally:
            sys.modules.pop("auditlab.research.controller", None)


if __name__ == "__main__":
    unittest.main()
