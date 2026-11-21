"""VER-01: an intentional double-credit matcher mutant must fail the same manual oracle.

Only a temporary source copy is changed. Production matcher/reward code is never patched.
The counterexample is S02's public 50,000 - 30,000 = 20,000 KZT payment, submitted twice.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class EvaluatorMutationTests(unittest.TestCase):
    def assert_manual_duplicate_score(self, score):
        # SPEC §9: one real error and two copies of one valid finding => TP=1, FP=1, FN=0.
        # F1=2/3; four inspections: 2*(2/3)-1-4*0.002 = 122/375.
        self.assertEqual(tuple(score[key] for key in ("tp", "fp", "fn")), (1, 1, 0))
        self.assertAlmostEqual(score["f1"], 2 / 3, places=12)
        self.assertAlmostEqual(score["reward"], 122 / 375, places=12)

    def test_manual_counterexample_kills_double_credit_mutant(self):
        temporary = tempfile.TemporaryDirectory(prefix="auditlab-evaluator-mutation-")
        self.addCleanup(temporary.cleanup)
        work = Path(temporary.name)
        supplied = os.environ.get("AUDITLAB_CHECK_ARTIFACTS")
        artifacts = (Path(supplied) if supplied else work) / "evaluator-double-credit"
        artifacts.mkdir(parents=True, exist_ok=False)

        finding = {"type": "payment_allocation", "affected_ids": ["payment/P9", "invoice/I4"],
                   "claimed_amount_minor": 2_000_000, "currency": "KZT",
                   "evidence_ids": ["payments/D9", "ledger/R4", "ledger/R5", "policy/allocation"],
                   "explanation": "50,000 minus 30,000 KZT remains unapplied at the November boundary."}
        duplicate = copy.deepcopy(finding)
        duplicate["affected_ids"].reverse()
        duplicate["evidence_ids"].reverse()
        duplicate["explanation"] = "Same financial assertion in a different order and wording."
        request = {"submission": {"episode_id": "S02", "findings": [finding, duplicate]},
                   "truth": json.loads((ROOT / "fixtures/foundation/S02/private/truth.json").read_text(encoding="utf-8")),
                   "inspection_steps": 4}
        (artifacts / "request.json").write_text(json.dumps(request, indent=2), encoding="utf-8")
        shutil.copytree(ROOT / "fixtures/foundation/S02/public", artifacts / "public")

        mutant_root = work / "mutant"
        package = mutant_root / "auditlab"
        package.mkdir(parents=True)
        for name in ("__init__.py", "contracts.py"):
            shutil.copyfile(ROOT / "auditlab" / name, package / name)
        shutil.copytree(ROOT / "auditlab/evaluator", package / "evaluator",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        matcher = package / "evaluator/matcher.py"
        original = matcher.read_text(encoding="utf-8")
        # Evaluator 1.2: the same mutation on the organizers' tie-break matcher. Every finding with an
        # eligible edge is credited with its first issue, so one issue can be counted twice.
        before = 'matches = sorted((finding, issues[issue]["issue_id"]) for issue, finding in chosen.items())'
        after = 'matches = sorted((finding, issues[edges[0]]["issue_id"]) for finding, edges in enumerate(adjacency) if edges)'
        self.assertEqual(original.count(before), 1, "Matcher changed: review the mutation instead of silently skipping it")
        mutant = original.replace(before, after)
        matcher.write_text(mutant, encoding="utf-8")
        (artifacts / "intentional-mutant-matcher.py").write_text(mutant, encoding="utf-8")
        program = (
            "import json, sys\n"
            "import auditlab.evaluator.matcher as matcher\n"
            "from auditlab.evaluator import score_submission\n"
            "r = json.load(sys.stdin)\n"
            "print(json.dumps({'score': score_submission(r['submission'], r['truth'], r['inspection_steps']), "
            "'source': matcher.__file__}))\n"
        )
        (artifacts / "reproduce.py").write_text(program, encoding="utf-8")
        environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
        outcomes = {}
        for label, checkout in (("production", ROOT), ("intentional_mutant", mutant_root)):
            env = {**environment, "PYTHONPATH": str(checkout)}
            process = subprocess.run([sys.executable, "-c", program], input=json.dumps(request),
                                     cwd=checkout, env=env, capture_output=True, text=True,
                                     encoding="utf-8", timeout=20)
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(process.stdout)
            self.assertEqual(Path(result["source"]).resolve(), (checkout / "auditlab/evaluator/matcher.py").resolve())
            outcomes[label] = result["score"]
            (artifacts / f"{label}-score.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

        self.assert_manual_duplicate_score(outcomes["production"])
        with self.assertRaises(AssertionError) as caught:
            self.assert_manual_duplicate_score(outcomes["intentional_mutant"])
        evidence = {"intentional_test_mutation": True, "status": "detected",
                    "mutation": "Reuse the first eligible truth issue for every candidate finding; uniqueness is removed.",
                    "entry_point": "auditlab.evaluator.score_submission",
                    "expected": {"tp": 1, "fp": 1, "fn": 0, "f1": 2 / 3, "reward": 122 / 375},
                    "outcomes": outcomes, "assertion_failure": str(caught.exception),
                    "source_sha256": hashlib.sha256(original.encode()).hexdigest(),
                    "mutant_sha256": hashlib.sha256(mutant.encode()).hexdigest(),
                    "reproduce": "python -m unittest discover -s tests/integration -p test_evaluator_mutation.py -v"}
        (artifacts / "result.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        self.assertEqual((ROOT / "auditlab/evaluator/matcher.py").read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
