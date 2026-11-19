"""Judge console: evidence lines, fact changes, scoring, isolation, API = CLI.

Run: python -m unittest discover -s tests/demo -v
API tests are skipped when FastAPI/httpx are not installed (CLI tests still run).
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from auditlab.demo import derive, engines, lab, service, standin
from auditlab.demo.paths import PACKAGE, DemoPaths
from auditlab.demo.snapshot import Snapshot
from auditlab.demo.store import ExportStore

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "fixtures" / "foundation"
FX01 = PACKAGE / "scenarios" / "FX01"
STANDIN_ONLY = engines.session(FIX / "S01" / "public")[1] == engines.STANDIN

try:
    from fastapi.testclient import TestClient
    from auditlab.demo.server import create_app
except ImportError:  # pragma: no cover
    TestClient = None


def tree_hash(path: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in path.rglob("*") if p.is_file()):
        h.update(str(f.relative_to(path)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()


class Workspace(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.paths = DemoPaths(self.tmp / "demo").ensure()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def store(self):
        return ExportStore([self.paths.exports], self.paths.ops)


class EvidenceLines(unittest.TestCase):
    def test_keys_resolve_to_exact_file_lines(self):
        snap = Snapshot(FIX / "S02" / "public")
        r5 = snap.get("ledger/R5")
        self.assertEqual((r5.file, r5.line), ("public/ledger.csv", 6))
        self.assertEqual(r5.raw, (FIX / "S02" / "public" / "ledger.csv").read_text().splitlines()[5])
        self.assertEqual(r5.fields["debit_minor"], 3000000)
        self.assertEqual(snap.get("payments/D9").line, 1)
        self.assertEqual(snap.resolve_affected("payment/P9").key, "payments/D9")
        self.assertIsNotNone(snap.get("policy/allocation"))
        self.assertIsNone(snap.get("ledger/R99"))
        self.assertEqual(Snapshot(FX01 / "public").get("fx/2026-11-15/USD/KZT").line, 4)

    def test_allocation_derivation_matches_foundation_amounts(self):
        for ep, expected in (("S01", 0), ("S02", 2_000_000)):
            snap = Snapshot(FIX / ep / "public")
            calc = derive.allocation(snap, snap.get("payments/D9"))
            self.assertEqual(calc["result_minor"], expected)
            self.assertTrue(calc["index_check"]["complete"])


class FactChange(Workspace):
    def test_moving_settlement_after_boundary_reproduces_s02(self):
        out = self.tmp / "new"
        built = standin.apply_change(FIX / "S01" / "public", {"target": "ledger/R7", "field": "timestamp",
                                     "value": "2026-12-01T00:01:00+05:00"}, out, "S02")
        for name in ("ledger.csv", "payments.jsonl", "invoices.jsonl", "fx.csv", "policy.json"):
            self.assertEqual((out / name).read_text(), (FIX / "S02" / "public" / name).read_text(), name)
        self.assertEqual(sorted(built["moved"]), ["ledger/R7", "ledger/R8"])
        truth = json.loads((FIX / "S02" / "private" / "truth.json").read_text())
        got = built["truth"]["issues"]
        self.assertEqual(len(got), 1)
        for field in ("type", "affected_ids", "expected_amount_minor", "currency"):
            self.assertEqual(got[0][field], truth["issues"][0][field])
        self.assertEqual(sorted(got[0]["evidence_sets"][0]), sorted(truth["issues"][0]["evidence_sets"][0]))

    def test_fx_payment_shift_gives_1000_tenge(self):
        built = standin.apply_change(FX01 / "public", {"target": "payments/D9", "field": "timestamp",
                                     "value": "2026-11-15T00:01:00+05:00"}, self.tmp / "fx", "FX02")
        [issue] = built["truth"]["issues"]
        self.assertEqual((issue["type"], issue["expected_amount_minor"], issue["currency"]), ("fx_conversion", 100000, "KZT"))
        self.assertIn("fx/2026-11-15/USD/KZT", issue["evidence_sets"][0])
        self.assertEqual(Snapshot(self.tmp / "fx").get("ledger/R3").fields["debit_minor"], 5000000)

    def test_rejected_changes_have_codes(self):
        cases = [({"target": "payments/D9", "field": "timestamp", "value": "2026-11-20T10:00:00+05:00"}, "no_rate_for_date"),
                 ({"target": "payments/D9", "field": "amount_minor", "value": "1"}, "unsupported_field"),
                 ({"target": "ledger/R404", "field": "timestamp", "value": "2026-11-15T00:01:00+05:00"}, "unknown_target"),
                 ({"target": "payments/D9", "field": "timestamp", "value": "15 Nov"}, "invalid_value")]
        for change, code in cases:
            with self.subTest(code=code), self.assertRaises(standin.ChangeRejected) as ctx:
                standin.apply_change(FX01 / "public", change, self.tmp / code, "FXX")
            self.assertEqual(ctx.exception.code, code)


class ScoreTable(unittest.TestCase):
    """Control rows of SPEC §9 against the stand-in evaluator."""

    issue = {"issue_id": "X1", "type": "payment_allocation", "affected_ids": ["invoice/I4", "payment/P9"],
             "expected_amount_minor": 2000000, "currency": "KZT",
             "evidence_sets": [["payments/D9", "ledger/R4", "ledger/R5", "policy/allocation"]]}
    good = {"type": "payment_allocation", "affected_ids": ["payment/P9", "invoice/I4"], "claimed_amount_minor": 2000000,
            "currency": "KZT", "evidence_ids": ["payments/D9", "ledger/R4", "ledger/R5", "policy/allocation"], "explanation": "x"}

    def run_case(self, findings, issues, steps):
        return standin.score({"episode_id": "E", "findings": findings}, {"episode_id": "E", "issues": issues}, steps)

    def test_rows(self):
        partial = {**self.good, "evidence_ids": ["payments/D9", "ledger/R4", "policy/allocation"]}
        rows = [([], [], 0, (0, 0, 0), 1.0), ([self.good], [], 3, (0, 1, 0), -1.0), ([], [self.issue], 4, (0, 0, 1), -1.0),
                ([self.good], [self.issue], 4, (1, 0, 0), 0.992), ([self.good, self.good], [self.issue], 4, (1, 1, 0), 0.325333),
                ([partial], [self.issue], 4, (0, 1, 1), -1.0)]
        for findings, issues, steps, counts, reward in rows:
            with self.subTest(counts=counts, steps=steps):
                s = self.run_case(findings, issues, steps)
                self.assertEqual((s["tp"], s["fp"], s["fn"]), counts)
                self.assertAlmostEqual(s["reward"], reward, places=5)
        verdict = self.run_case([partial], [self.issue], 4)["per_finding"][0]
        self.assertEqual(verdict["reason"], "evidence_incomplete")


class LabEndToEnd(Workspace):
    def test_samples_live_change_rescore_replay(self):
        built = lab.build_samples(self.paths)
        self.assertIn("TRAIN-S02-reconcile", built)
        store = self.store()
        s02 = service.run_detail(store, "TRAIN-S02-reconcile")
        self.assertEqual(s02["run"]["label"], "training_sample")
        self.assertEqual(s02["findings"][0]["finding"]["claimed_amount_minor"], 2000000)
        self.assertTrue(s02["findings"][0]["verdict"]["matched"])
        self.assertTrue(service.run_detail(store, "TRAIN-S01-reconcile")["empty_submission"])
        aborted = service.run_detail(store, "TRAIN-S02-aborted")
        self.assertIsNone(aborted["submission"])
        self.assertEqual(aborted["run"]["status"], "aborted")

        parent = self.paths.exports / "TRAIN-FX01-reconcile"
        before = tree_hash(parent)
        change = {"target": "payments/D9", "field": "timestamp", "value": "2026-11-15T00:01:00+05:00"}
        op = lab.live_change(self.paths, "TRAIN-FX01-reconcile", change, "1 000 тенге", "op-test-fx-1")
        self.assertEqual(op["status"], "completed")
        again = lab.live_change(self.paths, "TRAIN-FX01-reconcile", change, None, "op-test-fx-1")
        self.assertEqual(again["run_id"], op["run_id"], "same operation key returns the same experiment")
        self.assertEqual(tree_hash(parent), before, "original attempt is preserved")
        live = service.run_detail(store, op["run_id"])
        self.assertNotEqual(live["run"]["episode_id"], "FX01")
        self.assertEqual(live["findings"][0]["finding"]["claimed_amount_minor"], 100000)
        cmp = service.compare(store, "TRAIN-FX01-reconcile", op["run_id"])
        self.assertEqual(cmp["calculations"][0]["difference_minor"], 100000)
        self.assertTrue(cmp["left"]["empty"])

        with self.assertRaises(lab.LabError) as ctx:
            lab.live_change(self.paths, "TRAIN-FX01-reconcile", {**change, "value": "2026-11-21T00:01:00+05:00"}, None, "op-test-fx-bad")
        self.assertEqual(ctx.exception.code, "no_rate_for_date")
        self.assertEqual(store.op_status("op-test-fx-bad")["status"], "failed")

        copy = lab.rescore(self.paths, op["run_id"], 0, "fx/2026-11-15/USD/KZT", "fx/2026-11-14/USD/KZT", "op-test-copy-1")
        copied = service.run_detail(store, copy["run_id"])
        self.assertEqual(copied["score"]["tp"], 0)
        self.assertEqual(copied["findings"][0]["verdict"]["reason"], "evidence_incomplete")
        self.assertIn("fx/2026-11-15/USD/KZT", copied["findings"][0]["uncited_operands"])
        self.assertEqual(service.run_detail(store, op["run_id"])["score"]["tp"], 1, "source score unchanged")

        check = lab.replay_check(self.paths, "TRAIN-S02-reconcile")
        self.assertTrue(check["observations_match"])
        self.assertTrue(check["score_match"])


class Isolation(Workspace):
    CANARY = "LEAK-CANARY-91c4e2"

    def test_web_side_never_returns_truth_or_closed_runs(self):
        lab.build_samples(self.paths)
        # Plant canaries in every place the web must not read.
        for truth in self.paths.trusted.rglob("truth.json"):
            data = json.loads(truth.read_text())
            data["canary"] = self.CANARY
            truth.write_text(json.dumps(data))
        bad = self.paths.exports / "BAD-private"
        shutil.copytree(self.paths.exports / "TRAIN-S02-reconcile", bad)
        meta = json.loads((bad / "run.json").read_text())
        (bad / "run.json").write_text(json.dumps({**meta, "run_id": "BAD-private"}))
        (bad / "private").mkdir()
        (bad / "private" / "truth.json").write_text(json.dumps({"canary": self.CANARY}))
        closed = self.paths.exports / "CLOSED-1"
        shutil.copytree(self.paths.exports / "TRAIN-S01-reconcile", closed)
        (closed / "run.json").write_text(json.dumps({**meta, "run_id": "CLOSED-1", "mode": "closed", "note": self.CANARY}))

        store = self.store()
        ids = [r["run_id"] for r in service.list_runs(store)]
        self.assertNotIn("BAD-private", ids)
        self.assertNotIn("CLOSED-1", ids)
        dumps = [json.dumps(service.list_runs(store), ensure_ascii=False)]
        for rid in ids:
            detail = service.run_detail(store, rid)
            dumps.append(json.dumps(detail, ensure_ascii=False))
            for key in detail["keys"]:
                dumps.append(json.dumps(service.record_view(store, rid, key), ensure_ascii=False))
        blob = "\n".join(dumps)
        self.assertNotIn(self.CANARY, blob)
        self.assertNotIn("evidence_sets", blob)
        self.assertNotIn("expected_amount_minor", blob)

    def test_web_modules_do_not_touch_truth(self):
        for name in ("server.py", "service.py", "store.py", "snapshot.py"):
            text = (PACKAGE / name).read_text()
            for token in ("truth.json", ".trusted", "_load_truth", "evidence_sets", "expected_amount_minor"):
                self.assertNotIn(token, text, f"{name} must not reach training truth ({token})")
            self.assertNotIn("import lab", text, name)
            self.assertNotIn("from .lab", text, name)

    def test_static_bundle_is_offline(self):
        index = PACKAGE / "static" / "index.html"
        self.assertTrue(index.exists(), "frontend must be built into auditlab/demo/static")
        html = index.read_text()
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        for css in (PACKAGE / "static" / "assets").glob("*.css"):
            text = css.read_text()
            self.assertNotIn("@import", text)
            self.assertNotIn("url(http", text)


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class Api(Workspace):
    def client(self, samples=True):
        return TestClient(create_app(self.paths, build_samples=samples))

    def test_empty_state(self):
        client = self.client(samples=False)
        self.assertEqual(client.get("/demo/runs").json(), {"runs": []})
        self.assertEqual(client.get("/demo/runs/TRAIN-S02-reconcile").status_code, 404)
        self.assertEqual(client.get("/demo/runs/..%2F..%2Fetc").status_code, 404)
        self.assertEqual(client.get("/").status_code, 200)

    def test_api_equals_cli_and_pair_flow(self):
        client = self.client()
        api_detail = client.get("/demo/runs/TRAIN-S02-reconcile").json()
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        out = subprocess.run([sys.executable, "-m", "auditlab.demo", "--root", str(self.paths.root), "show",
                              "--run", "TRAIN-S02-reconcile", "--json"], capture_output=True, text=True, env=env, cwd=ROOT, check=True)
        self.assertEqual(api_detail, json.loads(out.stdout), "console shows exactly what the CLI prints")
        rec = client.get("/demo/runs/TRAIN-S02-reconcile/records/ledger/R5").json()
        self.assertEqual((rec["line"], rec["fields"]["debit_minor"]), (6, 3000000))
        self.assertEqual(client.get("/demo/runs/TRAIN-S02-reconcile/records/private/truth").status_code, 404)

        body = {"source_run_id": "TRAIN-S01-reconcile", "operation_key": "api-pair-0001",
                "change": {"target": "ledger/R7", "field": "timestamp", "value": "2026-12-01T00:01:00+05:00"}}
        self.assertIn(client.post("/demo/pairs", json=body).status_code, (200, 202))
        status = None
        for _ in range(200):
            status = client.get("/demo/ops/api-pair-0001").json()
            if status.get("status") in ("completed", "failed"):
                break
            import time
            time.sleep(0.1)
        self.assertEqual(status["status"], "completed", status)
        again = client.post("/demo/pairs", json=body)
        self.assertEqual((again.status_code, again.json()["run_id"]), (200, status["run_id"]))
        cmp = client.get(f"/demo/compare?left=TRAIN-S01-reconcile&right={status['run_id']}").json()
        self.assertEqual(cmp["calculations"][0]["difference_minor"], 2000000)
        self.assertEqual(cmp["right"]["claims"][0]["amount_minor"], 2000000)
        replay = client.post("/demo/runs/TRAIN-S02-reconcile/replay").json()
        self.assertTrue(replay["observations_match"] and replay["score_match"])


@unittest.skipUnless(STANDIN_ONLY, "golden file describes the stand-in engines")
class GoldenSample(Workspace):
    def test_committed_sample_export_is_reproduced(self):
        lab.build_samples(self.paths)
        golden = ROOT / "docs" / "demo" / "sample_export" / "TRAIN-S02-reconcile"
        made = self.paths.exports / "TRAIN-S02-reconcile"
        files = sorted(p.relative_to(golden) for p in golden.rglob("*") if p.is_file())
        self.assertEqual(files, sorted(p.relative_to(made) for p in made.rglob("*") if p.is_file()))
        for rel in files:
            self.assertEqual((golden / rel).read_bytes(), (made / rel).read_bytes(), str(rel))


if __name__ == "__main__":
    unittest.main()
