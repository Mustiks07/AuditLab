"""Publication refuses stale/partial CI evidence and mismatched draft assets."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import publish_candidate as publisher

SOURCE = 'a' * 40
RUN = 1234


def evidence():
    run = {'id': RUN, 'head_sha': SOURCE, 'repository': {'full_name': publisher.REPO},
           'head_repository': {'full_name': publisher.REPO}, 'path': '.github/workflows/verify.yml',
           'event': 'workflow_dispatch', 'status': 'completed', 'conclusion': 'success', 'run_attempt': 2}
    jobs = [{'name': name, 'run_id': RUN, 'head_sha': SOURCE, 'run_attempt': 2,
             'status': 'completed', 'conclusion': 'success'} for name in sorted(publisher.JOBS)]
    artifacts = [{'id': i + 1, 'name': name, 'expired': False, 'size_in_bytes': 100,
                  'workflow_run': {'id': RUN, 'head_sha': SOURCE}, 'digest': 'sha256:' + 'b' * 64}
                 for i, name in enumerate(sorted(publisher.artifact_names(SOURCE)))]
    return run, jobs, artifacts


class CandidateGuardTests(unittest.TestCase):
    def test_exact_source_successful_jobs_and_three_artifacts_are_accepted(self):
        run, jobs, artifacts = evidence()
        receipt = publisher.validate_ci(run, jobs, artifacts, SOURCE, RUN)
        self.assertEqual(receipt['run_attempt'], 2)
        self.assertEqual(set(receipt['artifacts']), publisher.artifact_names(SOURCE))

    def test_green_run_for_another_source_is_rejected(self):
        run, jobs, artifacts = evidence()
        run['head_sha'] = 'c' * 40
        with self.assertRaisesRegex(ValueError, 'revision differs'):
            publisher.validate_ci(run, jobs, artifacts, SOURCE, RUN)

    def test_failed_run_and_missing_failed_skipped_or_stale_jobs_are_rejected(self):
        mutations = [lambda run, jobs: run.update(conclusion='failure'),
                     lambda run, jobs: jobs.pop(),
                     lambda run, jobs: jobs[0].update(conclusion='failure'),
                     lambda run, jobs: jobs[0].update(conclusion='skipped'),
                     lambda run, jobs: jobs[0].update(run_attempt=1),
                     lambda run, jobs: jobs[0].update(head_sha='c' * 40),
                     lambda run, jobs: jobs.append(copy.deepcopy(jobs[0]))]
        for mutate in mutations:
            with self.subTest(mutation=mutations.index(mutate)):
                run, jobs, artifacts = evidence()
                mutate(run, jobs)
                with self.assertRaises(ValueError):
                    publisher.validate_ci(run, jobs, artifacts, SOURCE, RUN)

    def test_wrong_workflow_repository_or_pr_merge_ref_cannot_publish(self):
        for update in ({'path': '.github/workflows/unrelated.yml'}, {'event': 'pull_request'},
                       {'head_repository': {'full_name': 'someone/fork'}}):
            with self.subTest(update=update):
                run, jobs, artifacts = evidence()
                run.update(update)
                with self.assertRaises(ValueError):
                    publisher.validate_ci(run, jobs, artifacts, SOURCE, RUN)

    def test_expired_missing_duplicate_and_stale_artifacts_are_rejected(self):
        mutations = [lambda artifacts: artifacts.pop(),
                     lambda artifacts: artifacts[0].update(expired=True),
                     lambda artifacts: artifacts[0]['workflow_run'].update(head_sha='c' * 40),
                     lambda artifacts: artifacts.append(copy.deepcopy(artifacts[0])),
                     lambda artifacts: artifacts[0].pop('digest')]
        for mutate in mutations:
            with self.subTest(mutation=mutations.index(mutate)):
                run, jobs, artifacts = evidence()
                mutate(artifacts)
                with self.assertRaises(ValueError):
                    publisher.validate_ci(run, jobs, artifacts, SOURCE, RUN)

    def test_missing_oci_or_tag_preload_checks_cannot_claim_fresh_machine(self):
        images = {role: {'id': 'sha256:' + '1' * 64, 'oci_manifest_digest': 'sha256:' + '2' * 64,
                         'tag': role + ':accepted'} for role in ('policy', 'runtime', 'verifier')}
        preload = {'status': 'passed', 'images_present_before_load': {role: False for role in images},
                   'checked_identities': {role: {value: False for value in image.values()} for role, image in images.items()}}
        publisher.validate_preload(preload, images)
        for identity in images['policy'].values():
            with self.subTest(identity=identity):
                bad = copy.deepcopy(preload)
                bad['checked_identities']['policy'].pop(identity)
                with self.assertRaises(ValueError):
                    publisher.validate_preload(bad, images)
                bad = copy.deepcopy(preload)
                bad['checked_identities']['policy'][identity] = True
                with self.assertRaises(ValueError):
                    publisher.validate_preload(bad, images)

    def test_source_receipts_are_bound_to_actual_packaged_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = []
            for i in range(1, 21):
                name = f'internal-{i:02d}'
                snapshot = root / name
                hashes = {}
                for relative in [*(f'public/{file}' for file in publisher.PUBLIC_FILES),
                                 *(f'private/source_book/{file}' for file in publisher.PUBLIC_FILES),
                                 'private/source_transition.json', 'private/provenance.json', 'private/truth.json']:
                    path = snapshot / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(relative)
                    hashes[relative] = publisher.digest(path)
                receipt = {'status': 'passed', 'source_findings': 0,
                    'source_files_sha256': {file: hashes['private/source_book/' + file] for file in publisher.PUBLIC_FILES},
                    'target_files_sha256': {file: hashes['public/' + file] for file in publisher.PUBLIC_FILES},
                    'transition_sha256': hashes['private/source_transition.json'],
                    'provenance_sha256': hashes['private/provenance.json'], 'truth_sha256': hashes['private/truth.json']}
                rows.append({'id': name, 'status': 'passed', 'source_book': receipt, 'snapshot_sha256': hashes})
            self.assertEqual(len(publisher.validate_source_books(rows, root)), 20)
            (root / 'internal-01/private/source_book/ledger.csv').write_text('altered')
            with self.assertRaisesRegex(ValueError, 'packaged bytes'):
                publisher.validate_source_books(rows, root)

    def test_pagination_includes_releases_beyond_first_page(self):
        with patch.object(publisher, 'gh', return_value='[{"tag_name":"old"}]\n[{"tag_name":"new"}]\n'):
            self.assertEqual([row['tag_name'] for row in publisher.pages('endpoint')], ['old', 'new'])

    def test_existing_draft_blocks_new_release_without_tag_lookup(self):
        with patch.object(publisher, 'pages', return_value=[{'tag_name': 'v0.1.0-rc.3', 'draft': True}]), \
             patch.object(publisher.subprocess, 'run') as git:
            with self.assertRaisesRegex(ValueError, 'existing release'):
                publisher.require_unused_tag('v0.1.0-rc.3')
            git.assert_not_called()

    def test_asset_hash_mismatch_keeps_release_draft_and_uses_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dist = root / 'dist'
            dist.mkdir()
            (dist / 'payload.zip').write_bytes(b'accepted bytes')
            receipt = root / 'receipt.json'
            receipt.write_text('{}')
            report = {'workflow_run': 'https://example.test/ci', 'stores': {store: {'tests': 321} for store in publisher.STORES}}
            calls = []
            def api(endpoint, payload=None, method=None):
                calls.append((endpoint, method))
                if method == 'POST':
                    return {'id': 789, 'draft': True, 'tag_name': 'v0.1.0-rc.3'}
                return {'id': 789, 'draft': True, 'tag_name': 'v0.1.0-rc.3', 'target_commitish': SOURCE,
                        'assets': [{'name': 'payload.zip', 'state': 'uploaded', 'size': len(b'accepted bytes'),
                                    'digest': 'sha256:' + '0' * 64}]}
            with patch.object(publisher, 'require_merged_source'), patch.object(publisher, 'require_unused_tag'), \
                 patch.object(publisher, 'current_ci', return_value={}), \
                 patch.object(publisher, 'build_packages', return_value=(dist, report)), \
                 patch.object(publisher, 'api', side_effect=api), patch.object(publisher, 'gh'):
                with self.assertRaisesRegex(ValueError, 'uploaded asset differs'):
                    publisher.publish('v0.1.0-rc.3', SOURCE, RUN, root, receipt)
            self.assertIn((f'repos/{publisher.REPO}/releases/789', None), calls)
            self.assertFalse(any(method == 'PATCH' for _, method in calls))
            self.assertFalse(any('/tags/' in endpoint for endpoint, _ in calls))
