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


def sample_ids(built):
    """Actual run_ids of the labelled samples, in SAMPLES order (the real runner assigns its own ids)."""
    return dict(zip(("S01", "S02", "FX01", "OPEN_A", "OPEN_B", "OPEN_CLEAN"), built))


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
        self.assertEqual(Snapshot(FX01 / "public").get("fx/2026-11-15/USD/KZT").line, 3)
        self.assertEqual(Snapshot(FX01 / "public").get("ledger/R7").fields["account"], "FX_VALUATION")

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
                                     "value": "2026-12-01T00:01:00+05:00"}, out, "S02", source_truth=json.loads((FIX / "S01" / "private" / "truth.json").read_text()))
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
                                     "value": "2026-11-15T00:01:00+05:00"}, self.tmp / "fx", "FX02", source_truth=json.loads((FX01 / "private" / "truth.json").read_text()))
        [issue] = built["truth"]["issues"]
        self.assertEqual((issue["type"], issue["expected_amount_minor"], issue["currency"]), ("fx_conversion", 100000, "KZT"))
        self.assertEqual(sorted(issue["evidence_sets"][0]),
                         sorted(["payments/D9", "fx/2026-11-15/USD/KZT", "ledger/R7", "policy/fx_date"]))
        moved = Snapshot(self.tmp / "fx")
        self.assertEqual(moved.get("ledger/R7").fields["debit_minor"], 5000000, "recorded valuation is kept")
        self.assertEqual({moved.get(f"ledger/R{n}").fields["timestamp"] for n in range(3, 9)}, {"2026-11-15T00:01:00+05:00"})
        self.assertEqual(moved.get("payments/D9").fields["settlement_index"]["unapplied_row_ids"], ["R4", "R5"])

    def test_rejected_changes_have_codes(self):
        cases = [({"target": "payments/D9", "field": "timestamp", "value": "2026-11-20T10:00:00+05:00"}, "no_rate_for_date"),
                 ({"target": "payments/D9", "field": "amount_minor", "value": "1"}, "unsupported_field"),
                 ({"target": "ledger/R404", "field": "timestamp", "value": "2026-11-15T00:01:00+05:00"}, "unknown_target"),
                 ({"target": "payments/D9", "field": "timestamp", "value": "15 Nov"}, "invalid_value")]
        for change, code in cases:
            with self.subTest(code=code), self.assertRaises(standin.ChangeRejected) as ctx:
                standin.apply_change(FX01 / "public", change, self.tmp / code, "FXX", source_truth=json.loads((FX01 / "private" / "truth.json").read_text()))
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


class AggregateVerdict(unittest.TestCase):
    """score.json without per_finding: a per-finding verdict only where TP/FP/FN make it unambiguous."""

    def verdicts(self, n, tp):
        snap = Snapshot(FIX / "S02" / "public")
        finding = dict(ScoreTable.good)
        views = service.findings_view(snap, {"episode_id": "S02", "findings": [finding] * n},
                                      {"tp": tp, "fp": n - tp, "fn": 1 - tp if tp <= 1 else 0, "reward": 0}, None, [])
        return [v["verdict"] for v in views]

    def test_derivation_rules(self):
        self.assertEqual([v["matched"] for v in self.verdicts(1, 1)], [True])
        self.assertEqual({v["source"] for v in self.verdicts(1, 1)}, {"aggregate"})
        self.assertEqual([(v["matched"], v["reason"]) for v in self.verdicts(2, 0)], [(False, "not_published")] * 2)
        self.assertEqual(self.verdicts(2, 1), [None, None], "mixed TP: no invented per-finding verdict")


class LabEndToEnd(Workspace):
    def test_samples_live_change_rescore_replay(self):
        built = lab.build_samples(self.paths)
        ids = sample_ids(built)
        self.assertEqual(len(set(built)), 6)
        store = self.store()
        s02 = service.run_detail(store, ids["S02"])
        self.assertEqual(s02["run"]["label"], "training_sample")
        self.assertEqual(s02["findings"][0]["finding"]["claimed_amount_minor"], 2000000)
        self.assertTrue(s02["findings"][0]["verdict"]["matched"])
        self.assertEqual(s02["score"]["inspection_steps"], s02["run"]["inspection_steps"])
        self.assertIsInstance(s02["score"]["inspection_steps"], int)
        self.assertTrue(service.run_detail(store, ids["S01"])["empty_submission"])
        # An unfinished attempt stays on disk but is not published in the console.
        cut = self.paths.exports / "CUT-1"
        shutil.copytree(self.paths.exports / ids["S02"], cut)
        (cut / "run.json").write_text(json.dumps({"run_id": "CUT-1", "episode_id": "S02", "policy": "reconcile",
                                                  "mode": "train", "status": "truncated"}))
        self.assertNotIn("CUT-1", [r["run_id"] for r in service.list_runs(store)])
        with self.assertRaises(Exception):
            service.run_detail(store, "CUT-1")

        parent = self.paths.exports / ids["FX01"]
        before = tree_hash(parent)
        change = {"target": "payments/D9", "field": "timestamp", "value": "2026-11-15T00:01:00+05:00"}
        op = lab.live_change(self.paths, ids["FX01"], change, "1 000 тенге", "op-test-fx-1")
        self.assertEqual(op["status"], "completed")
        again = lab.live_change(self.paths, ids["FX01"], change, None, "op-test-fx-1")
        self.assertEqual(again["run_id"], op["run_id"], "same operation key returns the same experiment")
        self.assertEqual(tree_hash(parent), before, "original attempt is preserved")
        live = service.run_detail(store, op["run_id"])
        self.assertNotEqual(live["run"]["episode_id"], "FX01")
        self.assertNotEqual(op["run_id"], ids["FX01"])
        for ev in live["findings"][0]["evidence"]:
            self.assertTrue(ev["exists"] and (ev["opened_at_step"] or ev["listed_at_step"]), ev)
        self.assertEqual(live["findings"][0]["finding"]["claimed_amount_minor"], 100000)
        cmp = service.compare(store, ids["FX01"], op["run_id"])
        fx = [c for c in cmp["calculations"] if c["type"] == "fx_conversion"]
        self.assertEqual([c["difference_minor"] for c in fx], [100000])
        self.assertEqual(cmp["calculations"][0]["type"], "fx_conversion", "the changed amount is shown first")
        self.assertTrue(cmp["left"]["empty"])

        with self.assertRaises(lab.LabError) as ctx:
            lab.live_change(self.paths, ids["FX01"], {**change, "value": "2026-11-21T00:01:00+05:00"}, None, "op-test-fx-bad")
        self.assertEqual(ctx.exception.code, "no_rate_for_date")
        self.assertEqual(store.op_status("op-test-fx-bad")["status"], "failed")

        copy = lab.rescore(self.paths, op["run_id"], 0, "fx/2026-11-15/USD/KZT", "fx/2026-11-14/USD/KZT", "op-test-copy-1")
        copied = service.run_detail(store, copy["run_id"])
        self.assertEqual(copied["score"]["tp"], 0)
        self.assertFalse(copied["findings"][0]["verdict"]["matched"])
        if copied["findings"][0]["verdict"]["source"] == "per_finding":
            self.assertEqual(copied["findings"][0]["verdict"]["reason"], "evidence_incomplete")
        self.assertIn("fx/2026-11-15/USD/KZT", copied["findings"][0]["uncited_operands"])
        self.assertEqual(service.run_detail(store, op["run_id"])["score"]["tp"], 1, "source score unchanged")
        # The comparison names the no-longer-cited fact with its unit, never a bare integer.
        claim = service.compare(store, op["run_id"], copy["run_id"])["right"]["claims"][0]
        rate = next(u for u in claim["unconfirmed"] if u["key"] == "fx/2026-11-15/USD/KZT")
        self.assertEqual((rate["value"], rate.get("value_minor")), ("510", None), "a rate is not a money amount")
        detail = service.run_detail(store, op["run_id"])
        for operand in detail["findings"][0]["derivation"]["operands"]:
            if "value_minor" in operand:
                self.assertIn(operand["currency"], ("USD", "KZT"), operand)

        check = lab.replay_check(self.paths, ids["S02"])
        self.assertTrue(check["observations_match"])
        self.assertTrue(check["score_match"])


class Isolation(Workspace):
    CANARY = "LEAK-CANARY-91c4e2"

    def test_web_side_never_returns_truth_or_closed_runs(self):
        ids = sample_ids(lab.build_samples(self.paths))
        # Plant canaries in every place the web must not read.
        for truth in self.paths.trusted.rglob("truth.json"):
            data = json.loads(truth.read_text())
            data["canary"] = self.CANARY
            truth.write_text(json.dumps(data))
        bad = self.paths.exports / "BAD-private"
        shutil.copytree(self.paths.exports / ids["S02"], bad)
        meta = json.loads((bad / "run.json").read_text())
        (bad / "run.json").write_text(json.dumps({**meta, "run_id": "BAD-private"}))
        (bad / "console.json").unlink()
        (bad / "private").mkdir()
        (bad / "private" / "truth.json").write_text(json.dumps({"canary": self.CANARY}))
        closed = self.paths.exports / "CLOSED-1"
        shutil.copytree(self.paths.exports / ids["S01"], closed)
        (closed / "run.json").write_text(json.dumps({**meta, "run_id": "CLOSED-1", "mode": "closed", "note": self.CANARY}))
        (closed / "console.json").unlink()

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
        self.assertEqual(client.get("/demo/runs/no-such-run").status_code, 404)
        self.assertEqual(client.get("/demo/runs/..%2F..%2Fetc").status_code, 404)
        self.assertEqual(client.get("/").status_code, 200)

    def test_api_equals_cli_and_pair_flow(self):
        client = self.client()
        listed = client.get("/demo/runs").json()["runs"]
        ids = {r["episode_id"]: r["run_id"] for r in listed if r.get("label") == "training_sample"}
        self.assertEqual(set(ids), {"S01", "S02", "FX01", *(e for e, *_ in lab.OPEN_SAMPLES)})
        s02 = ids["S02"]
        api_detail = client.get(f"/demo/runs/{s02}").json()
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        out = subprocess.run([sys.executable, "-m", "auditlab.demo", "--root", str(self.paths.root), "show",
                              "--run", s02, "--json"], capture_output=True, text=True, env=env, cwd=ROOT, check=True)
        self.assertEqual(api_detail, json.loads(out.stdout), "console shows exactly what the CLI prints")
        rec = client.get(f"/demo/runs/{s02}/records/ledger/R5").json()
        self.assertEqual((rec["line"], rec["fields"]["debit_minor"]), (6, 3000000))
        self.assertEqual(client.get(f"/demo/runs/{s02}/records/private/truth").status_code, 404)

        body = {"source_run_id": ids["S01"], "operation_key": "api-pair-0001",
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
        cmp = client.get(f"/demo/compare?left={ids['S01']}&right={status['run_id']}").json()
        self.assertEqual(cmp["calculations"][0]["difference_minor"], 2000000)
        self.assertEqual(cmp["right"]["claims"][0]["amount_minor"], 2000000)
        replay = client.post(f"/demo/runs/{s02}/replay").json()
        self.assertTrue(replay["observations_match"] and replay["score_match"])


REAL_ENV = engines.session(FIX / "S01" / "public")[1] == "auditlab.environment"
REAL_POLICY = engines.policy("reconcile", 0)[1] == "auditlab.policies"


class PublicInterface(Workspace):
    """The stand-in must speak the agreed public interface, never the other way round."""

    ACTIONS = [
        {"action": "get_record", "args": {"table": "policy", "id": "allocation"}},
        {"action": "get_record", "args": {"table": "policy", "id": "fx_date"}},
        {"action": "get_record", "args": {"table": "ledger", "id": "R7"}},
        {"action": "get_record", "args": {"table": "fx", "id": "2026-11-15/USD/KZT"}},
        {"action": "get_record", "args": {"table": "ledger", "id": "R404"}},
        {"action": "get_document", "args": {"doc_id": "D9"}},
        {"action": "calculator", "args": {"expression": "10000/100*510*100-5000000"}},
        {"action": "list_records", "args": {"table": "policy", "cursor": None, "limit": 50}},
        {"action": "list_records", "args": {"table": "ledger", "cursor": None, "limit": 50}},
        {"action": "list_records", "args": {"table": "fx", "cursor": None, "limit": 50}},
    ]

    @unittest.skipUnless(REAL_ENV, "auditlab.environment is not merged in this checkout: parity cannot be measured")
    def test_standin_session_matches_public_environment(self):
        from auditlab.environment import open_session
        public = FX01 / "public"
        real, fake = open_session(public), standin.StandinSession(public)
        for action in self.ACTIONS:
            with self.subTest(action=action):
                a, b = real.inspect(action), fake.inspect(action)
                self.assertEqual(a["ok"], b["ok"])
                if not a["ok"]:
                    self.assertEqual(a["error"]["code"], "not_found")
                    continue
                if action["action"] == "list_records":
                    self.assertEqual(a["result"]["records"], b["result"]["records"])
                    self.assertEqual(set(a["result"]), set(b["result"]))
                else:
                    self.assertEqual(a["result"], b["result"])

    def test_fx01_shift_found_by_the_reconcile_in_use(self):
        """FX01 moved to 15.11 00:01: the reconcile in use (real when merged) claims exactly the truth."""
        built = standin.apply_change(FX01 / "public", {"target": "payments/D9", "field": "timestamp",
                                     "value": "2026-11-15T00:01:00+05:00"}, self.tmp / "fx", "FX02", source_truth=json.loads((FX01 / "private" / "truth.json").read_text()))
        session, _ = engines.session(self.tmp / "fx")
        policy, policy_engine = engines.policy("reconcile", 0)
        manifest = json.loads((self.tmp / "fx" / "manifest.json").read_text())
        result = standin.run_episode(session, policy, built["truth"], manifest)
        [finding] = result["submission"]["findings"]
        [issue] = built["truth"]["issues"]
        self.assertEqual((finding["type"], finding["claimed_amount_minor"], finding["currency"]), ("fx_conversion", 100000, "KZT"))
        self.assertEqual(sorted(finding["evidence_ids"]), sorted(issue["evidence_sets"][0]), policy_engine)
        self.assertEqual(result["score"]["tp"], 1)


class RealRunnerPath(Workspace):
    """The lab drives auditlab.runner exactly as docs/INTERFACES.md describes, once it exists."""

    def test_lab_uses_make_env_and_keeps_runner_export(self):
        import types
        calls = {}

        class FakeEnv:
            """Only the runner is faked; session, policy and evaluator are the agreed public modules."""

            def reset(self, seed, config):
                calls["config"] = config
                public = Path(config["snapshot_dir"]) / "public"
                self.truth = json.loads((Path(config["snapshot_dir"]) / "private" / "truth.json").read_text())
                self.session, calls["session"] = engines.session(public)
                self.score, calls["evaluator"] = engines.scorer()
                self.manifest = json.loads((public / "manifest.json").read_text())
                self.export, self.lines, self.left, self.inspections = Path(config["export_dir"]), [], 100, 0
                return {"manifest": self.manifest, "remaining_steps": 100}, {}

            def step(self, action):
                self.left -= 1
                if action["action"] == "submit":
                    score = self.score(action["args"], self.truth, self.inspections)
                    obs = {"ok": True, "result": {"accepted": True}, "remaining_steps": self.left}
                    self.lines.append({"episode_id": self.manifest["episode_id"], "step": 100 - self.left, "action": action,
                                       "observation": obs, "reward": score["reward"], "terminated": True, "truncated": False})
                    tmp = self.export.with_name(".runner-tmp")
                    shutil.copytree(Path(calls["config"]["snapshot_dir"]) / "public", tmp / "public")
                    (tmp / "trajectories.jsonl").write_text("".join(json.dumps(l) + "\n" for l in self.lines))
                    (tmp / "submission.json").write_text(json.dumps(action["args"]))
                    (tmp / "score.json").write_text(json.dumps(score))
                    (tmp / "run.json").write_text(json.dumps({"run_id": "runner-0001", "episode_id": self.manifest["episode_id"],
                                                              "policy": calls["config"]["policy_name"], "mode": "train", "status": "completed"}))
                    tmp.rename(self.export)
                    return obs, score["reward"], True, False, {}
                self.inspections += 1
                obs = {**self.session.inspect(action), "remaining_steps": self.left}
                self.lines.append({"episode_id": self.manifest["episode_id"], "step": 100 - self.left, "action": action,
                                   "observation": obs, "reward": 0, "terminated": False, "truncated": False})
                return obs, 0, False, False, {}

        fake = types.ModuleType("auditlab.runner")
        fake.make_env = lambda mode: FakeEnv() if mode == "train" else None
        sys.modules["auditlab.runner"] = fake
        try:
            run = lab.execute(self.paths, FIX / "S02" / "public", json.loads((FIX / "S02" / "private" / "truth.json").read_text()),
                              {"key": "sample:fake", "run_id": "ignored", "label": "training_sample"})
        finally:
            del sys.modules["auditlab.runner"]
        self.assertEqual(run["run_id"], "runner-0001")
        self.assertEqual(set(calls["config"]), {"snapshot_dir", "export_dir", "policy_name"})
        self.assertFalse(Path(calls["config"]["export_dir"]).exists(), "staging export moved into exports/")
        exported = self.paths.exports / "runner-0001"
        self.assertEqual(json.loads((exported / "run.json").read_text())["policy"], "reconcile")
        self.assertEqual(json.loads((exported / "console.json").read_text())["engine"]["runner"], "auditlab.runner")
        self.assertEqual(calls["session"], "auditlab.environment" if REAL_ENV else engines.STANDIN)
        detail = service.run_detail(self.store(), "runner-0001")
        self.assertEqual(detail["findings"][0]["finding"]["claimed_amount_minor"], 2000000)
        self.assertFalse((exported / "private").exists())


class GoldenSample(Workspace):
    def test_committed_sample_export_is_reproduced(self):
        """Snapshot and run.json core always match; trajectory bytes match when the stand-in engines produced them."""
        ids = sample_ids(lab.build_samples(self.paths))
        golden = ROOT / "docs" / "demo" / "sample_export" / "TRAIN-S02-reconcile"
        made = self.paths.exports / ids["S02"]
        for rel in sorted(p.relative_to(golden) for p in (golden / "public").rglob("*") if p.is_file()):
            self.assertEqual((golden / rel).read_bytes(), (made / rel).read_bytes(), str(rel))
        core = ("episode_id", "policy", "mode", "status")  # run_id is assigned by the runner in use
        self.assertEqual({k: json.loads((golden / "run.json").read_text())[k] for k in core},
                         {k: json.loads((made / "run.json").read_text())[k] for k in core})
        made_sub = json.loads((made / "submission.json").read_text())
        gold_sub = json.loads((golden / "submission.json").read_text())
        def norm(sub):
            return [{k: sorted(v) if k in ("affected_ids", "evidence_ids") else v
                     for k, v in f.items() if k != "explanation"} for f in sub["findings"]]
        self.assertEqual(norm(made_sub), norm(gold_sub), "same finding regardless of engine")
        if STANDIN_ONLY and engines.policy("reconcile", 0)[1] == engines.STANDIN:
            for name in ("trajectories.jsonl", "submission.json", "score.json", "console.json"):
                self.assertEqual((golden / name).read_bytes(), (made / name).read_bytes(), name)


if __name__ == "__main__":
    unittest.main()
