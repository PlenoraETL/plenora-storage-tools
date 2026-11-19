"""Independent observations of the shared model, each with a killed mutation.

Discovered by the existing Linux/Windows product-quality unittest jobs.
The POSIX cases are also mandatory in target/SEMPL-2-STORAGE-VM.sh.
"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import campaign_model as model
import campaign_shell as shell
import campaign_state
import qualification_watchdog
import release_campaign
import run_vm_campaign


class ModelTests(unittest.TestCase):
    def test_phase_results_are_owned_by_the_transition_table(self):
        def observe():
            with tempfile.TemporaryDirectory() as temporary:
                campaign = model.Campaign(temporary, {'subject': 'fixture'})
                campaign.phase('probe', lambda p: (p / 'result').write_text('ok'))
                self.assertEqual(campaign.state['phases']['probe'][0]['status'], 'PASS')
                with self.assertRaises(ValueError):
                    campaign.phase('probe', lambda p: None, retry=True, reason='again')
        observe()
        with patch.dict(model.TRANSITIONS, {('phase', 'RUNNING', 'success'): 'FAIL'}):
            with self.assertRaises(AssertionError):
                observe()

    def test_invalid_transition_has_no_destination(self):
        def observe():
            for state, event in [('PASS', 'retry'), ('FAIL', 'success'), ('NEW', 'success')]:
                with self.assertRaises(ValueError):
                    model.transition('phase', state, event)
        observe()
        with patch.dict(model.TRANSITIONS, {('phase', 'PASS', 'retry'): 'RUNNING'}):
            with self.assertRaises(AssertionError):
                observe()

    def test_round_and_polling_do_not_restart_the_outer_budget(self):
        def observe():
            now = [100.0]
            with patch.object(model.time, 'monotonic', side_effect=lambda: now[0]):
                deadline = model.Deadline(10)
                watchdog = qualification_watchdog.Watchdog(10)
                now[0] = 109
                watchdog.round(1, 50)
                now[0] = 110
                self.assertTrue(deadline.expired())
                self.assertEqual(watchdog.phase_deadline, 110)
                self.assertEqual(watchdog.round_deadline, 159)
        observe()
        original = model.PhaseBudget.round
        def restart(self, number, seconds):
            original(self, number, seconds)
            self.phase_deadline = self.round_deadline
        with patch.object(model.PhaseBudget, 'round', restart):
            with self.assertRaises(AssertionError):
                observe()

    def test_shell_writer_executes_the_same_persistence_barrier(self):
        # Execute the embedded Python adapter without requiring POSIX on Windows.
        def observe():
            command = shlex.split(shell.persist_command('destination'))
            with tempfile.TemporaryDirectory() as temporary:
                target = Path(temporary) / 'state'
                payload = b'complete\x00generation\n'
                result = subprocess.run([sys.executable, *command[1:-1], str(target)],
                                        input=payload, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(target.read_bytes(), payload)
                self.assertFalse(target.with_name('state.pending').exists())
        observe()
        # A producer that merely stages bytes cannot satisfy publication.
        source = shell.inspect.getsource(model.atomic_write)
        with patch.object(shell.inspect, 'getsource', return_value=source.replace(
                'temporary.replace(path)', 'path.write_bytes(b"wrong generation")')):
            with self.assertRaises(AssertionError):
                observe()

    def test_python_entrypoints_use_the_model_writer(self):
        def observe():
            events = []
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'ledger.json'
                with patch.object(model.os, 'fsync', side_effect=lambda fd: events.append('sync')):
                    campaign_state.write_json(path, {'generation': 1})
                self.assertEqual(events, ['sync'])
                self.assertEqual(json.loads(path.read_text()), {'generation': 1})
        observe()
        def bypass(path, writer, **options):
            with path.open('w') as stream:
                writer(stream)
        with patch.object(model, 'atomic_write', bypass):
            with self.assertRaises(AssertionError):
                observe()

    def test_transport_and_measurement_defaults_remain_the_reviewed_budgets(self):
        def observe():
            self.assertEqual((model.LEASE_SECONDS, model.LEASE_MARGIN, model.RENEW_SECONDS,
                              model.KILL_SECONDS, model.POLL_SECONDS, model.STALL_SECONDS),
                             (60, 30, 10, 10, 1, 9))
            self.assertEqual((run_vm_campaign.TRANSFER_ROUND_TIMEOUT,
                              run_vm_campaign.TRANSFER_PHASE_TIMEOUT), (10800, 14400))
            self.assertEqual(release_campaign.Remote.stream.__defaults__, (600,))
        observe()
        with patch.object(model, 'LEASE_MARGIN', 1):
            with self.assertRaises(AssertionError):
                observe()


@unittest.skipUnless(sys.platform == 'linux', 'real POSIX shell and modes; run on VM')
class ModelPosixTests(unittest.TestCase):
    def test_shell_admission_executes_the_transition_table(self):
        def observe():
            body = '\n'.join([*shell.transition_lines('admission'),
                              'for event in lock wait inspect advance record share confirm renew close; do',
                              'advance "$event"', 'done', 'printf "%s" "$state"'])
            result = subprocess.run(['bash', '-c', body], capture_output=True)
            self.assertEqual((result.returncode, result.stdout), (0, b'CLOSED'))
        observe()
        with patch.dict(model.TRANSITIONS, {('admission', 'SHARED', 'confirm'): 'CLOSED'}):
            with self.assertRaises(AssertionError):
                observe()

    def test_python_and_shell_create_from_one_profile(self):
        def observe():
            for mask in (0o000, 0o002, 0o022, 0o077):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    previous = os.umask(mask)
                    try:
                        with model.creation_profile('fixture_file'):
                            (root / 'python').write_text('fixture')
                        self.assertEqual(os.umask(mask), mask)
                        subprocess.run(['bash', '-c', shell.shell_profile('fixture_file')
                                        + '; printf fixture > "$1"', 'probe', str(root / 'shell')],
                                       check=True)
                        self.assertEqual([(root / name).stat().st_mode & 0o777
                                          for name in ('python', 'shell')], [0o644, 0o644])
                    finally:
                        os.umask(previous)
        observe()
        with patch.dict(model.PERMISSIONS, {'fixture_file': model.Permission(0o644, 0o000)}):
            with self.assertRaises(AssertionError):
                observe()
