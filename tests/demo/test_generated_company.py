"""Generated companies of the open training set in the console: real generator fact change, cross-check.

Run: python -m unittest discover -s tests/demo -v
"""
import csv
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from auditlab.demo import crosscheck, engines, lab, service
from auditlab.demo.__main__ import main as demo_cli
from auditlab.demo.paths import DemoPaths
from auditlab.demo.store import ExportStore

ROOT = Path(__file__).resolve().parents[2]
REAL = engines.runner()[0] is not None
LAST_SETTLEMENT = "ledger/R1356C5BA3E13DFDB078B6E60"  # 30.11 23:59 in the "before" company of the pair


def tree_hash(path: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in path.rglob("*") if p.is_file()):
        h.update(str(f.relative_to(path)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()


@unittest.skipUnless(REAL, "needs the merged runner, policies, evaluator and generator")
class GeneratedCompany(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.paths = DemoPaths(cls.tmp / "demo").ensure()
        lab.build_samples(cls.paths)
        cls.store = ExportStore([cls.paths.exports], cls.paths.ops)
        runs = service.list_runs(cls.store)
        cls.ids = {r["episode_id"]: r["run_id"] for r in runs}
        cls.pair = {r["pair_role"]: r for r in runs if r.get("pair_id") == "train-boundary"}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_cli_list_names_the_company_and_pair_role(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(demo_cli(["--root", str(self.paths.root), "list"]), 0)
        lines = {line.split()[0]: line for line in out.getvalue().splitlines() if line.strip()}
        self.assertIn("пара train-boundary A", lines[self.pair["A"]["run_id"]])
        self.assertIn("пара train-boundary B", lines[self.pair["B"]["run_id"]])
        self.assertIn("Сгенерированная чистая компания", lines[self.ids["E04F4ED6D6765D816DF94C62477B6EC4C"]])

    def test_open_pair_and_clean_company_are_real_runs(self):
        a, b = self.pair["A"], self.pair["B"]
        self.assertEqual((a["episode_id"], b["episode_id"]),
                         ("E6476455CA5F6F7CF4A34F78E68AD54C0", "E651B6A63DDA2448A91DC825714D7732E"))
        for run in (a, b):
            self.assertEqual(run["engine"]["runner"], "auditlab.runner")
            self.assertTrue(run["engine"]["generator"].startswith("auditlab.generator"))
            self.assertEqual(run["source"], f"datasets/open/train/{run['episode_id']}")
        types = lambda run_id: sorted(f["finding"]["type"] for f in service.run_detail(self.store, run_id)["findings"])
        self.assertEqual(types(a["run_id"]), ["duplicate_invoice"])
        self.assertEqual(types(b["run_id"]), ["duplicate_invoice", "payment_allocation"])
        clean = service.run_detail(self.store, self.ids["E04F4ED6D6765D816DF94C62477B6EC4C"])
        self.assertTrue(clean["empty_submission"])
        self.assertEqual(clean["verifier"]["findings"], [])
        self.assertEqual((clean["score"]["tp"], clean["score"]["fp"], clean["score"]["fn"]), (0, 0, 0))

    def test_generator_input_stays_on_the_trusted_side(self):
        for run in self.pair.values():
            published = json.dumps(service.run_detail(self.store, run["run_id"]), ensure_ascii=False)
            self.assertNotIn("seed", published)
            self.assertNotIn("snapshot_nonce", published)
            console = (self.paths.exports / run["run_id"] / "console.json").read_text(encoding="utf-8")
            self.assertNotIn("seed", console)

    def test_fact_change_goes_through_the_real_generator(self):
        source = self.pair["A"]["run_id"]
        before = tree_hash(self.paths.exports / source)
        change = {"target": LAST_SETTLEMENT, "field": "timestamp", "value": "2026-12-01T00:01:00+05:00"}
        op = lab.live_change(self.paths, source, change, "появится остаток 20 000 ₸", "op-gen-after-1")
        self.assertEqual(op["status"], "completed")
        self.assertEqual(tree_hash(self.paths.exports / source), before, "the original attempt is preserved")
        live = service.run_detail(self.store, op["run_id"])
        self.assertTrue(live["run"]["engine"]["generator"].startswith("auditlab.generator"))
        self.assertEqual(live["run"]["change"]["timing"], {"from": "before", "to": "after"})
        claims = sorted((f["finding"]["type"], f["finding"]["claimed_amount_minor"], f["finding"]["currency"])
                        for f in live["findings"])
        self.assertEqual(claims, [("duplicate_invoice", 1200000, "KZT"), ("payment_allocation", 2000000, "KZT")])
        self.assertEqual((live["score"]["tp"], live["score"]["fp"], live["score"]["fn"]), (2, 0, 0))
        cmp = service.compare(self.store, source, op["run_id"])
        self.assertEqual(sorted(c["key"] for c in cmp["changes"] if c["field"] == "timestamp"),
                         sorted(live["run"]["change"]["moved"]))
        self.assertEqual(len(live["run"]["change"]["moved"]), 2, "one settlement journal, two rows")
        allocation = [c for c in cmp["calculations"] if c["type"] == "payment_allocation"]
        self.assertEqual([(c["left"]["result_minor"], c["right"]["result_minor"]) for c in allocation], [(0, 2000000)])
        # Every value on screen equals the raw export files of the new run, and a live replay agrees.
        report = crosscheck.check_run(self.store, op["run_id"])
        self.assertEqual(report["problems"], [])
        replay = crosscheck.live_check(self.paths, op["run_id"], [])
        self.assertTrue(replay["observations_match"])
        self.assertTrue(replay["score_match"])
        self.assertIn("executed_at", replay)

    def test_only_generator_values_are_accepted(self):
        source = self.pair["A"]["run_id"]
        with self.assertRaises(lab.LabError) as ctx:
            lab.live_change(self.paths, source, {"target": LAST_SETTLEMENT, "field": "timestamp",
                                                 "value": "2026-12-03T00:00:00+05:00"}, None, "op-gen-bad-1")
        self.assertEqual(ctx.exception.code, "generator_unsupported")
        self.assertIn("2026-12-01T00:01:00+05:00", str(ctx.exception))
        with self.assertRaises(lab.LabError) as ctx:
            lab.live_change(self.paths, source, {"target": "ledger/R765F277C72A53DF193243B58", "field": "timestamp",
                                                 "value": "2026-12-01T00:01:00+05:00"}, None, "op-gen-bad-2")
        self.assertEqual(ctx.exception.code, "generator_unsupported")
        self.assertIn(LAST_SETTLEMENT, str(ctx.exception))
        self.assertEqual(self.store.op_status("op-gen-bad-2")["status"], "failed")

    def test_crosscheck_all_runs_and_detects_a_broken_export(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = crosscheck.main(self.paths, self.store, [], [], False, False)
        self.assertEqual(code, 0, out.getvalue())
        broken = self.tmp / "broken"
        target = broken / "BROKEN-1"
        shutil.copytree(self.paths.exports / self.pair["B"]["run_id"], target)
        run = json.loads((target / "run.json").read_text(encoding="utf-8"))
        (target / "run.json").write_text(json.dumps({**run, "run_id": "BROKEN-1"}), encoding="utf-8")
        lines = (target / "trajectories.jsonl").read_text(encoding="utf-8").splitlines()
        last = json.loads(lines[-1])
        last["reward"] = 0.5
        lines[-1] = json.dumps(last)
        (target / "trajectories.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        report = crosscheck.check_run(ExportStore([broken]), "BROKEN-1")
        self.assertTrue(any("награда" in p for p in report["problems"]), report["problems"])

    def _damaged_copy(self, name):
        root = self.tmp / name
        target = root / name
        shutil.copytree(self.paths.exports / self.pair["B"]["run_id"], target)
        run = json.loads((target / "run.json").read_text())
        (target / "run.json").write_text(json.dumps({**run, "run_id": name}))
        return ExportStore([root]), target

    def test_empty_check_and_empty_rehearsal_are_not_checked(self):
        empty = DemoPaths(self.tmp / "empty-check").ensure()
        for args in (["crosscheck", "--json"],
                     ["crosscheck", "--rehearsal", str(self.tmp / "no-attempts"), "--json"]):
            with redirect_stdout(io.StringIO()) as output:
                code = demo_cli(["--root", str(empty.root), *args])
            report = json.loads(output.getvalue())
            self.assertEqual(code, 2)
            self.assertFalse(report["ok"])
            self.assertEqual(report["status"], "not_checked")

    def test_missing_completed_export_is_not_silently_filtered(self):
        store, target = self._damaged_copy("MISSING-EXPORT")
        (target / "submission.json").unlink()
        self.assertEqual(store.list_runs(), [])  # UI correctly refuses it.
        with redirect_stdout(io.StringIO()) as output:
            code = crosscheck.main(self.paths, store, [], [], False, True)
        report = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertEqual(report["runs"][0]["run_id"], "MISSING-EXPORT")
        self.assertTrue(report["runs"][0]["problems"])

    def test_empty_trajectory_and_null_submission_do_not_pass(self):
        for name, filename, content in (("EMPTY-JOURNAL", "trajectories.jsonl", ""),
                                        ("NULL-SUBMIT", "submission.json", "null")):
            store, target = self._damaged_copy(name)
            (target / filename).write_text(content)
            self.assertTrue(crosscheck.check_run(store, name)["problems"])

    def test_live_check_requires_actual_rescoring(self):
        # Public exports alone allow observation replay, but cannot certify re-scoring.
        empty = DemoPaths(self.tmp / "no-trusted-truth").ensure()
        source = self.pair["B"]["run_id"]
        with redirect_stdout(io.StringIO()) as output:
            code = crosscheck.main(empty, self.store, [self.paths.exports], [source], True, True)
        report = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertIsNone(report["runs"][0]["live"]["score_match"])

    def test_rehearsal_missing_evidence_is_not_checked(self):
        root = self.tmp / "incomplete-rehearsal"
        (root / "attempts" / "reconcile" / "example").mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "обязательные файлы"):
            crosscheck.rehearsal_parity(self.paths, root)

    def test_generated_pair_changes_only_the_settlement_timestamp(self):
        a = self.paths.exports / self.pair["A"]["run_id"] / "public"
        b = self.paths.exports / self.pair["B"]["run_id"] / "public"
        for name in ("invoices.jsonl", "fx.csv", "policy.json"):
            self.assertEqual((a / name).read_bytes(), (b / name).read_bytes(), name)
        # The settlement index is a derived list of rows before the reporting boundary.
        # Source payment facts remain identical; the moved debit leaves that index.
        def payments(path):
            return [json.loads(line) for line in (path / "payments.jsonl").read_text().splitlines()]
        left_payments, right_payments = payments(a), payments(b)
        self.assertEqual([{k: v for k, v in p.items() if k != "settlement_index"} for p in left_payments],
                         [{k: v for k, v in p.items() if k != "settlement_index"} for p in right_payments])
        def rows(path):
            with (path / "ledger.csv").open() as stream:
                return {r["row_id"]: r for r in csv.DictReader(stream)}
        left, right = rows(a), rows(b)
        self.assertEqual(left.keys(), right.keys())
        changed = []
        for row_id in left:
            differences = {key for key in left[row_id] if left[row_id][key] != right[row_id][key]}
            if differences:
                self.assertEqual(differences, {"timestamp"})
                changed.append(row_id)
        self.assertEqual(len(changed), 2)
        self.assertIn(LAST_SETTLEMENT.split("/", 1)[1], changed)


if __name__ == "__main__":
    unittest.main()
