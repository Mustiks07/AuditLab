"""Independent verifier against hand calculations (SPEC §§4–6, 10; DAT-01/02/03/05)."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

import cases  # noqa: E402
from cases import F  # noqa: E402
from auditlab.verifier import verify_report, verify_snapshot  # noqa: E402
from auditlab.verifier.engine import Database  # noqa: E402

FIXTURES = ROOT / "fixtures" / "foundation"
HAVE_DUCKDB = importlib.util.find_spec("duckdb") is not None
ENGINES = ("duckdb", "sqlite") if HAVE_DUCKDB else ("sqlite",)


def keys(findings):
    return sorted((f["type"], tuple(sorted(f["affected_ids"])), f["claimed_amount_minor"], f["currency"])
                  for f in findings)


def tree_hash(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(path).rglob("*")) if p.is_file()}


class VerifierTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.n = 0

    def run_case(self, case, engine=None):
        self.n += 1
        public = case.write(self.tmp / f"case{self.n}")
        before = tree_hash(public)
        report = verify_report(public, engine)
        self.assertEqual(before, tree_hash(public), "verifier must not modify public/")
        for finding in report["findings"]:
            self.assertLessEqual(len(finding["evidence_ids"]), 10)
            self.assertLessEqual(len(finding["explanation"]), 1000)
        return report

    def assertFindings(self, case, expected, engines=ENGINES):
        for engine in engines:
            with self.subTest(case=case.episode_id, engine=engine):
                report = self.run_case(case, engine)
                self.assertEqual(keys(report["findings"]), sorted(expected), report["derivations"])
        return report


class ControlPairTests(VerifierTestCase):
    def test_pair_matches_committed_manual_answer(self):
        manual = json.loads((Path(__file__).parent / "manual_answers.json").read_text(encoding="utf-8"))
        for label in ("S01", "S02"):
            for engine in ENGINES:
                with self.subTest(label=label, engine=engine):
                    public = self.tmp / label / engine / "public"
                    shutil.copytree(FIXTURES / label / "public", public)
                    report = verify_report(public, engine)
                    expected = manual["episodes"][label]
                    if expected["unapplied_balance_minor"] == 0:
                        self.assertEqual(report["findings"], [])
                    else:
                        [finding] = report["findings"]
                        self.assertEqual(finding["type"], "payment_allocation")
                        self.assertEqual(finding["claimed_amount_minor"], expected["unapplied_balance_minor"])
                        self.assertEqual(finding["currency"], "KZT")
                        # Manual answer recorded payment/P9 and flagged invoice/I4 as ambiguous target.
                        self.assertEqual(finding["affected_ids"], ["invoice/I4", "payment/P9"])
                        self.assertEqual(finding["evidence_ids"],
                                         ["payments/D9", "policy/allocation", "ledger/R4", "ledger/R5"])
                    self.assertEqual(report["notes"], [])

    def test_committed_answer_files_are_current(self):
        for label in ("S01", "S02"):
            stored = json.loads((Path(__file__).parent / "answers" / f"{label}.report.json").read_text("utf-8"))
            self.assertEqual(stored["findings"], verify_snapshot(FIXTURES / label / "public"))


class BoundaryTests(VerifierTestCase):
    def test_allocation_boundary_local_and_utc(self):
        # 50 000 - 30 000 - 20 000 = 0 before the boundary; 50 000 - 30 000 = 20 000 at/after it.
        alloc = lambda amount: [F("payment_allocation", ["payment/P9", "invoice/I4"], amount, "KZT")]
        for instant, expected in (("2026-11-30T23:59:00+05:00", []),
                                  ("2026-11-30T18:59:59+00:00", []),
                                  ("2026-12-01T00:00:00+05:00", alloc(2_000_000)),
                                  ("2026-11-30T19:00:00+00:00", alloc(2_000_000)),
                                  ("2026-12-01T00:01:00+05:00", alloc(2_000_000)),
                                  ("2026-12-01T00:00:00+06:00", [])):  # = 30 Nov 23:00 company time
            with self.subTest(instant=instant):
                self.assertFindings(cases.allocation_case(instant), expected)

    def test_invoice_cutoff_boundary(self):
        cut = [F("period_cutoff", ["invoice/I4"], 1_000_000, "KZT")]
        for posted, expected in (("2026-11-30T15:00:00+05:00", []), ("2026-11-30T23:59:59+05:00", []),
                                 ("2026-11-30T18:59:59+00:00", []), ("2026-12-01T00:00:00+05:00", cut),
                                 ("2026-11-30T19:00:00+00:00", cut), ("2026-12-02T08:00:00+05:00", cut)):
            with self.subTest(posted=posted):
                report = self.assertFindings(cases.cutoff_invoice_case(posted), expected)
        self.assertEqual(report["findings"][0]["evidence_ids"],
                         ["invoices/D4", "policy/period", "ledger/R1"])

    def test_payment_cutoff_and_next_month_payment_is_clean(self):
        self.assertFindings(cases.cutoff_payment_case("2026-11-30T23:30:00+05:00", "2026-11-30T23:45:00+05:00"), [])
        self.assertFindings(cases.cutoff_payment_case("2026-12-01T00:10:00+05:00", "2026-12-01T00:20:00+05:00"),
                            [F("period_cutoff", ["payment/P9"], 700_000, "KZT")])


class SingleClassTests(VerifierTestCase):
    def test_payment_allocation_and_clean_tranches_and_refund(self):
        self.assertFindings(cases.split_payment_clean(), [])
        self.assertFindings(cases.refund_clean(), [])

    def test_allocation_instructions(self):
        # I4 instructed 50 000, applied 50 000; I5 instructed 30 000, applied 10 000 -> 20 000 on I5.
        self.assertFindings(cases.instructed_split(3_000_000), [])
        self.assertFindings(cases.instructed_split(1_000_000),
                            [F("payment_allocation", ["payment/P9", "invoice/I5"], 2_000_000, "KZT")])
        self.assertFindings(cases.instructed_split(0),
                            [F("payment_allocation", ["payment/P9", "invoice/I5"], 3_000_000, "KZT")])

    def test_fx_conversion(self):
        # 50.00 USD x 500 = 25 000.00 KZT = 2 500 000; recorded 2 550 000 -> 50 000 (500 KZT).
        report = self.assertFindings(cases.fx_payment_case(2_550_000),
                                     [F("fx_conversion", ["payment/P9"], 50_000, "KZT")])
        self.assertEqual(report["findings"][0]["evidence_ids"],
                         ["payments/D9", "fx/2026-11-15/USD/KZT", "policy/fx_date", "ledger/R9"])
        self.assertFindings(cases.fx_payment_case(2_500_000), [])

    def test_fx_rate_date_is_company_local_date(self):
        # 30 Nov 19:30 UTC = 1 Dec 00:30 company time -> rate of 2026-12-01 (510): 2 550 000 expected.
        case = cases.fx_payment_case(2_500_000, timestamp="2026-11-30T19:30:00+00:00")
        self.assertFindings(case, [F("fx_conversion", ["payment/P9"], 50_000, "KZT")])
        # 30 Nov 18:59:59 UTC = 30 Nov 23:59:59 -> rate of 2026-11-30 (500): clean.
        case = cases.fx_payment_case(2_500_000, timestamp="2026-11-30T18:59:59+00:00")
        case.rate("2026-11-30", "USD", "KZT", "500")
        self.assertFindings(case, [])

    def test_fx_missing_rate_is_ambiguity_not_finding(self):
        case = cases.fx_payment_case(2_500_000, timestamp="2026-11-20T12:00:00+05:00")
        report = self.assertFindings(case, [])
        self.assertIn("ambiguity", {n["kind"] for n in report["notes"]})

    def test_currency_exponents_and_rounding_point(self):
        # JPY exp 0: 1234 x 3.345 = 4127.73 KZT -> 412 773; recorded 412 700 -> 73.
        self.assertFindings(cases.fx_generic("JPY", 1234, "3.345", 412_700),
                            [F("fx_conversion", ["payment/P9"], 73, "KZT")])
        # KWD exp 3: 1.005 x 1628.125 = 1636.265625 KZT -> 163 626.5625 -> half up 163 627.
        self.assertFindings(cases.fx_generic("KWD", 1005, "1628.125", 163_627), [])
        self.assertFindings(cases.fx_generic("KWD", 1005, "1628.125", 163_626),
                            [F("fx_conversion", ["payment/P9"], 1, "KZT")])

    def test_period_cutoff_next_month_payment_alone_is_clean(self):
        self.assertFindings(cases.cutoff_invoice_case("2026-11-30T15:00:00+05:00"), [])

    def test_duplicate_invoice(self):
        # Two recognitions of 12 000 KZT for one supplier invoice -> 12 000 KZT extra.
        report = self.assertFindings(cases.duplicate_case(True),
                                     [F("duplicate_invoice", ["invoice/I4"], 1_200_000, "KZT")])
        self.assertEqual(report["findings"][0]["evidence_ids"],
                         ["invoices/D4", "invoices/D5", "ledger/R1", "ledger/R3"])
        self.assertFindings(cases.duplicate_case(True, "I5"),
                            [F("duplicate_invoice", ["invoice/I4"], 1_200_000, "KZT")])
        self.assertFindings(cases.duplicate_case(False), [])  # two copies, one recognition
        self.assertFindings(cases.equal_amount_different_suppliers(), [])


class InteractionTests(VerifierTestCase):
    def test_interaction_a_four_states(self):
        alloc = F("payment_allocation", ["payment/P9", "invoice/I4"], 2_000, "USD")   # 50 - 30 = 20 USD
        fx = F("fx_conversion", ["payment/P9"], 50_000, "KZT")                        # 50 x (510 - 500)
        for allocation_defect in (False, True):
            for fx_defect in (False, True):
                expected = [x for x, on in ((alloc, allocation_defect), (fx, fx_defect)) if on]
                self.assertFindings(cases.interaction_a(allocation_defect, fx_defect), expected)

    def test_interaction_b_four_states(self):
        cut = F("period_cutoff", ["invoice/I4"], 10_000, "USD")       # 100 USD nominal
        fx = F("fx_conversion", ["invoice/I4"], 100_000, "KZT")      # 100 x (510 - 500) = 1 000 KZT
        for cutoff_defect in (False, True):
            for fx_defect in (False, True):
                expected = [x for x, on in ((cut, cutoff_defect), (fx, fx_defect)) if on]
                self.assertFindings(cases.interaction_b(cutoff_defect, fx_defect), expected)


class ArithmeticPrimitiveTests(unittest.TestCase):
    HAND = [  # (src_minor, rate, src_exp, dst_exp) -> ROUND_HALF_UP(src / 10^se * rate * 10^de)
        ((5_000, "500", 2, 2), 2_500_000), ((5_000, "510", 2, 2), 2_550_000),
        ((1, "0.5", 2, 2), 1), ((1, "0.4999", 2, 2), 0), ((1005, "1", 3, 2), 101),  # float gives 100
        ((100, "2.5", 2, 0), 3), ((12_345, "1.5", 2, 0), 185), ((1234, "3.345", 0, 2), 412_773),
        ((1005, "1628.125", 3, 2), 163_627), ((0, "500", 2, 2), 0),
        ((999_999_999_999, "512.123456", 2, 3), 5_121_234_559_994_879),
    ]

    def test_hand_values_on_each_engine(self):
        for engine in ENGINES:
            database = Database(engine)
            self.addCleanup(database.close)
            for args, expected in self.HAND:
                with self.subTest(engine=engine, args=args):
                    [(value,)] = database.query(
                        f"SELECT convert_half_up({args[0]}, '{args[1]}', {args[2]}, {args[3]})")
                    self.assertEqual(value, expected)

    def test_sql_has_no_division_or_float(self):
        for path in (ROOT / "sql" / "verifier").glob("*.sql"):
            code = "\n".join(l.split("--")[0] for l in path.read_text(encoding="utf-8").splitlines())
            code = "".join(part for i, part in enumerate(code.split("'")) if i % 2 == 0)
            for token in ("/", "DOUBLE", "FLOAT", "REAL", "ROUND("):
                self.assertNotIn(token, code.upper(), f"{path.name} uses {token}")


@unittest.skipUnless(HAVE_DUCKDB, "не проверено: duckdb не установлен")
class EngineParityTests(VerifierTestCase):
    def test_duckdb_and_sqlite_agree_on_every_case(self):
        builders = [lambda: cases.allocation_case("2026-12-01T00:01:00+05:00"), cases.split_payment_clean,
                    cases.refund_clean, lambda: cases.instructed_split(1_000_000),
                    lambda: cases.fx_payment_case(2_550_000), lambda: cases.cutoff_invoice_case("2026-12-01T00:00:00+05:00"),
                    lambda: cases.duplicate_case(True, "I5"), lambda: cases.interaction_a(True, True),
                    lambda: cases.interaction_b(True, True), lambda: cases.fx_generic("KWD", 1005, "1628.125", 163_626)]
        for build in builders:
            case = build()
            with self.subTest(case=case.episode_id):
                a, b = self.run_case(case, "duckdb"), self.run_case(case, "sqlite")
                self.assertEqual(a["findings"], b["findings"])
                self.assertEqual(a["notes"], b["notes"])


class BoundaryOfTrustTests(unittest.TestCase):
    FORBIDDEN = ("auditlab.generator", "auditlab.evaluator", "auditlab.runner", "auditlab.policies",
                 "auditlab.verifier.crosscheck", "auditlab.scorer")

    def test_verifier_imports_only_public_schema(self):
        package = ROOT / "auditlab" / "verifier"
        for path in package.glob("*.py"):
            if path.name == "crosscheck.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [("." * node.level) + (node.module or "")] + \
                            [("." * node.level) + a.name for a in node.names]
                for name in names:
                    absolute = name.replace("..", "auditlab.").replace(".", "auditlab.verifier.", 1) \
                        if name.startswith(".") else name
                    self.assertFalse(any(absolute.startswith(f) or name.lstrip(".") == "crosscheck"
                                         for f in self.FORBIDDEN), f"{path.name} imports {name}")

    def test_runs_on_public_copy_without_private_and_reads_only_public(self):
        code = (
            "import sys, json\n"
            "opened = []\n"
            "sys.addaudithook(lambda e, a: opened.append(str(a[0])) if e == 'open' and isinstance(a[0], str) else None)\n"
            "from auditlab.verifier import verify_snapshot\n"
            "findings = verify_snapshot(sys.argv[1])\n"
            "bad = [p for p in opened if 'private' in p or 'truth' in p]\n"
            "print(json.dumps({'findings': findings, 'bad': bad}))\n")
        with tempfile.TemporaryDirectory() as temporary:
            public = Path(temporary) / "public"
            shutil.copytree(FIXTURES / "S02" / "public", public)
            out = subprocess.run([sys.executable, "-c", code, str(public)], cwd=ROOT, capture_output=True,
                                 text=True, check=True)
            result = json.loads(out.stdout)
            self.assertEqual(result["bad"], [])
            self.assertEqual(len(result["findings"]), 1)

    def test_refuses_private_directory(self):
        from auditlab.verifier.loader import SnapshotError
        with self.assertRaises(SnapshotError):
            verify_snapshot(FIXTURES / "S02" / "private")

    def test_cli_writes_new_report_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "r.json"
            cmd = [sys.executable, "-m", "auditlab.verifier", str(FIXTURES / "S02" / "public"), "--out", str(out)]
            subprocess.run(cmd, cwd=ROOT, check=True, capture_output=True)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(report["findings"][0]["claimed_amount_minor"], 2_000_000)
            self.assertNotEqual(subprocess.run(cmd, cwd=ROOT, capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
