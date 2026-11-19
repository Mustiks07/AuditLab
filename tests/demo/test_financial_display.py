"""Money units, independent agreement, and the supported live-change boundary."""
import json
import io
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest

from auditlab.demo import derive, lab, service, standin
from auditlab.demo.__main__ import money
from auditlab.demo.paths import DemoPaths
from auditlab.demo.snapshot import Snapshot
from auditlab.demo.store import ExportStore
from auditlab.evaluator import score_submission
from auditlab.verifier import verify_snapshot

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / 'fixtures' / 'foundation'
FX = ROOT / 'auditlab' / 'demo' / 'scenarios' / 'FX01'


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


class CurrencyDisplay(unittest.TestCase):
    def test_minor_unit_exponents_zero_two_three_and_unknown(self):
        exponents = {'JPY': 0, 'KZT': 2, 'KWD': 3}
        self.assertEqual(money(12000, 'JPY', exponents), '12 000 JPY (12000 мин. ед.)')
        self.assertEqual(money(20000, 'KWD', exponents), '20,000 KWD (20000 мин. ед.)')
        self.assertEqual(money(-1, 'KWD', exponents), '−0,001 KWD (-1 мин. ед.)')
        self.assertEqual(money(2000000, 'KZT', exponents), '20 000,00 ₸ (2000000 мин. ед.)')
        self.assertIn('разрядность не указана', money(12000, 'JPY', {}))

    def test_real_jpy_and_kwd_exports_keep_units_through_cli_and_api(self):
        from auditlab.runner import make_env
        from auditlab.demo.__main__ import _print_run
        for currency, exponent in (("JPY", 0), ("KWD", 3)):
            source = next(p.parent.parent for p in sorted((ROOT / "datasets/open/dev").glob("*/private/truth.json"))
                          if any(i["currency"] == currency for i in load(p)["issues"]))
            findings = verify_snapshot(source / "public")
            manifest = load(source / "public/manifest.json")
            self.assertEqual(manifest["currency_exponents"][currency], exponent)
            with tempfile.TemporaryDirectory() as temp:
                exports = Path(temp) / "exports"
                exports.mkdir()
                exported = exports / "run"
                env = make_env("train")
                env.reset(0, {"snapshot_dir": str(source), "export_dir": str(exported), "policy_name": "display-regression"})
                env.step({"action": "submit", "args": {"episode_id": manifest["episode_id"], "findings": findings}})
                store = ExportStore([exports])
                run_id = load(exported / "run.json")["run_id"]
                detail = service.run_detail(store, run_id)
                self.assertEqual(detail["score"]["fp"], 0)
                amount = next(f["claimed_amount_minor"] for f in findings if f["currency"] == currency)
                output = io.StringIO()
                with redirect_stdout(output):
                    _print_run(detail)
                self.assertIn(money(amount, currency, manifest["currency_exponents"]), output.getvalue())
                key = next(k for k in detail["keys"]
                           if (Snapshot(source / "public").get(k).fields.get("currency") == currency))
                self.assertEqual(service.record_view(store, run_id, key)["currency_exponents"][currency], exponent)

    def test_currency_tampering_cannot_be_green_when_real_scorer_rejects(self):
        public = FIX / 'S02' / 'public'
        findings = verify_snapshot(public)
        truth = load(FIX / 'S02' / 'private' / 'truth.json')
        for changed_currency, expected in [('KZT', True), ('USD', False)]:
            finding = {**findings[0], 'currency': changed_currency}
            submission = {'episode_id': 'S02', 'findings': [finding]}
            score = score_submission(submission, truth, 4)
            view = service.findings_view(Snapshot(public), submission, score,
                                        {'engine': 'auditlab.verifier', 'findings': findings}, [])[0]
            self.assertEqual(view['console_agrees'], expected)
            self.assertEqual(view['verifier']['agrees'], expected)
            self.assertEqual(score['tp'], int(expected))
            self.assertEqual(view['verdict']['matched'], expected)

    def test_same_amount_for_wrong_entity_does_not_agree(self):
        public = FIX / 'S02' / 'public'
        finding = verify_snapshot(public)[0]
        wrong = {**finding, 'affected_ids': ['payment/P9', 'invoice/OTHER']}
        view = service.findings_view(Snapshot(public), {'findings': [wrong]}, None,
                                    {'findings': [finding]}, [])[0]
        self.assertFalse(view['console_agrees'])
        self.assertFalse(view['verifier']['agrees'])

    def test_arithmetic_agreement_is_not_evidence_acceptance(self):
        public = FIX / 'S02' / 'public'
        finding = verify_snapshot(public)[0]
        partial = {**finding, 'evidence_ids': ['payments/D9']}
        submission = {'episode_id': 'S02', 'findings': [partial]}
        score = score_submission(submission, load(FIX / 'S02' / 'private' / 'truth.json'), 4)
        view = service.findings_view(Snapshot(public), submission, score,
                                    {'findings': [finding]}, [])[0]
        self.assertTrue(view['console_agrees'])
        self.assertTrue(view['verifier']['agrees'])
        self.assertFalse(view['verdict']['matched'])
        self.assertTrue(view['uncited_operands'])

    def test_record_and_pair_views_publish_manifest_currency_exponents(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = DemoPaths(Path(temp)).ensure()
            ids = lab.build_samples(paths)
            store = ExportStore([paths.exports], paths.ops)
            self.assertEqual(service.record_view(store, ids[2], 'payments/D9')['currency_exponents'],
                             {'KZT': 2, 'USD': 2})
            pair = service.compare(store, ids[0], ids[1])
            self.assertEqual(pair['left']['currency_exponents'], {'KZT': 2})
            self.assertEqual(pair['right']['currency_exponents'], {'KZT': 2})

    def test_preview_uses_company_fixed_offset_not_source_timestamp_offset(self):
        self.assertEqual(derive.local_date('2026-11-14T20:01:00+00:00', 'UTC+05:00'), '2026-11-15')
        self.assertEqual(derive.local_date('2026-11-15T01:01:00+00:00', 'UTC-04:00'), '2026-11-14')
        with self.assertRaises(ValueError):
            derive.local_date('2026-11-15T01:01:00+00:00', 'UTC+05:99')


class CounterfactualScope(unittest.TestCase):
    def test_trusted_source_answer_required_before_publishing(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / 'new'
            with self.assertRaises(standin.ChangeRejected) as caught:
                standin.apply_change(FX / 'public', {'target': 'payments/D9', 'field': 'timestamp',
                                     'value': '2026-11-15T00:01:00+05:00'}, destination, 'NEW')
            self.assertEqual(caught.exception.code, 'unsupported_counterfactual')
            self.assertFalse(destination.exists())

    def test_unsupported_existing_error_is_not_dropped_from_fullsize_company(self):
        source = next(p.parent.parent for p in sorted((ROOT / 'datasets/open/train').glob('*/private/truth.json'))
                      if [i['type'] for i in load(p)['issues']] == ['duplicate_invoice'])
        truth = load(source / 'private/truth.json')
        payment = next(p for p in Snapshot(source / 'public').by_table('payments') if p.fields['currency'] == 'KZT')
        with tempfile.TemporaryDirectory() as temp:
            paths = DemoPaths(Path(temp)).ensure()
            exported = lab.execute(paths, source / 'public', truth,
                                   {'key': 'duplicate-regression', 'run_id': 'SOURCE'})
            before = {str(p.relative_to(paths.root)): p.read_bytes() for p in paths.root.rglob('*') if p.is_file()}
            with self.assertRaises(lab.LabError) as caught:
                lab.live_change(paths, exported['run_id'], {'target': payment.key, 'field': 'timestamp',
                                'value': '2026-11-20T17:01:00+05:00'}, None, 'reject-duplicate')
            self.assertEqual(caught.exception.code, 'unsupported_counterfactual')
            self.assertEqual(len(list(paths.exports.iterdir())), 1)
            self.assertEqual(load(lab._truth_path(paths, truth['episode_id'])), truth)
            for relative, data in before.items():
                self.assertEqual((paths.root / relative).read_bytes(), data)
            status = load(paths.ops / 'reject-duplicate.json')
            self.assertEqual(status['status'], 'failed')
            self.assertEqual([i['type'] for i in verify_snapshot(source / 'public')], ['duplicate_invoice'])
            # Even an empty auditor submission must not hide SQL's independent finding.
            submission_path = paths.exports / exported['run_id'] / 'submission.json'
            submission_path.write_text(json.dumps({'episode_id': truth['episode_id'], 'findings': []}))
            detail = service.run_detail(ExportStore([paths.exports], paths.ops), exported['run_id'])
            self.assertTrue(detail['empty_submission'])
            self.assertEqual([f['type'] for f in detail['verifier']['findings']], ['duplicate_invoice'])

    def test_cross_period_payment_or_recognition_journal_is_rejected(self):
        truth = load(FX / 'private/truth.json')
        with tempfile.TemporaryDirectory() as temp:
            cases = [('payments/D9', '2026-12-01T00:01:00+05:00'),
                     ('ledger/R1', '2026-11-02T00:01:00+05:00')]
            for target, value in cases:
                out = Path(temp) / target.replace('/', '-')
                with self.assertRaises(standin.ChangeRejected) as caught:
                    standin.apply_change(FX / 'public', {'target': target, 'field': 'timestamp', 'value': value},
                                         out, 'NEW', source_truth=truth)
                self.assertEqual(caught.exception.code, 'unsupported_counterfactual')
                self.assertFalse(out.exists())

    def test_mismatched_trusted_answer_does_not_authorize_partial_recalculation(self):
        truth = load(FIX / 'S02/private/truth.json')
        truth['issues'][0]['expected_amount_minor'] += 1
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(standin.ChangeRejected) as caught:
                standin.apply_change(FIX / 'S02/public', {'target': 'ledger/R7', 'field': 'timestamp',
                                     'value': '2026-11-30T23:59:00+05:00'}, Path(temp)/'new', 'NEW', source_truth=truth)
            self.assertEqual(caught.exception.code, 'unsupported_counterfactual')


class UnconfirmedOperandUnits(unittest.TestCase):
    """A copy that drops a ledger row shows that row as money in its currency (RC walkthrough finding)."""

    def test_dropped_ledger_operand_carries_minor_value_and_currency(self):
        snap = Snapshot(FIX / 'S02' / 'public')
        finding = {'type': 'payment_allocation', 'affected_ids': ['invoice/I4', 'payment/P9'],
                   'claimed_amount_minor': 2000000, 'currency': 'KZT', 'explanation': 'x',
                   'evidence_ids': ['payments/D9', 'ledger/R4', 'ledger/R6', 'policy/allocation']}
        [view] = service.findings_view(snap, {'episode_id': 'S02', 'findings': [finding]},
                                       {'tp': 0, 'fp': 1, 'fn': 1, 'reward': -1.0}, None, [])
        [entry] = service._unconfirmed(view)
        self.assertEqual((entry['key'], entry['value_minor'], entry['currency']), ('ledger/R5', 3000000, 'KZT'))


class ServeAddress(unittest.TestCase):
    def test_bind_all_prints_a_browser_address(self):
        from auditlab.demo.__main__ import browser_url
        self.assertEqual(browser_url('0.0.0.0', 8765), 'http://127.0.0.1:8765/')
        self.assertEqual(browser_url('::', 8765), 'http://127.0.0.1:8765/')
        self.assertEqual(browser_url('127.0.0.1', 9000), 'http://127.0.0.1:9000/')
