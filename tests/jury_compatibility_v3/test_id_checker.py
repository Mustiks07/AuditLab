"""The external ID checker stays independent of AuditLab's transformer and detects broken links.

Run: python -m unittest discover -s tests/jury_compatibility_v3 -v
"""
import ast
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from review.jury_compatibility_v3 import check_ids, external_transform as ext

ROOT = Path(__file__).resolve().parents[2]
S02 = ROOT / "fixtures" / "foundation" / "S02"
PACKAGE = ROOT / "review" / "jury_compatibility_v3"


def imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def tree_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_file():
            digest.update(str(item.relative_to(path)).encode() + item.read_bytes())
    return digest.hexdigest()


class Independence(unittest.TestCase):
    def test_transformer_imports_nothing_from_auditlab(self):
        self.assertFalse({n for n in imports(PACKAGE / "external_transform.py") if n.startswith("auditlab")})

    def test_checker_never_imports_the_internal_transformers(self):
        names = set()
        for path in PACKAGE.glob("*.py"):
            names |= imports(path)
        self.assertFalse(names & {"auditlab.release.transforms", "auditlab.research.curriculum.families",
                                  "review.genesis_acceptance.data_checks"})


class Transformations(unittest.TestCase):
    def transform(self, root, **options):
        target = Path(root) / options.pop("name")
        meta = ext.transform(S02, target, seed=3, **options)
        truth = json.loads((target / "private/truth.json").read_text(encoding="utf-8"))
        return meta, check_ids.load(target / "public"), truth

    def test_value_based_rename_keeps_every_link_and_maps_back(self):
        before = tree_hash(S02)
        with tempfile.TemporaryDirectory() as root:
            meta, data, truth = self.transform(root, name="d", profile="discover", style="neutral", reorder=True)
            self.assertEqual(check_ids.link_problems(data, truth), [])
            self.assertEqual(check_ids.index_residuals(data), check_ids.index_residuals(check_ids.load(S02 / "public")))
            back = [ext.inverse_reference(r, meta["inverse"]) for r in truth["issues"][0]["evidence_sets"][0]]
            self.assertEqual(back, ["payments/D9", "ledger/R4", "ledger/R5", "policy/allocation"])
        self.assertEqual(tree_hash(S02), before, "the original episode is never modified")

    def test_field_list_rename_is_detected_as_a_stale_settlement_index(self):
        with tempfile.TemporaryDirectory() as root:
            _, data, truth = self.transform(root, name="l", profile="literal", style="neutral", reorder=True)
            problems = check_ids.link_problems(data, truth)
            self.assertEqual(len(problems), 2)
            self.assertTrue(all("settlement_index.unapplied_row_ids" in p for p in problems))
            self.assertEqual(check_ids.index_residuals(data), [(5000000, "KZT", "index_unresolved")])

    def test_opaque_truth_references_stay_on_old_ids(self):
        with tempfile.TemporaryDirectory() as root:
            _, data, truth = self.transform(root, name="o", profile="literal", style="neutral", truth_mode="opaque", reorder=False)
            self.assertEqual(truth["issues"][0]["evidence_sets"][0][1], "ledger/R4")
            self.assertTrue(any("unknown reference ledger/R4" in p for p in check_ids.link_problems(data, truth)))

    def test_fx_and_policy_keys_and_amounts_are_never_renamed(self):
        with tempfile.TemporaryDirectory() as root:
            _, data, truth = self.transform(root, name="k", profile="discover", style="numeric", reorder=True)
            original = check_ids.load(S02 / "public")
            self.assertEqual(sorted(data["policy"]), sorted(original["policy"]))
            self.assertEqual(sorted(map(json.dumps, data["fx"])), sorted(map(json.dumps, original["fx"])))
            self.assertEqual(sorted((r["debit_minor"], r["credit_minor"]) for r in data["ledger"]),
                             sorted((r["debit_minor"], r["credit_minor"]) for r in original["ledger"]))
            self.assertIn("policy/allocation", truth["issues"][0]["evidence_sets"][0])


class LinkSelfCheck(unittest.TestCase):
    def test_cli_exit_codes(self):
        from review.jury_compatibility_v3 import check_links
        with tempfile.TemporaryDirectory() as root:
            ext.transform(S02, Path(root) / "ok", profile="discover", seed=5, style="neutral")
            ext.transform(S02, Path(root) / "stale", profile="literal", seed=5, style="neutral")
            self.assertEqual(check_links.main([str(S02)]), 0)
            self.assertEqual(check_links.main([str(Path(root) / "ok")]), 0)
            self.assertEqual(check_links.main([str(Path(root) / "stale")]), 1)
            self.assertEqual(check_links.main([str(Path(root) / "missing")]), 2)


class Gate(unittest.TestCase):
    def test_gate_requires_complete_official_layer_and_compatible_profiles(self):
        from review.jury_compatibility_v3 import gate
        genesis = {"meta": {"commit": "abc", "product_code_dirty": False},
                   "summary_official_layer": {"contract": {"passed": 20, "failed": 0, "not_checked": 0}}}
        ids = {"commit": "abc", "replay_identical": {"passed": 2, "total": 2},
               "summary": {**{n: {"passed": 2, "total": 2} for n in gate.GATED_ID_PROFILES},
                           "literal+reorder": {"passed": 0, "total": 2}}}
        self.assertEqual(gate.check(genesis, ids, "abc")[0], 0, "informative profiles never fail the gate")
        self.assertEqual(gate.check(genesis, ids, "other")[0], 2)
        skipped = json.loads(json.dumps(genesis)); skipped["summary_official_layer"]["contract"]["not_checked"] = 1
        self.assertEqual(gate.check(skipped, ids, "abc")[0], 1, "not_checked is not a pass")
        broken = json.loads(json.dumps(ids)); broken["summary"]["reorder"]["passed"] = 1
        self.assertEqual(gate.check(genesis, broken, "abc")[0], 1)


if __name__ == "__main__":
    unittest.main()
