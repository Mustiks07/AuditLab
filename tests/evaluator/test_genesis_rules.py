"""Genesis matcher rules through the official score_episode (fintech-checks 2026-11-20, blocks 3 and M01-M12).

Every expected number below is written from the published table and re-derived here with exact
rational arithmetic (`expected_reward`), not taken from AuditLab's own matcher. Fixtures copy the
public files of fixtures/foundation/S02 and write a closed truth.json next to them, as the judge
would; the "missing" evidence ID U is chosen after reading the public data.
"""
from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import judge_adapter  # noqa: E402  the official entry point, as the judge imports it
from auditlab.evaluator.evidence import PublicEvidence  # noqa: E402
from auditlab.evaluator.matcher import TruthError, _lexmin_matching, _max_matching, evaluate  # noqa: E402
from auditlab.evaluator.oracle import oracle_submission  # noqa: E402
from auditlab.evaluator.service import handle  # noqa: E402

S02 = ROOT / "fixtures" / "foundation" / "S02"
EPISODE = "S02"
# Records and evidence of the published fixtures, mapped onto existing S02 data.
R1, R2, R3 = "invoice/I4", "payment/P9", "ledger/R1"
D1, D2, D3 = "payments/D9", "ledger/R4", "ledger/R5"
U = "ledger/R404"
TOLERANCE = 1e-9  # "абсолютная погрешность не более 10^-9"


def expected_reward(tp: int, fp: int, fn: int, steps: int) -> Fraction:
    """clip(2*F1 - 1 - 0.002*steps, -1, 1), F1 = 1 when truth and findings are both empty."""
    f1 = Fraction(1) if 2 * tp + fp + fn == 0 else Fraction(2 * tp, 2 * tp + fp + fn)
    return max(Fraction(-1), min(Fraction(1), 2 * f1 - 1 - Fraction(2, 1000) * steps))


def issue(issue_id="A", type_="payment_allocation", affected=(R1,), amount=10000, currency="KZT",
          evidence=((D1, D2),)):
    return {"issue_id": issue_id, "type": type_, "affected_ids": list(affected), "expected_amount_minor": amount,
            "currency": currency, "evidence_sets": [list(group) for group in evidence]}


def finding(type_="payment_allocation", affected=(R1,), amount=10000, currency="KZT", evidence=(D1, D2),
            explanation="Allocation short by 10000 minor units."):
    return {"type": type_, "affected_ids": list(affected), "claimed_amount_minor": amount, "currency": currency,
            "evidence_ids": list(evidence), "explanation": explanation}


def submission(*findings):
    return {"episode_id": EPISODE, "findings": list(findings)}


class Fixture:
    """A judge-side episode directory: S02 public files plus a written closed truth."""

    def __init__(self, issues, tolerance=None):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "episode"
        shutil.copytree(S02 / "public", self.dir / "public")
        (self.dir / "private").mkdir()
        (self.dir / "private" / "truth.json").write_text(
            json.dumps({"episode_id": EPISODE, "issues": issues}), encoding="utf-8")
        if tolerance is not None:
            policy_file = self.dir / "public" / "policy.json"
            policy = json.loads(policy_file.read_text(encoding="utf-8"))
            policy["matching_tolerance_minor"] = tolerance
            policy_file.write_text(json.dumps(policy), encoding="utf-8")

    def score(self, body, steps=10):
        return judge_adapter.score_episode(str(self.dir), body, steps)

    def detail(self, body, steps=10):
        reply = handle({"private_dir": str(self.dir / "private"), "submission": body, "inspection_steps": steps,
                        "rules": "genesis-2026-11-20"})
        return json.loads(json.dumps(reply))  # as it crosses the evaluator process boundary

    def close(self):
        self._tmp.cleanup()


class FixtureCase(unittest.TestCase):
    issues = [issue()]

    def setUp(self):
        self.fixture = Fixture(copy.deepcopy(self.issues))

    def tearDown(self):
        self.fixture.close()

    def assertScore(self, body, tp, fp, fn, steps=10, published=None):
        result = self.fixture.score(body, steps)
        self.assertEqual(set(result), {"tp", "fp", "fn", "precision", "recall", "f1", "return"})
        self.assertEqual((result["tp"], result["fp"], result["fn"]), (tp, fp, fn))
        reward = expected_reward(tp, fp, fn, steps)
        self.assertLessEqual(abs(result["return"] - float(reward)), TOLERANCE)
        if published is not None:  # the number printed in the organizers' table
            self.assertLessEqual(abs(result["return"] - float(published)), TOLERANCE)
        precision = Fraction(tp, tp + fp) if tp + fp else Fraction(0)
        recall = Fraction(tp, tp + fn) if tp + fn else Fraction(0)
        f1 = Fraction(1) if 2 * tp + fp + fn == 0 else Fraction(2 * tp, 2 * tp + fp + fn)
        for key, value in (("precision", precision), ("recall", recall), ("f1", f1)):
            self.assertLessEqual(abs(result[key] - float(value)), TOLERANCE, key)
        return result

    def assertInvalid(self, body, steps=10):
        result = self.fixture.score(body, steps)
        self.assertEqual(result, {"tp": None, "fp": None, "fn": None, "precision": None, "recall": None,
                                  "f1": None, "return": -1.0})
        return result


class FixtureSanity(FixtureCase):
    def test_fixture_ids_exist_and_u_does_not(self):
        available = PublicEvidence(self.fixture.dir / "public")
        for key in (D1, D2, D3, "invoices/D4", "policy/allocation", *(f"ledger/R{i}" for i in range(1, 9))):
            self.assertIn(key, available)
        self.assertNotIn(U, available)  # chosen after reading the public data
        self.assertNotIn("payments/D4", available)  # an ID of another table is not available here


class RewardVectors(FixtureCase):
    """Block 3, tests 1-20 (fixture: one issue A, tolerance 0, inspection_steps 10 by default)."""

    def test_01_one_correct_finding(self):
        self.assertScore(submission(finding()), 1, 0, 0, published=Fraction(98, 100))

    def test_02_empty_findings_on_an_error(self):
        self.assertScore(submission(), 0, 0, 1, published=-1)

    def test_05_two_identical_findings(self):
        self.assertScore(submission(finding(), finding()), 1, 1, 0, published=Fraction(47, 150))

    def test_06_wrong_supported_type(self):
        self.assertScore(submission(finding(type_="period_cutoff")), 0, 1, 1, published=-1)

    def test_07_other_existing_record(self):
        self.assertScore(submission(finding(affected=(R3,))), 0, 1, 1, published=-1)

    def test_08_amount_off_by_one_at_zero_tolerance(self):
        self.assertScore(submission(finding(amount=10001)), 0, 1, 1, published=-1)

    def test_09_other_currency(self):
        self.assertScore(submission(finding(currency="USD")), 0, 1, 1, published=-1)

    def test_10_incomplete_evidence(self):
        self.assertScore(submission(finding(evidence=(D1,))), 0, 1, 1, published=-1)

    def test_11_missing_evidence_instead_of_d2(self):
        self.assertScore(submission(finding(evidence=(D1, U))), 0, 1, 1, published=-1)

    def test_12_flag_every_record_with_every_type(self):
        records = (R1, R2, R3, "ledger/R2", "ledger/R3")
        types = ("duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff")
        spam = [finding(type_=t, affected=(r,)) for r in records for t in types]
        self.assertEqual(len(spam), 20)
        self.assertScore(submission(*spam), 1, 19, 0, published=Fraction(-871, 1050))

    def test_15_16_17_step_cost_unit(self):
        for steps, published in ((0, 1), (50, Fraction(90, 100)), (100, Fraction(80, 100))):
            with self.subTest(steps=steps):
                self.assertScore(submission(finding()), 1, 0, 0, steps=steps, published=published)

    def test_18_thirty_one_findings(self):
        self.assertInvalid(submission(*[finding() for _ in range(31)]))
        self.assertScore(submission(*[finding() for _ in range(30)]), 1, 29, 0)  # the limit itself is valid

    def test_19_eleven_distinct_evidence_ids(self):
        eleven = [f"ledger/R{i}" for i in range(1, 9)] + [D1, "invoices/D4", "policy/allocation"]
        self.assertEqual(len(set(eleven)), 11)
        self.assertInvalid(submission(finding(evidence=eleven)))

    def test_20_explanation_of_1001_ascii_characters(self):
        self.assertInvalid(submission(finding(explanation="x" * 1001)))
        self.assertScore(submission(finding(explanation="x" * 1000)), 1, 0, 0)

    def test_invalid_structure_is_logged_on_the_trusted_side_only(self):
        detail = self.fixture.detail(submission(*[finding() for _ in range(31)]))
        self.assertEqual((detail["status"], detail["reason"]), ("invalid_submission", "too_many_findings"))


class CleanTruth(FixtureCase):
    issues = []

    def test_03_empty_truth_and_empty_findings(self):
        self.assertScore(submission(), 0, 0, 0, published=Fraction(98, 100))

    def test_04_empty_truth_one_false_finding(self):
        self.assertScore(submission(finding()), 0, 1, 0, published=-1)


class TwoIssues(FixtureCase):
    issues = [issue(), issue("B", type_="duplicate_invoice", amount=5000, evidence=(("invoices/D4", "ledger/R1"),))]

    def test_13_only_a(self):
        self.assertScore(submission(finding()), 1, 0, 1, published=Fraction(47, 150))

    def test_14_a_and_one_false(self):
        self.assertScore(submission(finding(), finding(type_="fx_conversion")), 1, 1, 1, published=Fraction(-2, 100))


class OpenExamples(FixtureCase):
    """M01-M12: A affects {R1, R2}, 10000 KZT, admissible {D1, D2}; D3 extra available; U absent."""

    issues = [issue(affected=(R1, R2))]

    def correct(self, **changes):
        return finding(**{"affected": (R1, R2), **changes})

    def test_m01_repeated_affected_id_is_one_set(self):
        self.assertScore(submission(self.correct(affected=(R2, R1, R1))), 1, 0, 0)

    def test_m02_extra_unaffected_record(self):
        self.assertScore(submission(self.correct(affected=(R1, R2, R3))), 0, 1, 1)

    def test_m03_extra_available_evidence(self):
        self.assertScore(submission(self.correct(evidence=(D2, D1, D3))), 1, 0, 0)

    def test_m04_extra_unavailable_evidence(self):
        self.assertScore(submission(self.correct(evidence=(D1, D2, U))), 0, 1, 1)
        detail = self.fixture.detail(submission(self.correct(evidence=(D1, D2, U))))
        self.assertEqual(detail["unmatched"], {"0": "unavailable_evidence"})

    def test_m05_limit_is_checked_before_removing_repeats(self):
        self.assertInvalid(submission(self.correct(evidence=(D2,) + (D1,) * 10)))

    def test_m06_currency_is_exact(self):
        for currency in ("kzt", " KZT", "KZT "):
            with self.subTest(currency=currency):
                self.assertScore(submission(self.correct(currency=currency)), 0, 1, 1)

    def test_m07_amount_at_zero_tolerance(self):
        self.assertScore(submission(self.correct(amount=10001)), 0, 1, 1)

    def test_m09_amount_must_be_a_json_integer(self):
        for amount in ("10000", True, 10000.0, None):
            with self.subTest(amount=amount):
                self.assertInvalid(submission(self.correct(amount=amount)))

    def test_m12_identical_findings_are_not_merged(self):
        self.assertScore(submission(self.correct(), self.correct()), 1, 1, 0, published=Fraction(47, 150))


class PublishedTolerance(unittest.TestCase):
    def test_m08_inclusive_boundary_with_a_public_tolerance_of_one(self):
        fixture = Fixture([issue(affected=(R1, R2))], tolerance=1)
        try:
            self.assertEqual(fixture.score(submission(finding(affected=(R1, R2), amount=10001)))["tp"], 1)
            self.assertEqual(fixture.score(submission(finding(affected=(R1, R2), amount=9999)))["tp"], 1)
            self.assertEqual(fixture.score(submission(finding(affected=(R1, R2), amount=10002)))["tp"], 0)
        finally:
            fixture.close()
        other = Fixture([issue(affected=(R1, R2))])  # the other fixtures keep tolerance 0
        try:
            self.assertEqual(other.score(submission(finding(affected=(R1, R2), amount=10001)))["tp"], 0)
        finally:
            other.close()

    def test_per_type_tolerance_and_bad_values(self):
        fixture = Fixture([issue()], tolerance={"payment_allocation": 2})
        try:
            self.assertEqual(fixture.score(submission(finding(amount=10002)))["tp"], 1)
        finally:
            fixture.close()
        for bad in (-1, 1.5, True, "1", {"payment_allocation": -1}):
            broken = Fixture([issue()], tolerance=bad)
            try:
                with self.subTest(tolerance=bad), self.assertRaises(ValueError):
                    broken.score(submission(finding()))
            finally:
                broken.close()


class Ambiguity(unittest.TestCase):
    """M10, M11 and the integrator's direct algorithm probe."""

    def test_integrator_probe_m11_owners(self):
        # Before: Kuhn's order gave A->F1, B->F0 ([1, 0]). Required: A->F0, B->F1.
        self.assertEqual(_lexmin_matching([[0, 1], [0, 1]]), {0: 0, 1: 1})
        self.assertEqual(_max_matching([[0, 1], [0, 1]], 2), [0, 1])  # the exact call of probe.py

    def test_m10_maximum_matching_not_greedy(self):
        fixture = Fixture([issue("A", affected=(R1, R2), evidence=((D1, D2),)),
                           issue("B", affected=(R1, R2), evidence=((D3,),))])
        try:
            f0 = finding(affected=(R1, R2), evidence=(D1, D2, D3))  # fits A and B
            f1 = finding(affected=(R1, R2), evidence=(D1, D2))      # fits A only
            body = submission(f0, f1)
            self.assertEqual(fixture.detail(body)["matches"], [[0, "B"], [1, "A"]])
            result = fixture.score(body)
            self.assertEqual((result["tp"], result["fp"], result["fn"]), (2, 0, 0))
        finally:
            fixture.close()

    def test_m11_deterministic_choice_and_order_independence(self):
        fixture = Fixture([issue("B", affected=(R1, R2)), issue("A", affected=(R1, R2))])
        try:
            f0 = finding(affected=(R1, R2), explanation="first")
            f1 = finding(affected=(R1, R2), explanation="second")
            self.assertEqual(fixture.detail(submission(f0, f1))["matches"], [[0, "A"], [1, "B"]])
            self.assertEqual(fixture.score(submission(f0, f1)), fixture.score(submission(f1, f0)))
            self.assertEqual(fixture.score(submission(f0, f1))["tp"], 2)
        finally:
            fixture.close()

    def test_issue_ids_sort_by_code_points(self):
        # "Z" (U+005A) sorts before "a" (U+0061); locale-aware or case-folded order would not.
        fixture = Fixture([issue("a", affected=(R1, R2)), issue("Z", affected=(R1, R2))])
        try:
            body = submission(finding(affected=(R1, R2)), finding(affected=(R1, R2)))
            self.assertEqual(fixture.detail(body)["matches"], [[0, "Z"], [1, "a"]])
        finally:
            fixture.close()

    def test_lexmin_against_brute_force(self):
        import itertools
        import random
        rng = random.Random(20261120)
        for _ in range(300):
            issues, findings = rng.randint(0, 4), rng.randint(0, 5)
            candidates = [sorted(rng.sample(range(findings), rng.randint(0, findings))) for _ in range(issues)]
            best = None
            for assignment in itertools.product(*[[None, *c] for c in candidates]):
                used = [f for f in assignment if f is not None]
                if len(used) != len(set(used)):
                    continue
                pairs = sorted((j, f) for j, f in enumerate(assignment) if f is not None)
                key = (-len(pairs), pairs)
                best = key if best is None or key < best else best
            chosen = _lexmin_matching(candidates)
            self.assertEqual(sorted(chosen.items()), best[1], candidates)


class StructureVersusSemantics(FixtureCase):
    def test_unknown_string_type_is_a_false_positive_not_a_broken_submit(self):
        result = self.assertScore(submission(finding(type_="not_a_known_type")), 0, 1, 1)
        self.assertEqual(result["return"], -1.0)
        self.assertScore(submission(finding(), finding(type_="not_a_known_type")), 1, 1, 0)

    def test_non_string_type_and_missing_fields_are_structural(self):
        broken = finding()
        del broken["currency"]
        for body in (submission(finding(type_=5)), submission(finding(type_=None)), submission(broken),
                     submission(finding(evidence="payments/D9")), submission(finding(affected=(R1, 7))),
                     {"episode_id": EPISODE}, {"findings": []}, [], {"episode_id": "OTHER", "findings": []}):
            with self.subTest(body=body):
                self.assertInvalid(body)

    def test_negative_integer_amount_is_semantic(self):
        self.assertScore(submission(finding(amount=-10000)), 0, 1, 1)

    def test_explanation_counts_unicode_code_points(self):
        self.assertScore(submission(finding(explanation="ж" * 1000)), 1, 0, 0)    # 2000 UTF-8 bytes
        self.assertScore(submission(finding(explanation="\U0001F600" * 1000)), 1, 0, 0)
        self.assertInvalid(submission(finding(explanation="ж" * 1001)))

    def test_ids_are_exact_strings(self):
        for affected in (("Invoice/I4",), (" invoice/I4",), ("invoice/i4",)):
            with self.subTest(affected=affected):
                self.assertScore(submission(finding(affected=affected)), 0, 1, 1)


class InternalRulesUnchanged(unittest.TestCase):
    """The default AuditLab rules (public schema 1.0) keep their published structure."""

    def truth(self):
        return json.loads((S02 / "private" / "truth.json").read_text(encoding="utf-8"))

    def test_unknown_type_and_negative_amount_stay_invalid_internally(self):
        body = oracle_submission(self.truth())
        body["findings"][0]["type"] = "not_a_known_type"
        self.assertEqual(evaluate(body, self.truth(), 10).reason, "bad_type")
        body = oracle_submission(self.truth())
        body["findings"][0]["claimed_amount_minor"] = -1
        self.assertEqual(evaluate(body, self.truth(), 10).reason, "bad_amount")

    def test_duplicate_cause_still_rejected_internally(self):
        truth = self.truth()
        truth["issues"].append(copy.deepcopy(truth["issues"][0]) | {"issue_id": "X2"})
        with self.assertRaises(TruthError):
            evaluate(oracle_submission(self.truth()), truth, 0)
        self.assertEqual(evaluate(oracle_submission(self.truth()), truth, 0, rules="genesis-2026-11-20").score["tp"], 1)

    def test_integrator_probe_extra_unavailable_evidence_internal_service(self):
        # Integrator defect 2: the trusted service now checks every cited key, also for the default rules.
        body = oracle_submission(self.truth())
        body["findings"][0]["evidence_ids"].append("payments/DOES_NOT_EXIST")
        result = handle({"private_dir": str(S02 / "private"), "submission": body, "inspection_steps": 10})
        self.assertEqual((result["score"]["tp"], result["score"]["fp"], result["score"]["fn"]), (0, 1, 1))
        self.assertEqual(result["unmatched"], {"0": "unavailable_evidence"})
        self.assertNotIn("rules", result)  # the default reply keeps its 1.1 fields
        clean = handle({"private_dir": str(S02 / "private"), "submission": oracle_submission(self.truth()),
                        "inspection_steps": 10})
        self.assertEqual(clean["score"]["reward"], 0.98)


if __name__ == "__main__":
    unittest.main()
