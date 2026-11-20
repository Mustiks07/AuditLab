"""Cancellation removes only containers owned by one offline invocation."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import offline
from auditlab.runner.isolation import docker_command
from test_offline_bundle import manifest_for


class OfflineLifecycleTests(unittest.TestCase):
    def launch(self, root, behavior, cleanup_error=None):
        manifest = manifest_for(root)
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            if command[1:2] == ['--host']:
                return subprocess.CompletedProcess(command, 0, json.dumps({'OSType': 'linux'}))
            if command[1:3] == ['image', 'inspect']:
                return subprocess.CompletedProcess(command, 0, json.dumps({'Id': command[3], 'Os': 'linux'}))
            if command[1] in ('create', 'start'):
                behavior(command[1])
            return subprocess.CompletedProcess(command, 0, '')
        with patch('offline.subprocess.run', side_effect=run), patch('offline.cleanup_launch', side_effect=cleanup_error, return_value={
                'status': 'passed', 'remaining_containers': 0}) as cleanup, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = offline.main(['verify', '--bundle', str(root), '--output-root', str(root / 'results')])
        report = json.loads(next((root / 'results').glob('*/launch.json')).read_text())
        return code, report, calls, cleanup

    def test_parent_sigint_and_sigterm_clean_up_and_record_interruption(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum), tempfile.TemporaryDirectory() as temporary:
                old_handler = signal.getsignal(signum)
                def interrupt(stage):
                    if stage == 'start':
                        os.kill(os.getpid(), signum)
                code, report, calls, cleanup = self.launch(Path(temporary), interrupt)
                self.assertEqual(code, 128 + signum)
                self.assertEqual(report['status'], 'interrupted')
                self.assertEqual(report['signal'], signum)
                self.assertEqual(report['cleanup']['remaining_containers'], 0)
                self.assertEqual(signal.getsignal(signum), old_handler)
                cleanup.assert_called_once()
                launch_id = report['launch_id']
                self.assertEqual(cleanup.call_args.args[0], launch_id)
                created = next(c for c in calls if c[1] == 'create')
                self.assertIn('auditlab-controller-' + launch_id, created)
                self.assertIn('org.auditlab.launch=' + launch_id, created)

    def test_interrupt_during_create_waits_for_create_then_never_starts(self):
        with tempfile.TemporaryDirectory() as temporary:
            completed_create = []
            def interrupt(stage):
                if stage == 'create':
                    os.kill(os.getpid(), signal.SIGINT)
                    completed_create.append(True)
            code, report, calls, cleanup = self.launch(Path(temporary), interrupt)
            self.assertEqual(code, 130)
            self.assertEqual(completed_create, [True])
            self.assertFalse(any(c[1] == 'start' for c in calls))
            cleanup.assert_called_once()

    def test_cleanup_uses_exact_launch_label_and_verifies_removal(self):
        calls, enumerations = [], 0
        own_id = 'a' * 12
        launch_id = '1' * 32
        def run(command, **kwargs):
            nonlocal enumerations
            calls.append(command)
            if command[1] == 'ps':
                enumerations += 1
                return subprocess.CompletedProcess(command, 0, own_id if enumerations == 1 else '')
            return subprocess.CompletedProcess(command, 0, '')
        with patch('offline.subprocess.run', side_effect=run):
            result = offline.cleanup_launch(launch_id, {})
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['removed_siblings'], [own_id])
        self.assertTrue(all('label=org.auditlab.launch=' + launch_id in c for c in calls if c[1] == 'ps'))
        self.assertEqual([c[3:] for c in calls if c[1] == 'rm'], [['auditlab-controller-' + launch_id], [own_id]])

    def test_unavailable_daemon_cannot_be_reported_as_clean(self):
        with patch('offline.subprocess.run', side_effect=[subprocess.CompletedProcess([], 1),
                subprocess.CalledProcessError(1, ['docker', 'ps'])]):
            with self.assertRaises(subprocess.CalledProcessError):
                offline.cleanup_launch('1' * 32, {})

    def test_failed_cleanup_changes_otherwise_successful_return_to_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            code, report, calls, cleanup = self.launch(Path(temporary), lambda stage: None,
                                                       OSError("daemon unavailable"))
            self.assertEqual(code, 1)
            self.assertEqual(report['status'], 'failed')
            self.assertEqual(report['cleanup']['status'], 'failed')
            self.assertIn('daemon unavailable', report['cleanup']['error'])

    def test_ownership_is_metadata_not_policy_environment(self):
        with patch.dict(os.environ, {'AUDITLAB_LAUNCH_ID': 'a' * 32}):
            command = docker_command('immutable-image')
        self.assertIn('org.auditlab.launch=' + 'a' * 32, command)
        self.assertNotIn('--env', command)
        self.assertFalse(any('AUDITLAB_LAUNCH_ID' in arg for arg in command))
        with patch.dict(os.environ, {'AUDITLAB_LAUNCH_ID': 'not a launch id'}), self.assertRaises(ValueError):
            docker_command('immutable-image')


@unittest.skipUnless(os.environ.get('AUDITLAB_POLICY_IMAGE'), 'real Docker image not configured')
class RealOfflineInterruptionTests(unittest.TestCase):
    def test_parent_only_signals_remove_controller_and_owned_sibling_preserving_other_launch(self):
        image = os.environ['AUDITLAB_POLICY_IMAGE']
        root = Path(__file__).resolve().parents[2]
        # Exercise real launcher/signal/daemon lifecycle; only archive loading is
        # stubbed because this test uses the preloaded CI image, not a release ZIP.
        script = r'''
import json, os, sys
from pathlib import Path
from unittest.mock import patch
import offline
image, out = sys.argv[1:]
manifest = {'source_revision': 'a'*40, 'images': {r: {'id': image} for r in ('runtime','policy','verifier')}}
real_run = offline.subprocess.run
def run(command, **kwargs):
    if command[1] == 'load':
        return __import__('subprocess').CompletedProcess(command, 0)
    return real_run(command, **kwargs)
def command(manifest, output, cpus, suite, gid, launch_id):
    return ['docker','run','--rm','--pull','never','--network','none',
            '--name','auditlab-controller-'+launch_id,'--label','org.auditlab.launch='+launch_id,
            '--entrypoint','python',image,'-c','import time; time.sleep(120)']
with patch('offline.verify_bundle', return_value=manifest), patch('offline.validate_image_archive'), \
     patch('offline.loaded_image', side_effect=lambda value, env: value), \
     patch('offline.runtime_command', side_effect=command), patch('offline.subprocess.run', side_effect=run):
    raise SystemExit(offline.main(['verify','--bundle',out,'--output-root',out]))
'''
        foreign = 'auditlab-test-foreign-' + __import__('uuid').uuid4().hex
        subprocess.run(['docker', 'run', '-d', '--rm', '--pull=never', '--name', foreign,
                        '--label', 'org.auditlab.launch=' + 'f' * 32, '--entrypoint', 'python',
                        image, '-c', 'import time; time.sleep(120)'], check=True, capture_output=True)
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                with self.subTest(signal=signum), tempfile.TemporaryDirectory() as temporary:
                    process = subprocess.Popen([sys.executable, '-c', script, image, temporary], cwd=root,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                               start_new_session=True)
                    launch_id = None
                    try:
                        deadline = time.monotonic() + 20
                        while time.monotonic() < deadline:
                            reports = list(Path(temporary).glob('*/launch.json'))
                            if reports:
                                report = json.loads(reports[0].read_text())
                                launch_id = report.get('launch_id')
                            if launch_id:
                                found = subprocess.run(['docker', 'ps', '-q', '--filter',
                                    'label=org.auditlab.launch=' + launch_id], capture_output=True, text=True, check=True)
                                if found.stdout.strip():
                                    break
                            if process.poll() is not None:
                                self.fail('launcher exited early: ' + str(process.communicate()))
                            time.sleep(.05)
                        else:
                            self.fail('controller did not start')
                        subprocess.run(['docker', 'run', '-d', '--rm', '--pull=never',
                            '--label', 'org.auditlab.launch=' + launch_id, '--entrypoint', 'python', image,
                            '-c', 'import time; time.sleep(120)'], capture_output=True, check=True)
                        os.kill(process.pid, signum)  # deliberately NOT killpg / terminal Ctrl+C
                        stdout, stderr = process.communicate(timeout=20)
                        self.assertEqual(process.returncode, 128 + signum, stdout + stderr)
                        report = json.loads(reports[0].read_text())
                        self.assertEqual(report['status'], 'interrupted')
                        self.assertEqual(report['cleanup']['status'], 'passed')
                        found = subprocess.run(['docker', 'ps', '-aq', '--filter',
                            'label=org.auditlab.launch=' + launch_id], capture_output=True, text=True, check=True)
                        self.assertEqual(found.stdout.strip(), '')
                        foreign_state = subprocess.run(['docker', 'inspect', '-f', '{{.State.Running}}', foreign],
                            capture_output=True, text=True, check=True)
                        self.assertEqual(foreign_state.stdout.strip(), 'true')
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.communicate()
                        if launch_id:
                            offline.cleanup_launch(launch_id, offline.local_docker_environment())
        finally:
            subprocess.run(['docker', 'rm', '-f', foreign], capture_output=True)
