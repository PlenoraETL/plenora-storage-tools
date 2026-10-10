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
    def __init__(self, code=0, finished=False):
        self.closed, self.code, self.finished, self.sent = False, code, finished, []

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

    def renew(self):
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

    def __init__(self, root, *, code='0', check='PASS'):
        self.root, self.code, self.check = root, code, check
        self.files, self.commands = {}, []

    def write(self, remote, text):
        self.files[remote] = text

    def download(self, remote, path):
        path.write_text(self.files[remote])

    def run(self, command):
        self.commands.append(command)
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
        def __init__(self, first, code=0, epoch=EPOCH, replies=()):
            self.first, self.code, self.epoch, self.replies, self.channel = first, code, epoch, replies, None

        def hold(self, command):
            self.channel = FakeChannel(self.code, finished=not self.first.startswith('locked'))
            return self.channel, FakeReader(self.replies), self.first

        def run(self, command):
            return self.epoch

    def test_an_admission_yields_its_epoch_and_renews_its_lease_at_every_check(self):
        now = [100.0]
        remote = self.Remote(f'locked {EPOCH} /srv/q/.campaign', replies=['renewed\n'] * 2)
        with campaign_fence.admission(remote, '/srv/q', clock=lambda: now[0]) as session:
            self.assertEqual((session.epoch, session.directory), (EPOCH, '/srv/q/.campaign'))
            self.assertEqual(remote.channel.timeout, campaign_fence.RENEW_SECONDS)
            now[0] = 150.0
            session.check(remote)
            self.assertEqual(session.lease_until, 150.0 + campaign_fence.LEASE_SECONDS)
            remote.epoch = '8-' + '1' * 32
            with self.assertRaises(campaign_fence.CampaignFenced):
                session.check(remote)
            self.assertEqual(remote.channel.sent, [b'renew\n'] * 2)
        self.assertTrue(remote.channel.closed)

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

    def test_the_controller_seals_only_evidence_of_its_own_binaries(self):
        folder = Path(self.temporary.name)
        linux = folder / 'linux'
        linux.mkdir()
        (linux / 'plenora-storage').write_text('candidate')
        (linux / 'release-manifest.json').write_text(json.dumps({'artifacts': [{'name': 'plenora-storage'}]}))
        identity = {'revision': 'r' * 40, 'baseline_sha256': 'b' * 64}
        candidate = digest(linux / 'plenora-storage')

        def selected(baseline_sha, inputs, report_baseline):
            result = folder / 'result'
            shutil.rmtree(result, ignore_errors=True)
            performance = result / 'selected/gates/performance'
            performance.mkdir(parents=True)
            (performance / 'baseline.json').write_text(json.dumps({'binary_sha256': report_baseline}))
            (performance / 'candidate.json').write_text(json.dumps({'binary_sha256': candidate}))
            (result / 'selected/report.json').write_text(json.dumps({
                'status': 'PASS', 'inputs': inputs,
                'identity': {'source_revision': 'r' * 40, 'baseline_binary_sha256': baseline_sha,
                             'artifacts': {'plenora-storage': candidate}}}))
            return result

        release_campaign.check_selected(selected('b' * 64, self.expected, 'b' * 64), identity, linux, self.expected)
        for arguments in (('c' * 64, self.expected, 'b' * 64), ('b' * 64, {}, 'b' * 64),
                          ('b' * 64, self.expected, 'c' * 64)):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                release_campaign.check_selected(selected(*arguments), identity, linux, self.expected)


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
        self.vm_root = self.root / 'q'
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
        return (self.directory / 'epoch').read_text()


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
                        f'{limit}-' + '0' * 32 + '\n'):
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
        (self.directory / 'lease').write_text('garbage\n')
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
