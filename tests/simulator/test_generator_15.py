"""Observable minor-unit faults and independently auditable clean source books."""
import csv
import json
import unittest
from hashlib import sha256
from itertools import product
from pathlib import Path
from tempfile import TemporaryDirectory

from auditlab.environment import open_session
from auditlab.generator import generate_episode
from auditlab.verifier import verify_report
from auditlab.verifier.crosscheck import compare
from test_policies import run_public_policy


class Generator15Tests(unittest.TestCase):
    def test_observable_fx_across_forty_seeds_and_three_minor_unit_exponents(self):
        # Includes the concrete seed=0 EUR61 * 1.21 / 1.22 -> JPY74 regression.
        with TemporaryDirectory() as temporary:
            for base, interaction, seed in product(
                ("JPY", "USD", "KWD"),
                ("allocation_fx_same_payment", "cutoff_fx_same_invoice"), range(40),
            ):
                with self.subTest(base=base, interaction=interaction, seed=seed):
                    root = Path(temporary) / f"{base}-{interaction}-{seed}"
                    generate_episode(seed, {"config_version": "1.1", "base_currency": base,
                        "currencies": [base, "EUR"], "clean_distractors": [],
                        "interaction": interaction, "issue_counts": {"fx_conversion": 1}}, root)
                    public = root / "public"
                    # Independent verifier sees the documents before this test opens truth.
                    report = verify_report(public)
                    truth = json.loads((root / "private/truth.json").read_bytes())
                    self.assertEqual(compare(report, truth, public), [])
                    [issue] = truth["issues"]
                    self.assertEqual(issue["type"], "fx_conversion")
                    self.assertGreater(issue["expected_amount_minor"], 0)
                    self.assertEqual(report["notes"], [])
                    with (public / "fx.csv").open() as stream:
                        quotes = list(csv.DictReader(stream))
                    self.assertEqual(len(quotes), len({(q["date"], q["source_currency"], q["target_currency"])
                                                       for q in quotes}))
                    findings = run_public_policy("reconcile", 17, public)[-1]["args"]["findings"]
                    self.assertEqual(findings[0]["claimed_amount_minor"], issue["expected_amount_minor"])
                    self.assertEqual(len(findings), 1)

    def test_all_interaction_toggles_share_identical_complete_clean_book(self):
        with TemporaryDirectory() as temporary:
            for base, (interaction, kind) in product(("JPY", "USD", "KWD"), (
                ("allocation_fx_same_payment", "payment_allocation"),
                ("cutoff_fx_same_invoice", "period_cutoff"),
            )):
                baseline = None
                for first, fx in product((0, 1), repeat=2):
                    root = Path(temporary) / f"{base}-{interaction}-{first}-{fx}"
                    generate_episode(0, {"config_version": "1.1", "base_currency": base,
                        "currencies": [base, "EUR"], "clean_distractors": [],
                        "interaction": interaction, "issue_counts": {kind: first, "fx_conversion": fx}}, root)
                    source = root / "private/source_book"
                    contents = {p.name: p.read_bytes() for p in source.iterdir()}
                    if baseline is None:
                        baseline = contents
                    self.assertEqual(contents, baseline)
                    report = verify_report(source)
                    self.assertEqual((report["findings"], report["notes"]), ([], []))
                    self.assertEqual(len(json.loads((root / "private/truth.json").read_bytes())["issues"]), first + fx)

    def test_full_clean_book_hashes_transition_and_private_boundary(self):
        from tools.check_source_books import check_source_book
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "all"
            generate_episode(17, {"config_version": "1.1", "company_size": {
                "ledger_rows_target": 2000, "documents_target": 200}, "issue_counts": dict.fromkeys(
                ("duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff"), 3)}, root)
            result = check_source_book(root)
            self.assertEqual(result["status"], "passed", result)
            self.assertEqual(result["source_findings"], 0)
            self.assertEqual(result["target_findings"], 12)
            provenance = json.loads((root / "private/provenance.json").read_bytes())
            metadata = provenance["source_book"]
            self.assertEqual(len(metadata["files_sha256"]), 6)
            source = root / "private/source_book"
            for filename, digest in metadata["files_sha256"].items():
                self.assertEqual(sha256((source / filename).read_bytes()).hexdigest(), digest)
            session = open_session(root / "public")
            for table in ("source_book", "source_transition", "provenance", "../private/source_book"):
                self.assertFalse(session.inspect({"action": "list_records", "args": {
                    "table": table, "cursor": None, "limit": 50}})["ok"])
            for path in (root / "public").iterdir():
                text = path.read_text()
                self.assertNotIn("source_book", text)
                self.assertNotIn("mutation_indexes", text)
            # Corrupting an exported original must be detected, not accepted as clean proof.
            original = source / "ledger.csv"
            original.write_text(original.read_text().replace(",AR,", ",UNKNOWN,", 1))
            with self.assertRaisesRegex(ValueError, "source_hash_mismatch"):
                check_source_book(root)

    def test_missing_transition_change_is_rejected_even_with_updated_hash(self):
        from tools.check_source_books import check_source_book
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "episode"
            generate_episode(17, {"config_version": "1.1", "issue_counts": {"payment_allocation": 1}}, root)
            provenance_path = root / "private/provenance.json"
            provenance = json.loads(provenance_path.read_bytes())
            transition_path = root / "private/source_transition.json"
            transition = json.loads(transition_path.read_bytes())
            transition["changes"].pop()
            transition_path.write_text(json.dumps(transition))
            provenance["source_book"]["transition_sha256"] = sha256(transition_path.read_bytes()).hexdigest()
            provenance_path.write_text(json.dumps(provenance))
            with self.assertRaisesRegex(ValueError, "transition_incomplete"):
                check_source_book(root)
