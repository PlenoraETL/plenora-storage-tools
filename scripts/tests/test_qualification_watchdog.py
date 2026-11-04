import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from campaign_state import Campaign, logged
from qualification_watchdog import positive_seconds


class WatchdogTests(unittest.TestCase):
    def child(self, body, timeout=8):
        return subprocess.run([sys.executable, '-u', '-c',
                               'import sys; sys.path.insert(0, ' + repr(str(SCRIPTS)) + ')\n' + body],
                              capture_output=True, text=True, timeout=timeout)

    def test_main_exception_with_stuck_worker_exits_and_redacts(self):
        result = self.child('''
import threading
from qualification_watchdog import WorkerPool, entrypoint
def main():
    with WorkerPool(1) as pool:
        pool.submit(threading.Event().wait)
        raise RuntimeError('secret callback payload')
entrypoint(main)
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('main', result.stderr)
        self.assertIn('RuntimeError', result.stderr)
        self.assertNotIn('secret', result.stderr)

    def test_unexpected_main_system_exit_zero_is_failure(self):
        result = self.child('''
from qualification_watchdog import entrypoint
def main():
    raise SystemExit(0)
entrypoint(main)
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('main exception: SystemExit', result.stderr)

    def test_worker_base_exception_is_logged_even_with_stuck_sibling(self):
        result = self.child('''
import threading
from concurrent.futures import as_completed
from qualification_watchdog import WorkerPool, entrypoint
def fail():
    raise SystemExit(0)
def main():
    with WorkerPool(2) as pool:
        tasks = [pool.submit(threading.Event().wait), pool.submit(fail)]
        for task in as_completed(tasks):
            task.result()
entrypoint(main)
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('worker exception: SystemExit', result.stderr)
        self.assertNotIn('secret', result.stderr)

    def test_round_and_phase_watchdogs_terminate_blocked_main(self):
        for kind in ('round', 'phase'):
            with self.subTest(kind=kind):
                result = self.child('''
import threading
from qualification_watchdog import Watchdog, WorkerPool, entrypoint
def main():
    with Watchdog(PHASE) as watchdog, WorkerPool(1) as pool:
        watchdog.round(35, ROUND)
        pool.submit(threading.Event().wait).result()
entrypoint(main)
'''.replace('PHASE', '.2' if kind == 'phase' else '5')
                    .replace('ROUND', '.2' if kind == 'round' else '5'))
                self.assertEqual(result.returncode, 124)
                self.assertIn(kind + ' watchdog expired', result.stderr)

    def test_completed_main_cannot_be_held_alive_by_a_stray_thread(self):
        result = self.child('''
import threading
from qualification_watchdog import entrypoint
def main():
    threading.Thread(target=threading.Event().wait).start()
entrypoint(main)
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('main ended with live workers', result.stderr)

    def test_real_transfer_loop_slow_round_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.child('''
import threading
import qualify_transfers as q
q.sys.platform = 'linux'
q.new_report = lambda *a: {'status': 'RUNNING', 'results': [], 'rss_limit_bytes': 1000}
q.payload = lambda *a: 'hash'
q.roundtrip = lambda *a: threading.Event().wait()
sys.argv = ['qualify_transfers', '--bytes', '1', '--providers', 'local', '--round-timeout', '.2',
            '--phase-timeout', '5', '--output', OUTPUT]
q.entrypoint(q.main)
'''.replace('OUTPUT', repr(str(Path(temporary) / 'report.json'))))
            self.assertEqual(result.returncode, 124)
            self.assertIn('round watchdog expired (round 1)', result.stderr)
            self.assertNotEqual(json.loads((Path(temporary) / 'report.json').read_text())['status'], 'PASS')

    def test_real_transfer_loop_completes_48_rounds(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.child('''
from pathlib import Path
import qualify_transfers as q
q.sys.platform = 'linux'
q.new_report = lambda binary, args: {'status': 'RUNNING', 'results': [], 'rss_limit_bytes': 1000,
    'campaign_id': str(binary), 'rounds': args.rounds + args.warmup_rounds, 'warmup_rounds': args.warmup_rounds}
q.payload = lambda *a: 'hash'
q.roundtrip = lambda *a: {'status': 'PASS'}
folder = Path(OUTPUT)
sys.argv = ['qualify_transfers', '--bytes', '1', '--providers', 'local', '--workers', '4',
            '--rounds', '32', '--warmup-rounds', '16', '--baseline-binary', str(folder / 'binary'),
            '--baseline-output', str(folder / 'b.json'), '--output', str(folder / 'c.json')]
q.entrypoint(q.main)
'''.replace('OUTPUT', repr(temporary)), timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in ('b.json', 'c.json'):
                report = json.loads((Path(temporary) / name).read_text())
                self.assertEqual(report['status'], 'PASS')
                self.assertEqual(len(report['results']), 48 * 4)
                self.assertEqual({row['round'] for row in report['results']}, set(range(48)))
                self.assertEqual(len(report['paired_measurement']['order']), 48)

    def test_parent_timeout_records_failure_and_stderr(self):
        with tempfile.TemporaryDirectory() as temporary:
            campaign = Campaign(Path(temporary), {'test': True})
            with self.assertRaisesRegex(RuntimeError, 'watchdog'):
                campaign.phase('performance-ab', lambda folder: logged(
                    [sys.executable, '-u', '-c',
                     "import sys,time; print('stderr marker', file=sys.stderr); time.sleep(60)"],
                    folder, timeout=.5))
            folder = Path(temporary) / 'performance-ab/1'
            state = json.loads((folder / 'command.log.status.json').read_text())
            self.assertEqual(state['status'], 'TIMEOUT')
            self.assertNotEqual(state['returncode'], 0)
            self.assertIn('stderr marker', (folder / 'command.log').read_text())
            self.assertEqual(campaign.state['phases']['performance-ab'][0]['status'], 'FAIL')

    def test_parent_records_abrupt_child_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with self.assertRaises(RuntimeError):
                logged([sys.executable, '-c', 'import os; os._exit(7)'], folder, timeout=2)
            state = json.loads((folder / 'command.log.status.json').read_text())
            self.assertEqual((state['status'], state['returncode']), ('FAIL', 7))

    def test_timeouts_are_finite_positive(self):
        for value in ('nan', 'inf', '-inf', '0', '-1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                positive_seconds(value)

    @unittest.skipUnless(sys.platform == 'linux', 'Linux process tree and separate sessions')
    def test_parent_timeout_kills_separate_session_descendant(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], start_new_session=True); print(p.pid,flush=True); time.sleep(60)"
            with self.assertRaises(RuntimeError):
                logged([sys.executable, '-u', '-c', code], folder, timeout=1)
            pid = int((folder / 'command.log').read_text().splitlines()[0])
            stat = Path(f'/proc/{pid}/stat')
            if stat.exists():
                self.assertEqual(stat.read_text().rsplit(')', 1)[1].split()[0], 'Z')
