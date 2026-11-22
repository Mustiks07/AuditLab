"""Positive and negative controls for protocol independent-assurance-2.0 (Claude Pro 2, #43).

Each check must pass on a genuine case and fail (or say not_checked) on a deliberately broken one.
"""
import csv, io, json, random, shutil, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

try:
    import judge_adapter as ja
    from auditlab.generator import generate_episode
    HAVE_GEN = True
except Exception:  # pragma: no cover
    HAVE_GEN = False

Z = {"duplicate_invoice": 0, "payment_allocation": 0, "fx_conversion": 0, "period_cutoff": 0}


def cfg(**kw):
    c = {"config_version": "1.1", "company_size": {"ledger_rows_target": 200, "documents_target": 40}, "base_currency": "KZT",
         "currencies": ["KZT", "USD"], "timezone": "Asia/Almaty", "target_period": "2026-11", "interaction": None,
         "clean_distractors": ["split_payment", "refund"], "issue_counts": dict(Z)}
    c.update(kw)
    return c


def rewrite_ledger(d, fn):
    rows = list(csv.DictReader(io.StringIO((d / "public/ledger.csv").read_text())))
    rows = fn(rows)
    buf = io.StringIO(); w = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n"); w.writeheader(); w.writerows(rows)
    (d / "public/ledger.csv").write_text(buf.getvalue())


@unittest.skipUnless(HAVE_GEN, "not_checked: generator/judge_adapter unavailable")
class AssuranceV2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        generate_episode(9401, cfg(issue_counts={**Z, "payment_allocation": 1}), cls.tmp / "alloc")
        generate_episode(9402, cfg(issue_counts={**Z, "payment_allocation": 1}, allocation_mode="omitted"), cls.tmp / "omit")
        generate_episode(9402, cfg(allocation_mode="omitted"), cls.tmp / "omit-clean")
        generate_episode(9403, cfg(issue_counts={**Z, "duplicate_invoice": 1, "payment_allocation": 1}), cls.tmp / "two")
        generate_episode(9404, cfg(issue_counts={**Z, "duplicate_invoice": 1}), cls.tmp / "dup")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def copy(self, name):
        d = self.tmp / f"{name}-{random.random()}"
        shutil.copytree(self.tmp / name, d)
        return d

    # ---------------------------------------------------------------- P8 solver
    def test_solver_uses_only_adapter_replies(self):
        """solve() gets a step function and the first observation, never a path."""
        from auditlab.environment import open_session
        from review.agent_generalization.solver import solve
        session = open_session(self.tmp / "alloc" / "public")
        calls = []
        def step(a):
            calls.append(a["name"])
            r = session.inspect({"action": a["name"], "args": a["args"]})
            return {"result": r.get("result"), "error": r.get("error")}
        manifest = json.loads((self.tmp / "alloc/public/manifest.json").read_text())
        answer = solve(step, {"manifest": manifest})
        truth = json.loads((self.tmp / "alloc/private/truth.json").read_text())
        self.assertEqual({c for c in calls}, {"list_records"})
        self.assertEqual([(f["type"], f["claimed_amount_minor"]) for f in answer["findings"]],
                         [(i["type"], i["expected_amount_minor"]) for i in truth["issues"]])

    def test_solver_passes_on_genuine_error_and_clean(self):
        from review.agent_generalization.solver import run_solver
        for name in ("alloc", "omit", "omit-clean", "two"):
            r = run_solver(ja, self.tmp / name, self.tmp / f"{name}.solver")
            self.assertEqual(r["status"], "passed", (name, r))
            self.assertTrue(r["official_equals_reference"])

    def test_solver_without_budget_is_not_checked_not_passed(self):
        from review.agent_generalization.solver import run_solver
        r = run_solver(ja, self.tmp / "alloc", self.tmp / "budget.solver", max_actions=4)
        self.assertEqual(r["status"], "not_checked")

    def test_solver_on_hidden_document_never_passes(self):
        from review.agent_generalization.solver import run_solver
        d = self.copy("alloc")
        truth = json.loads((d / "private/truth.json").read_text())
        doc = next(r.split("/", 1)[1] for i in truth["issues"] for g in i["evidence_sets"] for r in g if r.startswith("payments/"))
        lines = [l for l in (d / "public/payments.jsonl").read_text().splitlines() if json.loads(l)["doc_id"] != doc]
        (d / "public/payments.jsonl").write_text("\n".join(lines) + "\n")
        self.assertNotEqual(run_solver(ja, d, d.parent / (d.name + ".s"))["status"], "passed")

    def test_witness_is_labelled_as_not_a_solution(self):
        from review.agent_generalization.validator import witness
        truth = json.loads((self.tmp / "alloc/private/truth.json").read_text())
        w = witness(ja, self.tmp / "alloc", truth)
        self.assertEqual(w["status"], "passed")
        self.assertFalse(w["is_independent_solution"]); self.assertEqual(w["submit_built_from"], "truth")

    # ---------------------------------------------------------------- P4, P10, P11
    def test_declared_mutation_must_match_the_real_change(self):
        from review.agent_generalization.validator import mutation_declared
        from review.genesis_acceptance.independent_calc import audit
        d = self.copy("alloc")
        answer = audit(d / "public")
        self.assertEqual(mutation_declared(d / "private", answer)["status"], "passed")
        prov = json.loads((d / "private/provenance.json").read_text())
        prov["mutations"][0]["after"]["remaining_minor"] += 100
        (d / "private/provenance.json").write_text(json.dumps(prov))
        self.assertEqual(mutation_declared(d / "private", answer)["status"], "failed")

    def test_regeneration_detects_an_edited_public_file(self):
        from review.agent_generalization.validator import reproducible
        d = self.copy("omit")
        self.assertEqual(reproducible(d, self.tmp / "regen-ok")["status"], "passed")
        rewrite_ledger(d, lambda rows: rows[:-1] + [{**rows[-1], "timestamp": rows[-1]["timestamp"].replace(":00+", ":01+", 1)}])
        r = reproducible(d, self.tmp / "regen-bad")
        self.assertEqual(r["status"], "failed"); self.assertIn("public/ledger.csv", r["differing"])

    def test_passport_is_bound_to_exact_files(self):
        from review.agent_generalization.validator import passport_binding, verify_passport
        d = self.copy("alloc")
        passport = {"binding": passport_binding(d)}
        self.assertEqual(verify_passport(passport, d)["status"], "passed")
        (d / "private/truth.json").write_text((d / "private/truth.json").read_text() + " ")
        self.assertEqual(verify_passport(passport, d)["status"], "failed")

    # ---------------------------------------------------------------- P9 pairs
    def test_matched_clean_control(self):
        from review.agent_generalization.pairs import matched_clean_control
        from review.agent_generalization.validator import clean_variant
        self.assertEqual(matched_clean_control(self.tmp / "omit", self.tmp / "omit-clean")["status"], "passed")
        # D2: the duplicate's own source book is a smaller book, a visible signal of the clean member
        clean = clean_variant(self.tmp / "dup", self.tmp / f"dup-sb-{random.random()}")
        r = matched_clean_control(self.tmp / "dup", clean)
        self.assertEqual(r["status"], "failed"); self.assertIn("ledger_rows", r["differing_features"])
        # a clean member with an error in it is not a control
        self.assertEqual(matched_clean_control(self.tmp / "omit", self.tmp / "omit")["status"], "failed")

    # ---------------------------------------------------------------- metamorphic relations
    def test_relations_hold_and_the_checker_catches_a_wrong_prediction(self):
        from review.agent_generalization import metamorphic as mr
        from review.agent_generalization.mr_study import check, relations
        rows = relations(ja, self.tmp / "two", 5, self.tmp / "mr-two")
        names = {r["relation"]: r["status"] for r in rows}
        for rel in ("MR1_rename_reorder", "MR3_revert_duplicate_invoice", "MR3_revert_payment_allocation", "MR5_fix_one_keep_other",
                    "MR3_own_allocation_fix", "MR4_allocation_amount", "MR2_archive_copy"):
            self.assertEqual(names.get(rel), "passed", (rel, rows))
        ep = self.tmp / "mr-two" / "mr4a"
        truth = json.loads((ep / "private/truth.json").read_text())
        a0 = mr.solver_answer(ja, ep)["findings"]
        wrong = [{**f, "claimed_amount_minor": f["claimed_amount_minor"] + 1} for f in a0]
        self.assertEqual(check(ja, "negative", ep, wrong, truth["issues"])["status"], "failed")
        # a stale answer that is still accepted after a meaningful change must be reported
        self.assertEqual(check(ja, "negative-stale", ep, a0, truth["issues"], stale=a0, meaning_changed=True)["status"], "failed")

    # ---------------------------------------------------------------- mutation testing harness
    def test_every_mutant_anchor_exists_exactly_once(self):
        from review.agent_generalization.mutation_testing import MUTANTS
        for name, (rel, old, new) in MUTANTS.items():
            self.assertEqual((ROOT / rel).read_text(encoding="utf-8").count(old), 1, name)
            self.assertNotEqual(old, new, name)

    def test_battery_expectations_match_the_official_scorer_on_one_episode(self):
        from review.agent_generalization.mutation_testing import battery, expected
        from auditlab.evaluator.service import handle
        cases = battery([self.tmp / "two"])
        self.assertGreater(len(cases), 30)
        for c in cases:
            r = handle({"private_dir": c["private_dir"], "public_dir": c["public_dir"], "submission": c["submission"],
                        "inspection_steps": c["steps"], "rules": "genesis-2026-11-20"})
            e = expected(c); s = r["score"]
            self.assertEqual((r["status"], s["tp"], s["fp"], s["fn"], round(s["reward"], 9)),
                             (e["status"], e["tp"], e["fp"], e["fn"], e["reward"]), c["case"])


class StatisticsAndPartition(unittest.TestCase):
    def test_service_failure_is_not_a_model_miss(self):
        from review.agent_generalization import compare
        miss = {"tp": 0, "fp": 0, "fn": 1, "valid": True, "return": -1, "excluded": False, "n_findings": 0}
        rows = [{"status": "provider_error", "variant": "error", "truth_issues": 1, "score": miss},
                {"status": "completed", "variant": "clean", "truth_issues": 0,
                 "score": {"tp": 0, "fp": 0, "fn": 0, "valid": True, "return": 1, "excluded": False, "n_findings": 0}}]
        m = compare.metrics(rows)
        self.assertEqual(m["micro_f1"]["fn"], 0); self.assertEqual(m["micro_f1_by_rule"]["fn"], 1)
        self.assertEqual(m["service_outcomes"], 1)

    @unittest.skipUnless(HAVE_GEN, "not_checked: generator unavailable")
    def test_lineage_rejects_a_renamed_copy_in_another_partition(self):
        from review.agent_generalization import lineage
        from review.genesis_acceptance.data_checks import transform
        tmp = Path(tempfile.mkdtemp())
        generate_episode(9405, cfg(issue_counts={**Z, "period_cutoff": 1}), tmp / "root")
        transform(tmp / "root", tmp / "copy", 3, "both")
        same = [{"id": "r", "split": "holdout", "episode": tmp / "root"}, {"id": "c", "split": "holdout", "episode": tmp / "copy", "parent": "r"}]
        self.assertEqual(lineage.check(same)["status"], "passed")
        other = [{"id": "r", "split": "holdout", "episode": tmp / "root"}, {"id": "c", "split": "dev", "episode": tmp / "copy"}]
        r = lineage.check(other)
        self.assertEqual(r["status"], "failed"); self.assertIn("public_history", {v["via"] for v in r["violations"]})
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
