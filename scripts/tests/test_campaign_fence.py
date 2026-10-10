"""Admission, epoch, lease and process tree of the release campaign (campaign_fence).

The protocol tests run the real admission command, preparation wrapper and
scripts with bash and flock, and are skipped where those are missing
(Windows); the Linux CI runs them.
"""
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campaign_state import Campaign, digest
import campaign_fence
import release_campaign
import run_vm_campaign

ROOT = Path(__file__).resolve().parents[2]
EPOCH = '7-' + '0' * 32
LINUX = sys.platform != 'win32' and bool(shutil.which('bash')) and bool(shutil.which('flock'))


class FakeChannel:
    def __init__(self, code=0, finished=False, clock=None):
        self.closed, self.code, self.finished, self.sent = False, code, finished, []
        self.clock, self.sent_at = clock, []

    def close(self):
        self.closed = True

    def exit_status_ready(self):
        return self.finished

    def recv_exit_status(self):
        return self.code

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, data):
        if self.closed:
            raise OSError('closed')
        self.sent.append(data)
        if self.clock:
            self.sent_at.append(self.clock())


class FakeReader:
    def __init__(self, replies):
        self.replies = list(replies)

    def readline(self):
        return self.replies.pop(0) if self.replies else ''


class FakeSession:
    """An admission that holds the VM until `lose()` is called."""

    def __init__(self, epoch=EPOCH, directory='/srv/q/.campaign', renewals=None):
        self.epoch, self.directory, self.lost, self.renewals = epoch, directory, False, renewals

    def alive(self):
        return not self.lost

    def renew(self, strict=False):
        if self.renewals is not None:
            if self.renewals == 0:
                raise campaign_fence.CampaignLost('lost')
            self.renewals -= 1
        if self.lost:
            raise campaign_fence.CampaignLost('lost')

    def check(self, remote):
        self.renew()

    def lose(self):
        self.lost = True


class FakeRemote:
    """A VM whose fixture preparation finishes at once with a scripted
    outcome; files are kept in memory by remote path."""

    def __init__(self, root, *, code='0', check='PASS', unowned=False):
        self.root, self.code, self.check, self.unowned = root, code, check, unowned
        self.files, self.commands = {}, []

    def write(self, remote, text):
        self.files[remote] = text

    def download(self, remote, path):
        path.write_text(self.files[remote])

    def run(self, command):
        self.commands.append(command)
        if ' campaign-owned ' in command:
            if self.unowned:
                raise RuntimeError('dedicated VM command failed')
            return 'owned'
        if 'umask 077 && mkdir -p' in command:
            return ''
        if '(nohup bash .fixtures/' in command:
            label_nonce = command.split('(nohup bash .fixtures/', 1)[1].split('-run.sh', 1)[0]
            signal = f'{self.root}/.fixtures/signals/{label_nonce}'
            self.files[signal + '.exit'] = self.code
            self.files[signal + '.log'] = 'log'
            if self.code != str(campaign_fence.LOCK_HELD):
                self.files[f'{self.root}/.fixtures/diagnostics/{label_nonce}.tar.gz'] = 'archive'
            self.files[signal + '-check.json'] = json.dumps({'status': self.check})
            return 'started'
        path = command.split('if test -f ', 1)[1].split(';', 1)[0]
        present = f'{self.root}/{path}' in self.files
        if 'then cat ' in command:
            return self.files[f'{self.root}/{path}'] if present else 'running'
        return 'yes' if present else 'no'


class PreparationTests(unittest.TestCase):
    ROOT = '/srv/root'

    def reset(self, remote, folder, session=None):
        return release_campaign.reset_fixtures(remote, self.ROOT, 'storage-q-abc', 'vm.invalid', folder,
                                               session or FakeSession(), poll=0, deadline=60)

    def test_a_reset_is_recorded_with_its_nonce_and_epoch_only_after_every_fixture_answers(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            remote = FakeRemote(self.ROOT)
            # A successful signal left by an earlier execution is never read.
            remote.files[f'{self.ROOT}/.fixtures/signals/fixture-reset-{"0" * 32}.exit'] = '0'
            nonce = self.reset(remote, folder)
            record = json.loads((folder / 'fixture-reset.json').read_text())
            self.assertEqual((record['nonce'], record['epoch'], record['recreated']), (nonce, EPOCH, True))
            self.assertEqual(json.loads((folder / 'fixture-reset-nonce.json').read_text())['nonce'], nonce)
            self.assertTrue((folder / 'pre-reset.tar.gz').is_file())
            self.assertIn(f'CAMPAIGN_EPOCH={EPOCH}', remote.files[f'{self.ROOT}/.fixtures/fixture-reset-{nonce}-run.sh'])

    def test_every_refusal_is_typed_and_records_no_reset(self):
        cases = ((str(campaign_fence.LOCK_HELD), 'PASS', campaign_fence.CampaignBusy),
                 (str(release_campaign.RUNNER_ACTIVE), 'PASS', campaign_fence.CampaignBusy),
                 (str(campaign_fence.FENCED), 'PASS', campaign_fence.CampaignFenced),
                 ('66', 'PASS', ValueError), ('0', 'FAIL', ValueError))
        for code, check, error in cases:
            with self.subTest(code=code, check=check), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                with self.assertRaises(error) as raised:
                    self.reset(FakeRemote(self.ROOT, code=code, check=check), folder)
                if error is ValueError:
                    self.assertNotIsInstance(raised.exception, campaign_fence.CampaignBusy)
                self.assertFalse((folder / 'fixture-reset.json').exists())

    def test_state_that_is_not_exclusively_ours_stops_before_any_script_is_written(self):
        remote = FakeRemote(self.ROOT, unowned=True)
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(ValueError):
            self.reset(remote, Path(temporary))
        self.assertEqual(remote.files, {})

    def test_a_lost_admission_starts_no_preparation(self):
        session = FakeSession()
        session.lose()
        remote = FakeRemote(self.ROOT)
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(campaign_fence.CampaignLost):
            self.reset(remote, Path(temporary), session)
        self.assertEqual(remote.files, {})

    def test_the_wrapper_keeps_lock_errors_apart_from_contention(self):
        _, wrapper = release_campaign.fixture_scripts(self.ROOT, 'p', 'h', 'prepare', 'n0nce', reset=False,
                                                      directory='/srv/q/.campaign', epoch=EPOCH)
        held = campaign_fence.LOCK_HELD
        self.assertIn('exec 9</srv/q/.campaign/lock', wrapper)
        self.assertIn(f'flock -n -E {held} -s 9 || exit $?', wrapper)
        self.assertIn(f'flock -n -E {held} -x 8 || exit $?', wrapper)
        self.assertIn(f'if [ "$code" -eq {held} ]; then exit 1; fi', wrapper)
        self.assertNotIn(f'|| exit {held}', wrapper)
        self.assertTrue(wrapper.rstrip().endswith('exit "$code"'))

    def test_every_preparation_recreates_and_the_reset_fences_every_change(self):
        for reset in (False, True):
            script, _ = release_campaign.fixture_scripts(self.ROOT, 'storage-q-abc', 'vm.invalid', 'l', 'n0nce',
                                                         reset=reset, directory='/srv/q/.campaign', epoch=EPOCH)
            steps = script.splitlines()
            recreate = steps.index('export PLENORA_FIXTURE_RECREATE=1')
            self.assertLess(recreate, steps.index('bash scripts/prepare-fixtures.sh'))
            self.assertNotIn('|| true', script)
        fences = [i for i, line in enumerate(steps) if line == 'fence']
        runner = next(i for i, line in enumerate(steps) if "grep -q '^storage-q-abc-campaign-'" in line)
        archive = next(i for i, line in enumerate(steps) if 'logs --no-color' in line)
        memory = next(i for i, line in enumerate(steps) if 'check_memory.py' in line)
        check = next(i for i, line in enumerate(steps) if 'check_fixtures.py' in line)
        states = [i for i, line in enumerate(steps) if line.startswith('printf') and 'fixture-state' in line]
        self.assertIn('in-progress', steps[states[0]])
        self.assertIn('reset', steps[states[-1]])
        self.assertLess(fences[0], runner)
        self.assertTrue(any(runner < f < states[0] for f in fences))
        self.assertTrue(any(memory < f < recreate for f in fences))
        self.assertTrue(any(check < f < states[-1] for f in fences))
        self.assertLess(states[0], archive)
        self.assertLess(archive, memory)

    def test_the_runner_measures_only_after_this_attempts_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with self.assertRaises(ValueError):
                run_vm_campaign.check_fixture_state(output, 'n0nce')
            for state in ({'nonce': 'other', 'kind': 'reset'}, {'nonce': 'n0nce', 'kind': 'prepare'}):
                (output / 'fixture-state.json').write_text(json.dumps(state))
                with self.assertRaises(ValueError):
                    run_vm_campaign.check_fixture_state(output, 'n0nce')
            (output / 'fixture-state.json').write_text(json.dumps({'nonce': 'n0nce', 'kind': 'reset'}))
            run_vm_campaign.check_fixture_state(output, 'n0nce')


class SessionTests(unittest.TestCase):
    """The controller side of the admission, with fake channels and clock."""

    class Remote:
        def __init__(self, first, code=0, content=EPOCH + '\n', replies=(), finished=None):
            self.first, self.code, self.content, self.replies, self.channel = first, code, content, replies, None
            # A refused admission has ended by the time its channel closes.
            self.finished = not first.startswith(f'locked {EPOCH} ') if finished is None else finished

        def hold(self, command, timeout):
            self.timeout = timeout
            self.channel = FakeChannel(self.code, finished=self.finished)
            return self.channel, FakeReader(self.replies), self.first

        def run(self, command):
            # Remote.run strips its output; the command frames the file.
            return ('x' + self.content + 'x').strip()

    def test_an_admission_yields_its_epoch_and_renews_its_lease_at_every_check(self):
        now = [100.0]
        remote = self.Remote(f'locked {EPOCH} /srv/q/.campaign', replies=['renewed\n'] * 2)
        with campaign_fence.admission(remote, '/srv/q', clock=lambda: now[0]) as session:
            self.assertEqual((session.epoch, session.directory), (EPOCH, '/srv/q/.campaign'))
            self.assertEqual(remote.channel.timeout, campaign_fence.RENEW_SECONDS)
            now[0] = 150.0
            session.check(remote)
            self.assertEqual(session.lease_until, 150.0 + campaign_fence.LEASE_SECONDS)
            remote.content = '8-' + '1' * 32 + '\n'
            with self.assertRaises(campaign_fence.CampaignFenced):
                session.check(remote)
            self.assertEqual(remote.channel.sent, [b'renew\n'] * 2)
            self.assertEqual(remote.timeout, campaign_fence.ADMISSION_SECONDS)
        self.assertTrue(remote.channel.closed)

    def test_the_epoch_is_compared_byte_for_byte(self):
        for content in (EPOCH, EPOCH + '\n\n', ' ' + EPOCH + '\n', EPOCH + ' \n', EPOCH + '\r\n',
                        EPOCH + '\n\0'):
            with self.subTest(content=content):
                remote = self.Remote(f'locked {EPOCH} /srv/q/.campaign', content=content, replies=['renewed\n'])
                with campaign_fence.admission(remote, '/srv/q') as session:
                    with self.assertRaises(campaign_fence.CampaignFenced):
                        session.check(remote)

    def test_a_strict_renewal_after_the_end_of_the_lease_sends_nothing(self):
        now = [0.0]
        remote = self.Remote(f'locked {EPOCH} /srv/q/.campaign', replies=['renewed\n'] * 2)
        with campaign_fence.admission(remote, '/srv/q', clock=lambda: now[0]) as session:
            now[0] = campaign_fence.LEASE_SECONDS
            with self.assertRaises(campaign_fence.CampaignLost):
                session.renew(strict=True)
            self.assertEqual(remote.channel.sent, [])
            session.renew()
            self.assertEqual(session.lease_until, 2 * campaign_fence.LEASE_SECONDS)

    def test_the_lease_ends_on_the_controller_clock_without_renewal(self):
        now = [0.0]
        remote = self.Remote(f'locked {EPOCH} /srv/q/.campaign')
        with campaign_fence.admission(remote, '/srv/q', clock=lambda: now[0]) as session:
            now[0] = campaign_fence.LEASE_SECONDS - 0.1
            self.assertTrue(session.alive())
            now[0] = campaign_fence.LEASE_SECONDS
            self.assertFalse(session.alive())

    def test_a_failed_renewal_or_a_finished_channel_is_a_lost_admission(self):
        for reply in ('', 'locked\n'):
            with self.subTest(reply=reply):
                remote = self.Remote(f'locked {EPOCH} /srv/q/.campaign', replies=[reply])
                with campaign_fence.admission(remote, '/srv/q') as session:
                    with self.assertRaises(campaign_fence.CampaignLost):
                        session.check(remote)
        remote = self.Remote(f'locked {EPOCH} /srv/q/.campaign', replies=['renewed\n'])
        with campaign_fence.admission(remote, '/srv/q') as session:
            remote.channel.finished = True
            self.assertFalse(session.alive())
            with self.assertRaises(campaign_fence.CampaignLost):
                session.renew()

    def test_contention_is_busy_and_any_other_failure_is_reported_as_such(self):
        with self.assertRaises(campaign_fence.CampaignBusy):
            with campaign_fence.admission(self.Remote('', campaign_fence.LOCK_HELD), '/srv/q'):
                self.fail('admitted')
        for first, code in (('', 1), ('', 66), ('locked 7 /d', 1), ('locked 07-' + '0' * 32 + ' /d', 1)):
            with self.subTest(first=first, code=code), self.assertRaises(RuntimeError) as failure:
                with campaign_fence.admission(self.Remote(first, code), '/srv/q'):
                    self.fail('admitted')
            self.assertNotIsInstance(failure.exception, campaign_fence.CampaignBusy)

    def test_an_admission_that_neither_starts_nor_ends_is_an_error_in_bounded_time(self):
        with patch.object(campaign_fence, 'EXIT_SECONDS', 0.2):
            with self.assertRaises(RuntimeError) as failure:
                with campaign_fence.admission(self.Remote('', finished=False), '/srv/q'):
                    self.fail('admitted')
        self.assertNotIsInstance(failure.exception, campaign_fence.CampaignBusy)

    def test_a_busy_vm_exits_with_the_lock_code(self):
        def busy():
            raise campaign_fence.CampaignBusy('held')
        self.assertEqual(release_campaign.entrypoint(busy), campaign_fence.LOCK_HELD)
        self.assertEqual(release_campaign.entrypoint(lambda: None), 0)
        for error in (campaign_fence.CampaignFenced('epoch'), campaign_fence.CampaignLost('lease'),
                      RuntimeError('lock command failed')):
            def failing(error=error):
                raise error
            with self.assertRaises(type(error)):
                release_campaign.entrypoint(failing)


# A child that starts a grandchild appending to a file every 50 ms, then
# sleeps, or exits at once with `exit` as second argument.
CHILD = """import os, subprocess, sys, time
subprocess.Popen([sys.executable, '-c', '''
import sys, time
for _ in range(1200):
    with open(sys.argv[1], 'a') as stream:
        stream.write('.')
    time.sleep(0.05)
''', sys.argv[1]])
if sys.argv[2] == 'exit':
    while not os.path.exists(sys.argv[1]):
        time.sleep(0.01)
    raise SystemExit(0)
time.sleep(60)
"""


class ProcessTreeTests(unittest.TestCase):
    """Real processes with a real grandchild, on every platform."""

    def run_child(self, session, mode, **options):
        folder = Path(self.temporary.name)
        beat = folder / 'beat'
        started = time.monotonic()
        error = None
        try:
            campaign_fence.supervised([sys.executable, '-c', CHILD, str(beat), mode], folder / 'command.log',
                                      session, poll=0.05, **options)
        except Exception as raised:  # the caller asserts on it
            error = raised
        return beat, error, time.monotonic() - started

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temporary.cleanup()

    def assert_stopped(self, beat):
        size = beat.stat().st_size
        time.sleep(0.5)
        self.assertEqual(beat.stat().st_size, size, 'a grandchild survived')

    def lose_when_beating(self, session, beat):
        def watch():
            limit = time.monotonic() + 30
            while time.monotonic() < limit and not (beat.exists() and beat.stat().st_size > 2):
                time.sleep(0.02)
            session.lose()
        import threading
        threading.Thread(target=watch, daemon=True).start()

    def test_losing_the_admission_kills_the_whole_tree(self):
        session = FakeSession()
        self.lose_when_beating(session, Path(self.temporary.name) / 'beat')
        beat, error, elapsed = self.run_child(session, 'sleep')
        self.assertIsInstance(error, campaign_fence.CampaignLost)
        self.assertLess(elapsed, 30)
        self.assert_stopped(beat)

    def test_a_stall_past_the_lease_renews_nothing_and_kills_the_whole_tree(self):
        import threading
        now = [0.0]
        channel = FakeChannel(clock=lambda: now[0])
        session = campaign_fence.Session(channel, FakeReader(['renewed\n'] * 1000), EPOCH, '/srv/q/.campaign',
                                         0.0, clock=lambda: now[0])
        beat = Path(self.temporary.name) / 'beat'
        stalled = campaign_fence.LEASE_SECONDS + 1

        def stall():
            wait_for(lambda: beat.exists() and beat.stat().st_size > 2, 30)
            # The supervisor wakes up after the end of its lease.
            now[0] = stalled
        threading.Thread(target=stall, daemon=True).start()
        _, error, _ = self.run_child(session, 'sleep', renew_every=0.0)
        self.assertIsInstance(error, campaign_fence.CampaignLost)
        self.assertTrue(channel.sent_at)
        self.assertTrue(all(sent < stalled for sent in channel.sent_at), 'renewed after the end of the lease')
        self.assert_stopped(beat)

    def test_a_failed_renewal_kills_the_whole_tree(self):
        session = FakeSession(renewals=1)
        beat = Path(self.temporary.name) / 'beat'
        _, error, _ = self.run_child(session, 'sleep', renew_every=1.0)
        self.assertIsInstance(error, campaign_fence.CampaignLost)
        if beat.exists():
            self.assert_stopped(beat)

    def test_a_finished_command_leaves_no_descendant(self):
        beat, error, _ = self.run_child(FakeSession(), 'exit')
        self.assertIsNone(error)
        self.assert_stopped(beat)

    def test_a_failing_command_reports_its_own_failure(self):
        log = Path(self.temporary.name) / 'command.log'
        with self.assertRaises(RuntimeError) as failure:
            campaign_fence.supervised([sys.executable, '-c', 'raise SystemExit(3)'], log, FakeSession(), poll=0.05)
        self.assertNotIsInstance(failure.exception, campaign_fence.CampaignLost)

    def test_no_command_starts_without_a_renewed_lease(self):
        session = FakeSession(renewals=0)
        beat, error, _ = self.run_child(session, 'sleep')
        self.assertIsInstance(error, campaign_fence.CampaignLost)
        self.assertFalse(beat.exists())


@unittest.skipUnless(sys.platform == 'win32', 'Win32 job object')
class JobTests(unittest.TestCase):
    """Failures of the Win32 calls are errors, never taken for success."""

    class Kernel:
        def __init__(self, real, **overrides):
            self.real, self.overrides = real, overrides

        def __getattr__(self, name):
            return self.overrides.get(name) or getattr(self.real, name)

    @staticmethod
    def failing(code):
        import ctypes

        def call(*arguments):
            ctypes.set_last_error(code)
            return 0
        return call

    def test_a_failed_close_keeps_the_handle_and_raises(self):
        job = campaign_fence._Job()
        real = job.kernel
        handle = job.handle
        job.kernel = self.Kernel(real, CloseHandle=self.failing(6))
        with self.assertRaises(OSError):
            job.close()
        self.assertEqual(job.handle, handle)
        job.kernel = real
        job.close()
        self.assertIsNone(job.handle)

    def test_thread_enumeration_ends_only_on_no_more_files(self):
        import ctypes
        job = campaign_fence._Job()
        try:
            class Entry(ctypes.Structure):
                _fields_ = [('th32OwnerProcessID', ctypes.c_ulong), ('th32ThreadID', ctypes.c_ulong)]

            def first(snapshot, entry):
                return 1
            real = job.kernel
            for code, expected in ((campaign_fence.ERROR_NO_MORE_FILES, None), (5, OSError)):
                with self.subTest(code=code):
                    job.kernel = self.Kernel(real, Thread32First=first, Thread32Next=self.failing(code))
                    threads = job.threads(None, Entry())
                    self.assertEqual(next(threads), (0, 0))
                    if expected:
                        with self.assertRaises(expected):
                            next(threads)
                    else:
                        self.assertEqual(list(threads), [])
                    job.kernel = real
        finally:
            job.close()

    def test_a_process_tree_lives_in_the_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            with open(Path(temporary) / 'log', 'wb') as stream:
                tree = campaign_fence.ProcessTree([sys.executable, '-c', 'import time; time.sleep(30)'], stdout=stream)
                try:
                    self.assertIsNotNone(tree.job)
                    self.assertEqual(tree.job.active(), 1)
                    tree.terminate()
                    self.assertEqual(tree.job.active(), 0)
                finally:
                    tree.close()


class InputTests(unittest.TestCase):
    """The runner measures only the inputs the controller uploaded for its epoch."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.inputs = Path(self.temporary.name) / 'inputs'
        for name, text in (('baseline/plenora-storage', 'old'), ('compose.campaign.json', '{}'),
                           ('dist/3.0.0/x/plenora-storage', 'new')):
            (self.inputs / name).parent.mkdir(parents=True, exist_ok=True)
            (self.inputs / name).write_text(text)
        self.expected = release_campaign.expected_inputs(
            {name: self.inputs / name for name in ('baseline/plenora-storage', 'compose.campaign.json',
                                                   'dist/3.0.0/x/plenora-storage')})
        self.baseline = self.inputs / 'baseline/plenora-storage'

    def tearDown(self):
        self.temporary.cleanup()

    def test_only_the_exact_inputs_pass(self):
        run_vm_campaign.check_inputs(self.inputs, self.expected, self.baseline)
        (self.inputs / 'extra').write_text('x')
        with self.assertRaises(ValueError):
            run_vm_campaign.check_inputs(self.inputs, self.expected, self.baseline)
        (self.inputs / 'extra').unlink()
        self.baseline.write_text('other controller')
        with self.assertRaises(ValueError):
            run_vm_campaign.check_inputs(self.inputs, self.expected, self.baseline)
        with self.assertRaises(ValueError):
            run_vm_campaign.check_inputs(self.inputs, {**self.expected, 'missing': 'f' * 64}, self.baseline)

    def test_an_input_replaced_during_a_phase_voids_it(self):
        fence = run_vm_campaign.input_fence(lambda: None, self.inputs, self.expected, self.baseline)
        with tempfile.TemporaryDirectory() as temporary:
            campaign = Campaign(Path(temporary), {'subject': 'same'})

            def measured(path):
                (path / 'report.json').write_text('measured')
                self.baseline.write_text('uploaded late by another controller')
            with self.assertRaises(ValueError):
                run_vm_campaign.fenced_phase(campaign, fence, 'performance-ab', measured)
            self.assertEqual(campaign.state['phases']['performance-ab'][-1]['status'], 'FAIL')

    def test_measured_bytes_come_from_private_copies(self):
        with tempfile.TemporaryDirectory() as root:
            folder = self.inputs / 'dist/3.0.0/x'
            private, candidate, baseline, copies = run_vm_campaign.private_inputs(
                folder, self.baseline, self.inputs, self.expected, root=Path(root))
            self.assertEqual(set(copies), {'baseline/plenora-storage', 'dist/3.0.0/x/plenora-storage'})
            self.assertTrue(private.is_relative_to(Path(root)))
            # Replaced on the host during a measurement, then restored.
            self.baseline.write_text('swapped')
            (folder / 'plenora-storage').write_text('swapped')
            self.assertEqual((baseline.read_text(), (candidate / 'plenora-storage').read_text()), ('old', 'new'))
            run_vm_campaign.check_private(copies, self.expected)
            self.baseline.write_text('old')
            (folder / 'plenora-storage').write_text('new')
            # A change that reaches a private copy is detected at the next fence.
            baseline.write_text('changed')
            with self.assertRaises(ValueError):
                run_vm_campaign.check_private(copies, self.expected)

    @unittest.skipIf(sys.platform == 'win32', 'the runner holds a flock')
    def test_the_runner_measures_the_private_copies_and_removes_them(self):
        campaign_dir = Path(self.temporary.name) / '.campaign'
        campaign_dir.mkdir()
        (campaign_dir / 'lock').write_text('')
        (campaign_dir / 'epoch').write_text(EPOCH + '\n')
        seen = {}

        def measure(folder, baseline, private, *arguments):
            fence = arguments[-3]
            seen.update(folder=folder, baseline=baseline, private=private)
            self.assertEqual((folder / 'plenora-storage').read_text(), 'new')
            self.baseline.write_text('swapped on the host')
            self.assertEqual(baseline.read_text(), 'old')
            with self.assertRaises(ValueError):
                fence()
            self.baseline.write_text('old')
            fence()
        with patch.object(run_vm_campaign, 'measure', measure):
            run_vm_campaign.run(self.inputs / 'dist/3.0.0/x', self.baseline, Path(self.temporary.name) / 'out', [],
                                None, [], 'n0nce', campaign_dir, EPOCH, self.inputs, self.expected,
                                Path(self.temporary.name) / 'fixture-state')
        self.assertTrue(seen['folder'].is_relative_to(seen['private']))
        self.assertTrue(seen['baseline'].is_relative_to(seen['private']))
        self.assertFalse(seen['private'].is_relative_to(self.inputs))
        self.assertFalse(seen['private'].exists())

    def test_inputs_already_replaced_when_copied_stop_the_run(self):
        self.baseline.write_text('swapped before the copy')
        with tempfile.TemporaryDirectory() as root, self.assertRaises(ValueError):
            run_vm_campaign.private_inputs(self.inputs / 'dist/3.0.0/x', self.baseline, self.inputs, self.expected,
                                           root=Path(root))

    def test_selected_evidence_must_be_what_its_phase_recorded(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            campaign = Campaign(output / 'ledger', {'subject': 'same'})
            folder = campaign.phase('soak', lambda path: (path / 'report.json').write_text('measured'))
            sources = {'gates/soak/report.json': (folder, folder / 'report.json')}
            files = run_vm_campaign.select_evidence(campaign, output / 'first', sources)
            self.assertEqual(files, {'gates/soak/report.json': digest(folder / 'report.json')})
            (folder / 'report.json').write_text('rewritten after the phase passed')
            with self.assertRaises(ValueError):
                run_vm_campaign.select_evidence(campaign, output / 'second', sources)
            with self.assertRaises(ValueError):
                run_vm_campaign.select_evidence(campaign, output / 'third',
                                                {'gates/x.json': (folder, folder / 'unrecorded.json')})


@unittest.skipUnless(LINUX, 'runs bash')
class PrivateRootTests(unittest.TestCase):
    """The VM root is used only when no other user can write it, or anything above it."""

    def setUp(self):
        # The walk stops at a directory the test controls: the directories
        # above it belong to the machine running the tests.
        self.top = Path(tempfile.mkdtemp())
        self.base = self.top / 'base'
        self.base.mkdir(mode=0o700)

    def tearDown(self):
        self.base.chmod(0o700)
        shutil.rmtree(self.top)

    def check(self, root):
        return subprocess.run(campaign_fence.private_root_command(str(root), str(self.top)), shell=True,
                              capture_output=True, text=True)

    def test_every_campaign_checks_up_to_the_filesystem_root(self):
        self.assertTrue(campaign_fence.private_root_command('/srv/q').endswith(' /srv/q /'))
        outside = subprocess.run(campaign_fence.private_root_command(str(self.base), str(self.base / 'elsewhere')),
                                 shell=True, capture_output=True, text=True)
        self.assertEqual(outside.returncode, 1)

    def test_a_new_root_is_created_private(self):
        result = self.check(self.base / 'root')
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'private'))
        self.assertEqual((self.base / 'root').stat().st_mode & 0o777, 0o700)

    def test_a_root_others_could_write_or_reach_by_a_link_is_refused(self):
        root = self.base / 'root'
        root.mkdir(mode=0o700)
        root.chmod(0o755)
        self.assertEqual(self.check(root).returncode, 1)
        root.chmod(0o700)
        self.base.chmod(0o777)
        self.assertEqual(self.check(root).returncode, 1)
        self.base.chmod(0o700)
        (self.base / 'link').symlink_to(root)
        self.assertEqual(self.check(self.base / 'link').returncode, 1)
        # Only the canonical path is accepted, so the one checked is the one used.
        self.assertEqual(self.check(f'{self.base}/./root').returncode, 1)
        self.assertEqual(self.check(root).returncode, 0)

    def test_missing_levels_are_private_from_the_start_whatever_the_umask(self):
        # The window between creating a level and restricting it does not
        # exist: each level is created with mode 700 even under umask 000.
        root = self.base / 'a' / 'b' / 'root'
        command = 'umask 000 && ' + campaign_fence.private_root_command(str(root), str(self.top))
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'private'))
        for level in (self.base / 'a', self.base / 'a' / 'b', root):
            self.assertEqual(level.stat().st_mode & 0o777, 0o700)

    def test_an_existing_level_is_checked_before_anything_below_it_and_never_changed(self):
        # A level placed by someone else before the check: open to others,
        # a link, or a file. Nothing is created below it and it stays as it is.
        cases = []
        open_level = self.base / 'open'
        open_level.mkdir()
        open_level.chmod(0o777)
        cases.append((open_level, 0o777))
        elsewhere = self.top / 'elsewhere'
        elsewhere.mkdir(mode=0o700)
        link = self.base / 'link'
        link.symlink_to(elsewhere)
        cases.append((link, None))
        collision = self.base / 'file'
        collision.write_text('not a directory')
        cases.append((collision, None))
        for level, mode in cases:
            with self.subTest(level=level.name):
                self.assertEqual(self.check(level / 'root').returncode, 1)
                self.assertFalse(os.path.lexists(level / 'root'))
                if mode is not None:
                    self.assertEqual(level.stat().st_mode & 0o777, mode)
        self.assertEqual(list(elsewhere.iterdir()), [])
        open_level.chmod(0o700)

    def test_a_path_with_a_control_or_unexpected_character_is_refused_before_anything(self):
        # A newline would end a line reader early and leave the levels after
        # it unchecked: every character outside the allowed set is refused.
        for name in ('a\nb', 'a b', 'a\tb', 'a$b', 'a\\b'):
            with self.subTest(name=name):
                self.assertEqual(self.check(f'{self.base}/{name}/root').returncode, 1)
        self.assertEqual(list(self.base.iterdir()), [])
        for root in ('/srv/a\nb/campaign', '/srv/a b/campaign', '/srv/\u00e9/campaign'):
            with self.subTest(root=root), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'campaign.json'
                path.write_text(json.dumps({'vm_root': root, 'candidate_run': '1', 'ci_run': '2'}))
                with self.assertRaises(ValueError):
                    release_campaign.configuration(path)

    def test_a_listing_that_fails_or_is_not_a_mode_string_is_a_refusal(self):
        bin_folder = self.top / 'bin'
        bin_folder.mkdir()
        for body in ('exit 2', 'echo unexpected; exit 0'):
            with self.subTest(body=body):
                (bin_folder / 'ls').write_text('#!/bin/sh\n' + body + '\n')
                (bin_folder / 'ls').chmod(0o755)
                command = (f'PATH={bin_folder}:$PATH ' +
                           campaign_fence.private_root_command(str(self.base / 'root'), str(self.top)))
                result = subprocess.run(command, shell=True, capture_output=True, text=True)
                self.assertEqual((result.returncode, result.stdout.strip()), (1, ''))

    def test_a_root_of_another_user_or_open_to_others_is_refused_unchanged(self):
        root = self.base / 'root'
        root.mkdir()
        root.chmod(0o755)
        self.assertEqual(self.check(root).returncode, 1)
        self.assertEqual(root.stat().st_mode & 0o777, 0o755)
        if os.geteuid() != 0:
            # A directory of another user (root) where the VM root should be.
            self.assertEqual(subprocess.run(campaign_fence.private_root_command('/usr'), shell=True,
                                            capture_output=True).returncode, 1)

    @unittest.skipUnless(shutil.which('setfacl'), 'needs setfacl')
    def test_a_root_with_an_access_control_list_is_refused(self):
        root = self.base / 'root'
        root.mkdir(mode=0o700)
        if subprocess.run(['setfacl', '-m', 'u:nobody:r', str(root)]).returncode:
            self.skipTest('the filesystem has no access control lists')
        self.assertEqual(self.check(root).returncode, 1)


@unittest.skipUnless(LINUX, 'runs bash')
class OwnedStateTests(unittest.TestCase):
    """Campaign state is used only if it is exclusively this user's, checked as itself."""

    def test_links_open_or_foreign_state_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / 'state').write_text('x')
            (folder / 'state').chmod(0o600)
            (folder / 'directory').mkdir(mode=0o700)

            def owned(*paths):
                return subprocess.run(campaign_fence.owned_command([str(path) for path in paths]), shell=True,
                                      capture_output=True, text=True)
            self.assertEqual(owned(folder / 'state', folder / 'directory').stdout.strip(), 'owned')
            (folder / 'link').symlink_to(folder / 'state')
            (folder / 'open').write_text('x')
            (folder / 'open').chmod(0o666)
            refused = [folder / 'link', folder / 'open', folder / 'missing']
            if os.geteuid() != 0:
                refused.append(Path('/usr'))
            for path in refused:
                with self.subTest(path=path.name):
                    self.assertEqual(owned(folder / 'state', path).returncode, 1)


class PrivateRootControllerTests(unittest.TestCase):
    def test_only_a_confirmed_private_root_is_used(self):
        class Remote:
            def __init__(self, answer):
                self.answer = answer

            def run(self, command):
                if isinstance(self.answer, Exception):
                    raise self.answer
                return self.answer
        campaign_fence.check_private_root(Remote('private'), '/srv/q')
        for answer in (RuntimeError('dedicated VM command failed'), ''):
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                campaign_fence.check_private_root(Remote(answer), '/srv/q')


@unittest.skipUnless(LINUX and shutil.which('git') and shutil.which('sha256sum'), 'runs bash, git and sha256sum')
class RunnerBootstrapTests(unittest.TestCase):
    """The runner starts only from its own verified checkout and fixture files."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        repository = root / 'repository'
        (repository / 'scripts').mkdir(parents=True)
        (repository / 'scripts/runner.py').write_text('verified source\n')
        (repository / '.gitignore').write_text('/.fixtures/\n')
        git = ['git', '-C', str(repository), '-c', 'user.name=t', '-c', 'user.email=t@t.invalid']
        subprocess.run(['git', 'init', '-q', str(repository)], check=True)
        subprocess.run([*git, 'add', '.'], check=True)
        subprocess.run([*git, 'commit', '-q', '-m', 'source'], check=True)
        self.revision = subprocess.check_output([*git, 'rev-parse', 'HEAD'], text=True).strip()
        self.inputs = root / 'inputs'
        (self.inputs / 'fixtures').mkdir(parents=True)
        subprocess.run([*git, 'bundle', 'create', '-q', str(self.inputs / 'source.bundle'), 'HEAD'], check=True)
        (self.inputs / 'fixtures/ca.crt').write_text('certificate\n')
        self.digests = {'bundle': digest(self.inputs / 'source.bundle'),
                        'ca.crt': digest(self.inputs / 'fixtures/ca.crt')}
        self.work = root / 'work'

    def tearDown(self):
        self.temporary.cleanup()

    def start(self, *, bundle=None, revision=None, fixture=None):
        lines = release_campaign.runner_bootstrap(str(self.work), str(self.inputs), bundle or self.digests['bundle'],
                                                  revision or self.revision,
                                                  {'ca.crt': fixture or self.digests['ca.crt']})
        script = '\n'.join([*lines, 'cd "$work/source"', 'cat scripts/runner.py .fixtures/ca.crt'])
        return subprocess.run(['bash', '-c', script], capture_output=True, text=True)

    def test_the_runner_starts_from_its_verified_copy(self):
        result = self.start()
        self.assertEqual((result.returncode, result.stdout), (0, 'verified source\ncertificate\n'))

    def test_any_difference_stops_the_container_before_the_runner(self):
        for options in ({'bundle': '0' * 64}, {'revision': '0' * 40}, {'fixture': '0' * 64}):
            with self.subTest(options=options):
                shutil.rmtree(self.work, ignore_errors=True)
                result = self.start(**options)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('verified source', result.stdout)

    def test_a_copy_already_present_is_never_reused(self):
        self.work.mkdir()
        self.assertNotEqual(self.start().returncode, 0)


class StreamTests(unittest.TestCase):
    """Remote.stream: the local file appears only for a complete, successful transfer."""

    class Output:
        def __init__(self, chunks, code, failure=None):
            self.chunks, self.failure = list(chunks), failure
            self.channel = type('Channel', (), {'recv_exit_status': lambda channel: code})()

        def read(self, size=-1):
            if self.chunks:
                return self.chunks.pop(0)
            if self.failure:
                raise self.failure
            return b''

    def remote(self, output, error=None):
        class Client:
            def exec_command(client, command, timeout):
                if error:
                    raise error
                return None, output, self.Output([], 0)
        remote = release_campaign.Remote.__new__(release_campaign.Remote)
        remote.client = Client()
        return remote

    def test_only_a_complete_successful_transfer_leaves_a_file(self):
        import socket
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'runner-output.tar'
            self.remote(self.Output([b'whole', b' output'], 0)).stream('docker cp c:/x -', path)
            self.assertEqual(path.read_bytes(), b'whole output')
            path.unlink()
            cases = {
                # `docker cp` or `docker logs` failing on the VM.
                'command failed': (self.Output([b'partial'], 1), None),
                # The connection drops: the channel ends without exit status.
                'cut short': (self.Output([b'partial'], -1), None),
                'connection lost': (self.Output([b'partial'], 0, socket.timeout('timed out')), None),
                'not started': (None, OSError('connection reset')),
            }
            for name, (output, error) in cases.items():
                with self.subTest(case=name):
                    with self.assertRaises(RuntimeError):
                        self.remote(output, error).stream('docker logs c', path)
                    self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_no_partial_file_survives_a_stopped_or_failed_transfer(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'runner.log'
            # Left by a controller stopped half way: never kept by the next transfer.
            path.with_name('runner.log.partial').write_bytes(b'stale')
            self.remote(self.Output([b'fresh'], 0)).stream('docker logs c', path)
            self.assertEqual(sorted(item.name for item in Path(temporary).iterdir()), ['runner.log'])
            path.unlink()
            with patch.object(Path, 'replace', side_effect=OSError('rename failed')):
                with self.assertRaises(RuntimeError):
                    self.remote(self.Output([b'fresh'], 0)).stream('docker logs c', path)
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_a_truncated_export_is_never_taken_for_evidence(self):
        import io
        import tarfile
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode='w') as stream:
                for name in ('output/selected/report.json', 'output/campaign.json'):
                    data = b'{}' * 4096
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    stream.addfile(info, io.BytesIO(data))
            (folder / 'runner-output.tar').write_bytes(buffer.getvalue()[:9000])
            with self.assertRaises(Exception):
                release_campaign.extract_runner_output(folder / 'runner-output.tar', folder / 'extracted')


class RunnerOutputTests(unittest.TestCase):
    """The controller reads the runner's evidence only from the exported archive."""

    def archive(self, folder, members):
        import io
        import tarfile
        path = folder / 'runner-output.tar'
        with tarfile.open(path, 'w') as stream:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                stream.addfile(info, io.BytesIO(data))
        return path

    def test_an_export_without_ledger_or_escaping_the_attempt_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            good = self.archive(folder, {'output/campaign.json': b'{}', 'output/selected/report.json': b'{}'})
            self.assertEqual(release_campaign.extract_runner_output(good, folder / 'a'), folder / 'a/output')
            with self.assertRaises(ValueError):
                release_campaign.extract_runner_output(self.archive(folder, {'output/selected/report.json': b'{}'}),
                                                       folder / 'b')
            with self.assertRaises(Exception):
                release_campaign.extract_runner_output(self.archive(folder, {'../escape': b'x'}), folder / 'c')
            self.assertFalse((folder / 'escape').exists())

    @unittest.skipIf(sys.platform == 'win32', 'the private root is the Linux container one')
    def test_the_runner_refuses_a_checkout_or_output_outside_its_container(self):
        private = run_vm_campaign.PRIVATE_ROOT
        run_vm_campaign.require_private(private / 'plenora-runner/source', private / 'plenora-runner/output')
        for root, output in ((ROOT, private / 'out'), (private / 'source', ROOT / '.fixtures/campaign')):
            with self.subTest(root=root, output=output), self.assertRaises(ValueError):
                run_vm_campaign.require_private(root, output)


class SelectedEvidenceTests(unittest.TestCase):
    """The controller seals only the inventoried evidence of this attempt, of its own binaries."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.linux = self.folder / 'linux'
        self.linux.mkdir()
        (self.linux / 'plenora-storage').write_text('candidate')
        self.candidate = digest(self.linux / 'plenora-storage')
        (self.linux / 'release-manifest.json').write_text(json.dumps(
            {'artifacts': [{'name': 'plenora-storage', 'sha256': self.candidate}]}))
        self.identity = {'revision': 'r' * 40, 'baseline_sha256': 'b' * 64}
        self.expected = {'baseline/plenora-storage': 'b' * 64}

    def tearDown(self):
        self.temporary.cleanup()

    def result(self, *, epoch=EPOCH, nonce='n0nce', baseline_sha='b' * 64, inputs=None, report_baseline='b' * 64,
               ledger_epoch=EPOCH):
        """A downloaded attempt: selected evidence, its report and the runner ledger."""
        result = self.folder / 'result'
        shutil.rmtree(result, ignore_errors=True)
        selected = result / 'selected'
        for name, value in (('gates/performance/baseline.json', {'binary_sha256': report_baseline}),
                            ('gates/performance/candidate.json', {'binary_sha256': self.candidate}),
                            ('linux-qualification/qualification.json', {'status': 'PASS'})):
            (selected / name).parent.mkdir(parents=True, exist_ok=True)
            (selected / name).write_text(json.dumps(value))
        files = {path.relative_to(selected).as_posix(): digest(path)
                 for path in sorted(selected.rglob('*')) if path.is_file()}
        (selected / 'report.json').write_text(json.dumps({
            'status': 'PASS', 'inputs': self.expected if inputs is None else inputs, 'epoch': epoch,
            'fixture_nonce': nonce, 'files': files,
            'identity': {'source_revision': 'r' * 40, 'baseline_binary_sha256': baseline_sha,
                         'artifacts': {'plenora-storage': self.candidate}}}))
        (result / 'vm-campaign.json').write_text(json.dumps(
            {'selected': {'report_sha256': digest(selected / 'report.json'), 'epoch': ledger_epoch}}))
        return result

    def check(self, result):
        release_campaign.check_selected(result, self.identity, self.linux, self.expected,
                                        result / 'vm-campaign.json', EPOCH, 'n0nce')

    def test_only_this_attempts_complete_evidence_of_its_binaries_passes(self):
        self.check(self.result())
        older = '6-' + '1' * 32
        for options in ({'baseline_sha': 'c' * 64}, {'inputs': {}}, {'report_baseline': 'c' * 64},
                        {'epoch': older, 'ledger_epoch': older}, {'nonce': 'other'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.check(self.result(**options))

    def test_an_earlier_valid_report_in_place_of_this_one_is_refused(self):
        older = '6-' + '1' * 32
        historical = self.result(epoch=older, ledger_epoch=older)
        report = (historical / 'selected/report.json').read_bytes()
        current = self.result()
        (current / 'selected/report.json').write_bytes(report)
        with self.assertRaises(ValueError):
            self.check(current)

    def test_a_report_other_than_the_one_in_the_ledger_is_refused(self):
        # Same epoch, nonce and files, otherwise valid: still not the report
        # whose digest the runner recorded in its ledger.
        result = self.result()
        path = result / 'selected/report.json'
        path.write_text(json.dumps({**json.loads(path.read_text()), 'note': 'written later'}))
        with self.assertRaises(ValueError):
            self.check(result)

    def test_a_file_more_less_or_changed_is_refused(self):
        def extra(selected):
            (selected / 'gates/extra.json').write_text('{}')

        def missing(selected):
            (selected / 'linux-qualification/qualification.json').unlink()

        def changed(selected):
            (selected / 'linux-qualification/qualification.json').write_text('{"status": "FAIL"}')
        for change in (extra, missing, changed):
            with self.subTest(change=change.__name__):
                result = self.result()
                change(result / 'selected')
                with self.assertRaises(ValueError):
                    self.check(result)

    def test_a_distribution_differing_from_its_verified_manifest_is_not_sealed(self):
        copy = self.folder / 'copy'
        shutil.copytree(self.linux, copy)
        release_campaign.check_distribution(copy, self.linux)
        (copy / 'plenora-storage').write_text('replaced')
        with self.assertRaises(ValueError):
            release_campaign.check_distribution(copy, self.linux)


STUB_DOCKER = """#!/usr/bin/env bash
# Stub: `ps` lists DOCKER_IDS or fails with FAIL_PS; `compose logs` fails with FAIL_LOGS.
if [ "$1" = ps ]; then [ -z "${FAIL_PS:-}" ] || exit 1; printf '%s' "${DOCKER_IDS:-}"; exit 0; fi
if [ "$1" = compose ] && [[ " $* " == *" logs "* ]] && [ -n "${FAIL_LOGS:-}" ]; then exit 1; fi
exit 0
"""

# Stub flock: fails with 66 for the arguments in FAIL_FLOCK; pauses once
# after releasing the lock for the arguments in PAUSE_FLOCK, until RESUME exists.
STUB_FLOCK = """#!/usr/bin/env bash
if [ -n "${FAIL_FLOCK:-}" ] && [ "$*" = "$FAIL_FLOCK" ]; then exit 66; fi
if [ -n "${PAUSE_FLOCK:-}" ] && [ "$*" = "$PAUSE_FLOCK" ] && mkdir "$PAUSE_DIR/intercepted" 2>/dev/null; then
  "$REAL_FLOCK" -u 9
  : >"$PAUSE_DIR/paused"
  while [ ! -e "$PAUSE_DIR/resume" ]; do sleep 0.05; done
fi
exec "$REAL_FLOCK" "$@"
"""


def alive(pid):
    try:
        state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
    except OSError:
        return False
    return state not in ('Z', 'X')


def wait_for(predicate, seconds=10):
    limit = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= limit:
            raise AssertionError('condition not reached')
        time.sleep(0.02)


@unittest.skipUnless(LINUX, 'runs bash and flock')
class ProtocolCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        (self.root / 'bin').mkdir()
        for name, text in (('docker', STUB_DOCKER), ('flock', STUB_FLOCK)):
            (self.root / 'bin' / name).write_text(text)
            (self.root / 'bin' / name).chmod(0o755)
        # The VM root exists and is private before any admission
        # (private_root_command).
        self.vm_root = self.root / 'q'
        self.vm_root.mkdir(mode=0o700)
        self.directory = self.vm_root / '.campaign'
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait()
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()
        self.folder.cleanup()

    def environment(self, **values):
        return dict(os.environ, PATH=f'{self.root / "bin"}:{os.environ["PATH"]}',
                    REAL_FLOCK=shutil.which('flock'), PAUSE_DIR=str(self.root), **values)

    def start(self, command, **values):
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                   env=self.environment(**values))
        self.processes.append(process)
        return process

    def admit(self, lease=0, **values):
        """A real admission; returns the process and its first line, split."""
        process = self.start(['sh', '-c', campaign_fence.admission_command(str(self.vm_root), lease=lease)],
                             **values)
        return process, process.stdout.readline().split()

    def admitted(self, **values):
        process, line = self.admit(**values)
        self.assertEqual(line[0], 'locked')
        return process, line[1]

    def end(self, process):
        process.stdin.close()
        return process.wait(30)

    def refused(self, **values):
        process, line = self.admit(**values)
        self.assertEqual(line, [])
        return self.end(process)

    def epoch(self):
        return (self.directory / 'epoch').read_bytes().decode('ascii')


class AdmissionProtocolTests(ProtocolCase):
    def test_the_first_admission_creates_the_directory_and_each_one_a_new_epoch(self):
        first, epoch = self.admitted()
        self.assertRegex(epoch, '^1-[0-9a-f]{32}$')
        self.assertEqual(self.refused(), campaign_fence.LOCK_HELD)
        self.assertEqual(self.epoch(), epoch + '\n')
        self.end(first)
        second, epoch = self.admitted()
        self.assertRegex(epoch, '^2-[0-9a-f]{32}$')
        self.end(second)

    def test_campaign_state_reached_by_a_link_or_open_to_others_is_refused(self):
        # Someone else's directory with a plausible lock, epoch and lease,
        # put in place of the campaign directory.
        foreign = self.root / 'foreign'
        foreign.mkdir(mode=0o700)
        (foreign / 'lock').write_text('')
        (foreign / 'epoch').write_text('5-' + '0' * 32 + '\n')
        (foreign / 'lease').write_text('none\n')
        self.directory.symlink_to(foreign)
        self.assertEqual(self.refused(), 1)
        self.assertEqual((foreign / 'epoch').read_text(), '5-' + '0' * 32 + '\n')
        self.directory.unlink()
        first, epoch = self.admitted()
        self.end(first)
        for name, change in (('lock', 'link'), ('epoch', 'open'), ('lease', 'link')):
            with self.subTest(name=name, change=change):
                path = self.directory / name
                original = path.read_bytes()
                path.unlink()
                if change == 'link':
                    (foreign / name).write_bytes(original)
                    path.symlink_to(foreign / name)
                else:
                    path.write_bytes(original)
                    path.chmod(0o666)
                self.assertEqual(self.refused(), 1)
                path.unlink()
                path.write_bytes(original)
                path.chmod(0o600)
        self.assertEqual(self.epoch(), epoch + '\n')

    def test_a_runner_of_a_lost_admission_is_fenced_whatever_its_revision(self):
        # A and B qualify different revisions, in different remote roots: the
        # admission state lives in the VM root and binds them both.
        controller_a, epoch_a = self.admitted()
        self.end(controller_a)
        controller_b, epoch_b = self.admitted()
        with self.assertRaises(campaign_fence.CampaignFenced):
            with campaign_fence.held(self.directory, epoch_a):
                self.fail('a late runner of A measured')
        with campaign_fence.held(self.directory, epoch_b) as fence:
            fence()
        self.end(controller_b)

    def test_a_surviving_runner_keeps_every_admission_out(self):
        controller_a, epoch = self.admitted()
        runner = self.start([sys.executable, '-c', RUNNER, str(ROOT / 'scripts'), str(self.directory), epoch])
        self.assertEqual(runner.stdout.readline().strip(), 'running')
        self.end(controller_a)
        self.assertEqual(self.refused(), campaign_fence.LOCK_HELD)
        self.assertEqual(self.epoch(), epoch + '\n')
        self.end(runner)
        controller_b, epoch = self.admitted()
        self.assertTrue(epoch.startswith('2-'))
        self.end(controller_b)

    def test_runner_containers_are_listed_and_a_listing_failure_is_an_error(self):
        first, epoch = self.admitted()
        self.end(first)
        self.assertEqual(self.refused(DOCKER_IDS='0123456789ab'), campaign_fence.LOCK_HELD)
        self.assertEqual(self.refused(FAIL_PS='1'), 1)
        self.assertEqual(self.epoch(), epoch + '\n')

    def test_a_lock_error_that_is_not_contention_keeps_its_code(self):
        first, epoch = self.admitted()
        self.end(first)
        self.assertEqual(self.refused(FAIL_FLOCK=f'-n -E {campaign_fence.LOCK_HELD} -x 9'), 66)
        self.assertEqual(self.epoch(), epoch + '\n')

    def test_a_missing_malformed_or_exhausted_epoch_is_an_error_not_a_fresh_start(self):
        first, epoch = self.admitted()
        self.end(first)
        limit = campaign_fence.COUNTER_LIMIT
        for content in (None, '', 'x\n', '01-' + '0' * 32 + '\n', epoch, epoch + '\n\n',
                        f'{limit}-' + '0' * 32 + '\n', epoch + '\n\0', epoch[:4] + '\0' + epoch[4:] + '\n',
                        epoch + '\r\n', epoch + '\0'):
            with self.subTest(content=content):
                if content is None:
                    (self.directory / 'epoch').unlink()
                else:
                    (self.directory / 'epoch').write_text(content)
                self.assertEqual(self.refused(), 1)
                if content is None:
                    self.assertFalse((self.directory / 'epoch').exists())
                else:
                    self.assertEqual(self.epoch(), content)
        (self.directory / 'epoch').write_text(f'{limit - 1}-' + '0' * 32 + '\n')
        last, epoch = self.admitted()
        self.assertTrue(epoch.startswith(f'{limit}-'))
        self.end(last)

    def test_a_conversion_overtaken_by_another_admission_announces_nothing(self):
        first, _ = self.admitted()
        self.end(first)
        # A releases its exclusive lock to convert it; B is admitted in that
        # instant, then A takes the shared lock next to B's.
        a = self.start(['sh', '-c', campaign_fence.admission_command(str(self.vm_root), lease=0)],
                       PAUSE_FLOCK=f'-n -E {campaign_fence.LOCK_HELD} -s 9')
        wait_for(lambda: (self.root / 'paused').exists())
        b, epoch_b = self.admitted()
        (self.root / 'resume').touch()
        self.assertEqual(a.stdout.readline(), '')
        self.assertEqual(a.wait(30), campaign_fence.LOCK_HELD)
        self.assertEqual(self.epoch(), epoch_b + '\n')
        self.end(b)

    def test_an_admission_waits_for_the_end_of_the_previous_lease(self):
        first, _ = self.admitted(lease=3)
        self.end(first)
        started = time.monotonic()
        second, _ = self.admitted(lease=0)
        self.assertGreaterEqual(time.monotonic() - started, 2)
        self.end(second)
        for content in ('garbage\n', 'none\n\0', 'no\0ne\n', 'none\0'):
            with self.subTest(content=content):
                (self.directory / 'lease').write_bytes(content.encode('ascii'))
                self.assertEqual(self.refused(), 1)

    def test_every_renewal_extends_the_lease(self):
        controller, _ = self.admitted(lease=5)
        first = (self.directory / 'lease').read_text()
        time.sleep(1.1)
        controller.stdin.write('renew\n')
        controller.stdin.flush()
        self.assertEqual(controller.stdout.readline().strip(), 'renewed')
        self.assertGreater(int((self.directory / 'lease').read_text().split()[1]), int(first.split()[1]))
        controller.stdin.write('other\n')
        controller.stdin.flush()
        self.assertEqual(controller.wait(30), 1)

    def test_an_epoch_replaced_during_a_phase_voids_it(self):
        # The lock makes a second admission wait for this runner; the only way
        # to a new epoch during the phase is outside the protocol, by
        # recreating the directory. The fence still catches it.
        controller_a, epoch_a = self.admitted()
        with tempfile.TemporaryDirectory() as temporary:
            campaign = Campaign(Path(temporary), {'subject': 'same'})
            with campaign_fence.held(self.directory, epoch_a) as fence:
                def measured(path):
                    (path / 'report.json').write_text('measured')
                    shutil.rmtree(self.directory)
                    controller_b, _ = self.admitted()
                    self.end(controller_b)
                with self.assertRaises(campaign_fence.CampaignFenced):
                    run_vm_campaign.fenced_phase(campaign, fence, 'performance-ab', measured)
            attempt = campaign.state['phases']['performance-ab'][-1]
            self.assertEqual((attempt['status'], attempt['failure_type']), ('FAIL', 'CampaignFenced'))
        self.end(controller_a)

    def test_the_runner_reports_lock_errors_and_unreadable_epochs_as_such(self):
        import fcntl
        controller, epoch = self.admitted()
        with patch.object(fcntl, 'flock', side_effect=OSError(errno.EBADF, 'bad descriptor')):
            with self.assertRaises(OSError) as failure:
                with campaign_fence.held(self.directory, epoch):
                    self.fail('held')
            self.assertNotIsInstance(failure.exception, campaign_fence.CampaignBusy)
        with patch.object(fcntl, 'flock', side_effect=OSError(errno.EWOULDBLOCK, 'busy')):
            with self.assertRaises(campaign_fence.CampaignBusy):
                with campaign_fence.held(self.directory, epoch):
                    self.fail('held')
        with campaign_fence.held(self.directory, epoch) as fence:
            (self.directory / 'epoch').rename(self.directory / 'epoch.moved')
            with self.assertRaises(RuntimeError) as failure:
                fence()
            self.assertNotIsInstance(failure.exception, campaign_fence.CampaignFenced)
            (self.directory / 'epoch.moved').rename(self.directory / 'epoch')
        self.end(controller)


@unittest.skipUnless(LINUX, 'runs bash')
class ByteExactFenceTests(unittest.TestCase):
    """Every fence accepts only the epoch and exactly one newline."""

    def test_shell_and_runner_fences_compare_every_byte(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / 'lock').write_text('')
            script = '\n'.join([*campaign_fence.fence_lines(), 'fence'])
            environment = dict(os.environ, CAMPAIGN_DIR=str(directory), CAMPAIGN_EPOCH=EPOCH)

            def shell():
                return subprocess.run(['bash', '-c', script], env=environment, stderr=subprocess.DEVNULL).returncode
            self.assertEqual(shell(), 1)
            for content, code in ((EPOCH + '\n', 0), (EPOCH, campaign_fence.FENCED),
                                  (EPOCH + '\n\n', campaign_fence.FENCED), (EPOCH + ' \n', campaign_fence.FENCED),
                                  (EPOCH + '\r\n', campaign_fence.FENCED), (EPOCH[:5] + '\0' + EPOCH[5:] + '\n',
                                                                          campaign_fence.FENCED),
                                  (EPOCH + '\n\0', campaign_fence.FENCED)):
                with self.subTest(content=content):
                    (directory / 'epoch').write_bytes(content.encode('ascii'))
                    self.assertEqual(shell(), code)
                    if code:
                        with self.assertRaises(campaign_fence.CampaignFenced):
                            with campaign_fence.held(directory, EPOCH):
                                self.fail('held')
                    else:
                        with campaign_fence.held(directory, EPOCH) as fence:
                            fence()


RUNNER = """import sys
sys.path.insert(0, sys.argv[1])
import campaign_fence
with campaign_fence.held(sys.argv[2], sys.argv[3]):
    print('running', flush=True)
    sys.stdin.read()
"""


class PreparationProtocolTests(ProtocolCase):
    """The real wrapper and script on the admission's directory, with stubbed
    docker and preparation steps."""

    def setUp(self):
        super().setUp()
        self.checkout = self.root / 'run'
        for path in ('.fixtures/minio', '.fixtures/extended', 'scripts'):
            (self.checkout / path).mkdir(parents=True)
        for path in ('.fixtures/ca.crt', '.fixtures/minio/public.crt', '.fixtures/extended/server.crt',
                     '.fixtures/sftp-fingerprint'):
            (self.checkout / path).write_text('fixture')
        (self.checkout / 'scripts/prepare-fixtures.sh').write_text('touch .fixtures/prepared\n')
        (self.checkout / 'scripts/prepare-extended-fixtures.sh').write_text('exit "${FAIL_EXTENDED:-0}"\n')
        report = "import json,sys; open(sys.argv[2],'w').write(json.dumps({'status': 'PASS'}))\n"
        (self.checkout / 'scripts/check_fixtures.py').write_text(report)
        (self.checkout / 'scripts/check_memory.py').write_text(report)

    def write(self, nonce, epoch, *, script=None, reset=True):
        generated, wrapper = release_campaign.fixture_scripts(
            str(self.checkout), 'storage-q-abc', 'vm.invalid', 'prepare', nonce, reset=reset,
            directory=str(self.directory), epoch=epoch)
        (self.checkout / f'.fixtures/prepare-{nonce}.sh').write_text(script or generated)
        (self.checkout / f'.fixtures/prepare-{nonce}-run.sh').write_text(wrapper)
        return self.checkout / f'.fixtures/prepare-{nonce}-run.sh'

    def execute(self, nonce, epoch, *, script=None, **values):
        """Run the wrapper; returns its exit code and the one it recorded."""
        code = subprocess.run(['bash', str(self.write(nonce, epoch, script=script))], stdin=subprocess.DEVNULL,
                              env=self.environment(**values), check=False).returncode
        recorded = (self.checkout / f'.fixtures/signals/prepare-{nonce}.exit').read_text().strip()
        return code, recorded

    def state(self, nonce):
        return run_vm_campaign.check_fixture_state(self.checkout / '.fixtures/campaign', nonce)

    def test_a_failed_preparation_invalidates_the_previous_reset(self):
        controller, epoch = self.admitted()
        self.assertEqual(self.execute('a' * 32, epoch), (0, '0'))
        self.state('a' * 32)
        code, recorded = self.execute('b' * 32, epoch, FAIL_EXTENDED='1')
        self.assertNotEqual(code, 0)
        self.assertEqual(recorded, str(code))
        for nonce in ('a' * 32, 'b' * 32):
            with self.assertRaises(ValueError):
                self.state(nonce)
        self.end(controller)

    def test_a_failed_diagnostic_collection_recreates_nothing(self):
        controller, epoch = self.admitted()
        self.assertNotEqual(self.execute('c' * 32, epoch, FAIL_LOGS='1')[0], 0)
        self.assertFalse((self.checkout / '.fixtures/prepared').exists())
        self.end(controller)

    def test_a_preparation_of_an_older_admission_touches_nothing(self):
        controller_a, epoch_a = self.admitted()
        self.assertEqual(self.execute('a' * 32, epoch_a), (0, '0'))
        (self.checkout / '.fixtures/prepared').unlink()
        self.end(controller_a)
        controller_b, _ = self.admitted()
        code = campaign_fence.FENCED
        self.assertEqual(self.execute('d' * 32, epoch_a), (code, str(code)))
        self.assertFalse((self.checkout / '.fixtures/prepared').exists())
        self.state('a' * 32)
        self.end(controller_b)

    def test_a_failed_container_listing_is_an_error_and_a_runner_a_refusal(self):
        controller, epoch = self.admitted()
        self.assertEqual(self.execute('a' * 32, epoch, FAIL_PS='1'), (1, '1'))
        self.assertEqual(self.execute('b' * 32, epoch, DOCKER_IDS='storage-q-abc-campaign-x\n'),
                         (release_campaign.RUNNER_ACTIVE, str(release_campaign.RUNNER_ACTIVE)))
        self.assertFalse((self.checkout / '.fixtures/campaign/fixture-state.json').exists())
        self.end(controller)

    def test_lock_contention_errors_and_a_stray_lock_code_stay_apart(self):
        import fcntl
        controller, epoch = self.admitted()
        held = campaign_fence.LOCK_HELD
        (self.checkout / '.fixtures/campaign').mkdir(parents=True)
        with open(self.checkout / '.fixtures/campaign/campaign.lock', 'a') as ledger:
            fcntl.flock(ledger.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.execute('e' * 32, epoch, script='touch .fixtures/ran\n'), (held, str(held)))
        self.assertEqual(self.execute('f' * 32, epoch, script='touch .fixtures/ran\n',
                                      FAIL_FLOCK=f'-n -E {held} -s 9'), (66, '66'))
        self.assertFalse((self.checkout / '.fixtures/ran').exists())
        self.assertEqual(self.execute('0' * 32, epoch, script=f'exit {held}\n'), (1, '1'))
        self.end(controller)

    def test_a_surviving_preparation_keeps_every_admission_out(self):
        controller_a, epoch = self.admitted()
        wrapper = self.write('a' * 32, epoch, script=': >.fixtures/started; read line\n')
        preparation = self.start(['bash', str(wrapper)])
        wait_for(lambda: (self.checkout / '.fixtures/started').exists())
        self.end(controller_a)
        self.assertEqual(self.refused(), campaign_fence.LOCK_HELD)
        self.assertEqual(self.epoch(), epoch + '\n')
        self.end(preparation)
        controller_b, epoch = self.admitted()
        self.assertTrue(epoch.startswith('2-'))
        self.end(controller_b)

    def test_a_detached_descendant_without_the_lock_is_fenced(self):
        # The wrapper and its script are killed; a descendant that closed the
        # lock descriptor survives them. The lock no longer holds the VM, but
        # the descendant's next write is fenced.
        controller_a, epoch = self.admitted()
        script = '\n'.join([
            *campaign_fence.fence_lines(),
            'echo "$$ $PPID" >.fixtures/pids',
            '( exec 8<&- 9<&-; echo "$BASHPID" >.fixtures/descendant',
            '  while [ ! -e .fixtures/go ]; do sleep 0.05; done',
            '  fence; : >.fixtures/descendant-wrote ) </dev/null >/dev/null 2>&1 &',
            ': >.fixtures/started; read line', ''])
        preparation = self.start(['bash', str(self.write('a' * 32, epoch, script=script))])
        wait_for(lambda: (self.checkout / '.fixtures/started').exists()
                 and (self.checkout / '.fixtures/descendant').exists())
        self.end(controller_a)
        for pid in (self.checkout / '.fixtures/pids').read_text().split():
            os.kill(int(pid), 9)
        preparation.kill()
        preparation.wait()
        descendant = int((self.checkout / '.fixtures/descendant').read_text())
        self.assertTrue(alive(descendant))
        controller_b, _ = self.admitted()
        (self.checkout / '.fixtures/go').touch()
        wait_for(lambda: not alive(descendant))
        self.assertFalse((self.checkout / '.fixtures/descendant-wrote').exists())
        self.end(controller_b)


if __name__ == '__main__':
    unittest.main()
