"""Test the validated source before the profile 1.1 mutation phase."""

import json
import unittest
from collections import Counter
from copy import deepcopy
from itertools import product
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from auditlab.generator import generate_episode
from auditlab.generator.core import Company


class SourceBookTests(unittest.TestCase):
    def validated_books(self, root, config):
        books = []
        validate = Company.validate
        apply = Company.apply_mutations

        def record_validation(company, expected_counts, policy):
            validate(company, expected_counts, policy)
            books.append((company, Counter(expected_counts)))

        def check_source_before_applying(company):
            self.assertEqual(books, [(company, Counter())])
            self.assertEqual(company.issues, [])
            before = deepcopy((company.invoices, company.payments, company.ledger, company.fx))
            changed = apply(company)
            self.assertIsNot(changed, company)
            self.assertIsNot(changed.ledger, company.ledger)
            self.assertEqual((company.invoices, company.payments, company.ledger, company.fx), before)
            return changed

        with patch.object(Company, "validate", record_validation), patch.object(
            Company, "apply_mutations", check_source_before_applying,
        ):
            generate_episode(51, {"config_version": "1.1", **config}, root)
        (source, _), (changed, counts) = books
        self.assertEqual(source.issues, [])
        self.assertEqual(Counter(issue["type"] for issue in changed.issues), counts)
        self.assertEqual(changed.invoices, source.invoices)
        self.assertEqual(changed.fx, source.fx)
        provenance = json.loads((root / "private" / "provenance.json").read_bytes())
        self.assertEqual(provenance["source_book_validation"], {
            "ledger_rows": len(source.ledger),
            "document_count": len(source.invoices) + len(source.payments),
            "issue_count": 0,
        })
        manifest = json.loads((root / "public" / "manifest.json").read_bytes())
        self.assertNotIn("source_book_validation", manifest)
        self.assertEqual(manifest["ledger_rows"], len(changed.ledger))
        return source, changed

    def test_source_book_precedes_all_eight_interaction_states(self):
        interactions = (("allocation_fx_same_payment", "payment_allocation"),
                        ("cutoff_fx_same_invoice", "period_cutoff"))
        with TemporaryDirectory() as temporary:
            for interaction, issue in interactions:
                baseline = None
                for first, fx in product((0, 1), repeat=2):
                    with self.subTest(interaction=interaction, first=first, fx=fx):
                        output = Path(temporary) / f"{interaction}-{first}{fx}"
                        source, changed = self.validated_books(output, {
                            "clean_distractors": [], "interaction": interaction,
                            "issue_counts": {issue: first, "fx_conversion": fx},
                        })
                        if baseline is None:
                            baseline = (source.invoices, source.payments, source.ledger, source.fx)
                        self.assertEqual((source.invoices, source.payments, source.ledger, source.fx), baseline)
                        self.assertEqual(len(changed.issues), first + fx)

    def test_source_book_contains_clean_replacement_for_each_defect(self):
        issue_types = ("duplicate_invoice", "payment_allocation", "fx_conversion", "period_cutoff")
        with TemporaryDirectory() as temporary:
            source, changed = self.validated_books(Path(temporary) / "all-issues", {
                "issue_counts": dict.fromkeys(issue_types, 1),
                "company_size": {"ledger_rows_target": 300, "documents_target": 80},
            })
        self.assertEqual(Counter(issue["type"] for issue in changed.issues), Counter(issue_types))
        self.assertEqual((len(source.ledger), len(changed.ledger), len(source.invoices) + len(source.payments)),
                         (298, 300, 80))
        source_rows = {row["row_id"]: row for row in source.ledger}
        changed_rows = {row["row_id"]: row for row in changed.ledger}
        added = [changed_rows[key] for key in changed_rows.keys() - source_rows.keys()]
        self.assertCountEqual([row["account"] for row in added], ["AR", "REVENUE"])
        self.assertFalse(source_rows.keys() - changed_rows.keys())
        allocation = next(issue for issue in changed.issues if issue["type"] == "payment_allocation")
        payment_id = next(key.split("/")[1] for key in allocation["affected_ids"] if key.startswith("payment/"))
        reduced = [row for key, row in source_rows.items()
                   if row["payment_id"] == payment_id and row != changed_rows[key]]
        self.assertCountEqual([row["account"] for row in reduced], ["UNAPPLIED", "AR"])
        for row in reduced:
            field = "debit_minor" if row["debit_minor"] else "credit_minor"
            self.assertEqual(row[field] - changed_rows[row["row_id"]][field], allocation["expected_amount_minor"])

    def test_balanced_but_incorrect_source_is_rejected_before_application(self):
        post = Company.post

        def wrong_valuation(company, timestamp, currency, amount, debit, credit, *args, **kwargs):
            if (debit, credit) == ("FX_VALUATION", "FX_OFFSET"):
                amount += 1
            return post(company, timestamp, currency, amount, debit, credit, *args, **kwargs)

        with TemporaryDirectory() as temporary, patch.object(Company, "post", wrong_valuation), patch.object(
            Company, "apply_mutations", side_effect=AssertionError("applied mutations to an invalid source"),
        ) as apply:
            root = Path(temporary) / "invalid"
            with self.assertRaisesRegex(RuntimeError, "accounting_disagrees_with_truth"):
                generate_episode(51, {"config_version": "1.1", "clean_distractors": [],
                    "issue_counts": {"fx_conversion": 1}}, root)
            apply.assert_not_called()
            self.assertFalse(root.exists())
