"""Behavioral prerequisites for S1; no inspection of implementation text."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import campaign_state as state
import campaign_fence as fence
import qualification_watchdog as watchdog


class FrozenCampaignTests(unittest.TestCase):
    def test_two_processes_cannot_hold_the_same_campaign(self):
        with tempfile.TemporaryDirectory() as temp:
            body = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from campaign_state import exclusive
try:
    with exclusive(Path(sys.argv[2])):
        Path(sys.argv[2], 'admitted').write_text('effect')
except OSError:
    sys.exit(75)
'''
            command = [sys.executable, '-c', body, str(SCRIPTS), temp]
            with state.exclusive(Path(temp)):
                denied = subprocess.run(command, capture_output=True, timeout=10)
                self.assertEqual(denied.returncode, 75)
                self.assertFalse((Path(temp) / 'admitted').exists())
            accepted = subprocess.run(command, capture_output=True, timeout=10)
            self.assertEqual(accepted.returncode, 0)
            self.assertEqual((Path(temp) / 'admitted').read_text(), 'effect')

    def test_renewal_latency_does_not_extend_authority(self):
        now = [10.0]
        channel = Mock(closed=False)
        channel.exit_status_ready.return_value = False
        def response():
            now[0] = 15.0
            return 'renewed\n'
        session = fence.Session(channel, Mock(readline=response), '1-' + 'a' * 32,
                                '/fixture', 10, lease=5, clock=lambda: now[0])
        session.renew(strict=True)
        self.assertEqual(session.lease_until, 15)
        self.assertFalse(session.alive())
        with self.assertRaises(fence.CampaignLost):
            session.renew(strict=True)
        channel.sendall.assert_called_once_with(b'renew\n')

    def test_round_budget_never_restarts_phase_budget(self):
        now = [100.0]
        with patch.object(watchdog.time, 'monotonic', side_effect=lambda: now[0]):
            guard = watchdog.Watchdog(10)
            now[0] = 109
            guard.round(1, 50)
            guard.round(2, 50)
            now[0] = 110
            guard.stopped = Mock()
            guard.stopped.wait.side_effect = [False, True]
            with patch.object(watchdog, 'diagnostic') as diagnostic, \
                    patch.object(watchdog, 'kill_descendants') as kill, \
                    patch.object(watchdog.os, '_exit') as leave:
                guard.watch()
            leave.assert_called_once_with(124)
            kill.assert_called_once()
            self.assertIn('phase watchdog expired', diagnostic.call_args.args[0])

    def test_fsync_precedes_replace_and_failure_preserves_previous_ledger(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'ledger.json'
            state.write_json(path, {'generation': 1})
            events = []
            replace = Path.replace
            def publish(source, target):
                self.assertEqual(events, ['fsync'])
                return replace(source, target)
            with patch.object(state.os, 'fsync', side_effect=lambda fd: events.append('fsync')), \
                    patch.object(Path, 'replace', publish):
                state.write_json(path, {'generation': 2})
            with patch.object(state.os, 'fsync', side_effect=OSError('fixture')):
                with self.assertRaises(OSError):
                    state.write_json(path, {'generation': 3})
            self.assertEqual(json.loads(path.read_text()), {'generation': 2})

    def test_crash_at_persistence_boundaries_keeps_complete_generation(self):
        for boundary, generation in [('before_fsync', 1), ('after_fsync', 1),
                                     ('before_replace', 1), ('after_replace', 2)]:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'ledger.json'
                state.write_json(path, {'generation': 1})
                body = '''
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import campaign_state as s
boundary = sys.argv[3]
fsync, replace = os.fsync, Path.replace
def sync(fd):
    if boundary == 'before_fsync': os._exit(73)
    fsync(fd)
    if boundary == 'after_fsync': os._exit(73)
def publish(source, target):
    if boundary == 'before_replace': os._exit(73)
    replace(source, target)
    if boundary == 'after_replace': os._exit(73)
os.fsync, Path.replace = sync, publish
s.write_json(Path(sys.argv[2]), {'generation': 2})
'''
                result = subprocess.run([sys.executable, '-c', body, str(SCRIPTS), str(path), boundary],
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 73)
                self.assertEqual(json.loads(path.read_text()), {'generation': generation})

    def test_process_crash_releases_lock_but_not_running_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            body = '''
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from campaign_state import Campaign, exclusive
with exclusive(Path(sys.argv[2])):
    Campaign(sys.argv[2], {'subject': 'fixture'}).phase('check', lambda p: os._exit(73))
'''
            child = subprocess.run([sys.executable, '-c', body, str(SCRIPTS), temp],
                                   capture_output=True, timeout=10)
            self.assertEqual(child.returncode, 73)
            with state.exclusive(Path(temp)):
                campaign = state.Campaign(temp, {'subject': 'fixture'})
                self.assertEqual(campaign.state['phases']['check'][0]['status'], 'RUNNING')
                action = Mock()
                with self.assertRaises(ValueError):
                    campaign.phase('check', action)
                action.assert_not_called()
                campaign.phase('check', lambda p: (p / 'result').write_text('ok'),
                               retry=True, reason='explicit crash recovery')
                self.assertEqual([a['status'] for a in campaign.state['phases']['check']],
                                 ['RUNNING', 'PASS'])

    def test_pass_inventory_rejects_added_removed_and_replaced_artifacts(self):
        for change in ('add', 'remove', 'replace'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                campaign = state.Campaign(temp, {'revision': 'fixture', 'target': 'offline'})
                folder = campaign.phase('check', lambda p: (p / 'result').write_bytes(b'original'))
                if change == 'add':
                    (folder / 'extra').write_bytes(b'extra')
                elif change == 'remove':
                    (folder / 'result').unlink()
                else:
                    (folder / 'result').write_bytes(b'replaced')
                action = Mock()
                with self.assertRaises(ValueError):
                    state.Campaign(temp, campaign.state['identity']).phase('check', action)
                action.assert_not_called()


if __name__ == '__main__':
    unittest.main()
