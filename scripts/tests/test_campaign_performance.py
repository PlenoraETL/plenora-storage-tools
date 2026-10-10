import json
import math
from pathlib import Path
import shutil
import socket
from statistics import median
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campaign_state import Campaign
from check_performance import UNRELIABLE, compare
from fixture_connections import BUFFERED, PROVIDERS
from performance_order import SCHEME, paired_order, validate_pairing
import qualify_transfers
import campaign_fence
import release_campaign
import release_evidence
import run_vm_campaign

ROOT = Path(__file__).resolve().parents[2]
POLICY = json.loads((ROOT / 'scripts/performance-policy.json').read_text())
ROUNDS, WORKERS = run_vm_campaign.PERFORMANCE_ROUNDS, 4
BINARIES = {'baseline': 'a' * 64, 'candidate': 'b' * 64}
ENVIRONMENT = {'machine': 'x86_64', 'kernel': 'test', 'cpu_count': 8, 'cpu_model': 'test', 'fixture_sha256': 'c' * 64}
OPERATIONS = ('test', 'put', 'get', 'stat', 'copy', 'delete', 'delete')


def report(name, rounds):
    return {'schema_version': 1, 'binary_sha256': BINARIES[name], 'platform': 'linux',
            'campaign_id': str(uuid.uuid4()), 'environment': ENVIRONMENT, 'source_revision': 'd' * 40,
            'dirty': False, 'payload_bytes': 1024**2, 'workers': WORKERS, 'rounds': rounds, 'spool_uploads': False,
            'rss_limit_bytes': 256 * 1024**2, 'status': 'PASS', 'results': []}


def measured(runs, rounds, slowdown):
    """Reports of a run executing `runs` (round, provider, role) in order: the
    workers of one run are concurrent, runs are sequential, and the elapsed
    time of the run at position `clock` is 0.2 s times 1 + slowdown(fraction
    of the run already done)."""
    reports = {name: report(name, rounds) for name in BINARIES}
    for clock, (iteration, provider, name) in enumerate(runs):
        elapsed = 0.2 * (1 + slowdown(clock / len(runs)))
        for _ in range(WORKERS):
            reports[name]['results'].append({
                'provider': provider, 'round': iteration, 'status': 'PASS', 'payload_bytes': 1024**2,
                'mode': 'buffered_roundtrip' if provider in BUFFERED else 'streaming_roundtrip',
                'measurements': [{'operation': operation, 'status': 'PASS', 'elapsed_seconds': elapsed,
                                  'peak_rss_bytes': 12 * 1024**2} for operation in OPERATIONS]})
    return reports['baseline'], reports['candidate']


def linear(drift):
    return lambda fraction: drift * fraction


def sequential(rounds):
    return [(r, p, name) for name in ('baseline', 'candidate') for r in range(rounds) for p in PROVIDERS]


def alternated(rounds):
    runs = []
    for slot in paired_order(rounds, PROVIDERS):
        second = 'candidate' if slot['first'] == 'baseline' else 'baseline'
        runs += [(slot['round'], slot['provider'], slot['first']), (slot['round'], slot['provider'], second)]
    return runs


def pair(baseline, candidate):
    order = paired_order(baseline['rounds'], PROVIDERS)
    for name, own, other in (('baseline', baseline, candidate), ('candidate', candidate, baseline)):
        own['paired_measurement'] = {'role': name, 'order_scheme': SCHEME,
                                     'partner_campaign_id': other['campaign_id'], 'order': order}


def paired_run(slowdown):
    baseline, candidate = measured(alternated(ROUNDS), ROUNDS, slowdown)
    pair(baseline, candidate)
    return baseline, candidate


def statistics(report_, provider, operation):
    """Median and p95 exactly as check_performance computes them."""
    values = sorted(m['elapsed_seconds'] for row in report_['results'] if row['provider'] == provider
                    for m in row['measurements'] if m['operation'] == operation)
    return median(values), values[math.ceil(len(values) * .95) - 1]


def largest_residual(baseline, candidate):
    """Largest relative difference between the roles, per compared statistic."""
    worst = [0.0, 0.0]
    for provider in PROVIDERS:
        for operation in set(OPERATIONS):
            for index, (old, new) in enumerate(zip(statistics(baseline, provider, operation),
                                                   statistics(candidate, provider, operation))):
                worst[index] = max(worst[index], abs(new / old - 1))
    return worst


class PairedOrderTests(unittest.TestCase):
    def test_every_provider_follows_abba_on_its_own_rounds(self):
        order = paired_order(ROUNDS, PROVIDERS)
        self.assertEqual(order, paired_order(ROUNDS, PROVIDERS))
        self.assertEqual([(slot['round'], slot['provider']) for slot in order],
                         [(r, p) for r in range(ROUNDS) for p in PROVIDERS])
        for provider in PROVIDERS:
            firsts = [slot['first'] for slot in order if slot['provider'] == provider]
            self.assertEqual(firsts, ['baseline', 'candidate', 'candidate', 'baseline'] * (ROUNDS // 4))

    def test_rounds_must_be_a_multiple_of_four(self):
        for rounds in (0, 2, 30, 31):
            with self.assertRaises(ValueError):
                paired_order(rounds, PROVIDERS)
        self.assertEqual(ROUNDS % 4, 0)

    def test_a_drift_the_guard_accepts_leaves_only_a_one_run_residual(self):
        # 8 % over the whole run: each binary moves 4 % between its halves,
        # inside the guard (half of the 10 % budget).
        baseline, candidate = paired_run(linear(0.08))
        comparison = compare(baseline, candidate, BINARIES['candidate'], POLICY)
        self.assertEqual(comparison['status'], 'PASS')
        self.assertTrue(all(row['status'] == 'STABLE' for row in comparison['stability']))
        # Runs are sequential, so at any order statistic the two roles' samples
        # are one run apart: the residual is one run's share of the drift.
        step = 0.08 / len(alternated(ROUNDS))
        median_residual, p95_residual = largest_residual(baseline, candidate)
        self.assertLessEqual(median_residual, step * 1.01)
        self.assertLessEqual(p95_residual, step * 1.01)
        # The same drift, run one binary after the other, biases the candidate
        # by half of it.
        old, new = measured(sequential(ROUNDS), ROUNDS, linear(0.08))
        self.assertGreater(largest_residual(old, new)[0], 0.035)

    def test_the_documented_residual_of_a_large_linear_drift(self):
        # The figures quoted in docs/release-campaign.md, for 50 % over the run.
        baseline, candidate = paired_run(linear(0.5))
        median_residual, p95_residual = largest_residual(baseline, candidate)
        self.assertAlmostEqual(median_residual * 100, 0.07, delta=0.01)
        self.assertAlmostEqual(p95_residual * 100, 0.06, delta=0.01)
        # ... and that drift is far beyond what the guard accepts.
        self.assertEqual(compare(baseline, candidate, BINARIES['candidate'], POLICY)['status'], UNRELIABLE)

    def test_a_jump_mid_run_is_unreliable_never_pass_or_regression(self):
        baseline, candidate = paired_run(lambda fraction: 0.25 if fraction >= 0.5 else 0.0)
        comparison = compare(baseline, candidate, BINARIES['candidate'], POLICY)
        self.assertEqual(comparison['status'], UNRELIABLE)
        self.assertTrue(any(row['status'] == 'UNSTABLE' for row in comparison['stability']))
        # Even when the candidate also regressed, the verdict is unreliable.
        for row in candidate['results']:
            for measure in row['measurements']:
                measure['elapsed_seconds'] *= 1.3
        self.assertEqual(compare(baseline, candidate, BINARIES['candidate'], POLICY)['status'], UNRELIABLE)

    def test_the_stability_allowance_is_five_percent_with_a_ten_millisecond_floor(self):
        from check_performance import stability_checks
        self.assertEqual(POLICY['stability_allowance']['median_percent'], 5)
        self.assertEqual(POLICY['stability_allowance']['minimum_seconds'], 0.010)

        def halves(first, second):
            own = report('baseline', 4)
            for iteration in range(4):
                elapsed = first if iteration < 2 else second
                own['results'].append({'provider': 'azure', 'round': iteration, 'measurements': [
                    {'operation': 'copy', 'elapsed_seconds': elapsed}]})
            return stability_checks(own, 'baseline', POLICY)[0]['status']
        # The accepted 2.1.0 campaign: azure copy moved 5.45 ms on a quiet host.
        self.assertEqual(halves(0.06530, 0.05985), 'STABLE')
        # A real drift of 3.0.0 day: smb copy -11.7 % on 90 ms.
        self.assertEqual(halves(0.09065, 0.08005), 'UNSTABLE')
        # Long operations follow the percentage: ftp copy +26 % on 5.3 s.
        self.assertEqual(halves(5.2785, 6.65395), 'UNSTABLE')

    def test_a_real_regression_still_fails_when_alternated(self):
        baseline, candidate = paired_run(linear(0.0))
        for row in candidate['results']:
            for measure in row['measurements']:
                measure['elapsed_seconds'] *= 1.3
        self.assertEqual(compare(baseline, candidate, BINARIES['candidate'], POLICY)['status'], 'FAIL')

    def test_paired_metadata_is_validated_never_read_as_absent(self):
        baseline, candidate = measured(alternated(ROUNDS), ROUNDS, linear(0.0))
        self.assertFalse(validate_pairing(baseline, candidate, PROVIDERS))
        pair(baseline, candidate)
        self.assertTrue(validate_pairing(baseline, candidate, PROVIDERS))
        for broken in (None, {}, [], 'ABBA'):
            with self.subTest(broken=broken):
                altered = dict(candidate, paired_measurement=broken)
                with self.assertRaises(ValueError):
                    compare(baseline, altered, BINARIES['candidate'], POLICY)
        order = paired_order(ROUNDS, PROVIDERS)
        wrong_types = [dict(order[0], round=True), dict(order[0], round=0.0), dict(order[0], first='other'),
                       dict(order[0], provider=7), dict(order[0], extra=1)]
        for field, value in [('order', order[::-1]), ('order', []), ('order', None), ('order_scheme', 'ABAB'),
                             ('role', 'baseline'), ('partner_campaign_id', 'x'),
                             *(('order', [slot] + order[1:]) for slot in wrong_types)]:
            with self.subTest(field=field, value=str(value)[:40]):
                broken = dict(candidate['paired_measurement'], **{field: value})
                altered = dict(candidate, paired_measurement=broken)
                with self.assertRaises(ValueError):
                    compare(baseline, altered, BINARIES['candidate'], POLICY)
        with self.assertRaises(ValueError):
            compare(baseline, {k: v for k, v in candidate.items() if k != 'paired_measurement'},
                    BINARIES['candidate'], POLICY)

    def test_release_evidence_from_3_0_0_requires_a_paired_run(self):
        baseline, candidate = measured(alternated(ROUNDS), ROUNDS, linear(0.0))
        release_evidence.require_paired_performance((2, 1, 0), baseline, candidate)
        with self.assertRaises(ValueError):
            release_evidence.require_paired_performance((3, 0, 0), baseline, candidate)
        pair(baseline, candidate)
        release_evidence.require_paired_performance((3, 0, 0), baseline, candidate)
        with self.assertRaises(ValueError):
            release_evidence.validate_transfers(candidate, BINARIES['candidate'], size=1024**2, workers=WORKERS,
                                                rounds=1)


class ProducerTests(unittest.TestCase):
    """Runs the real qualify_transfers loop, concurrent workers included, with
    a recording roundtrip in place of the fixtures and binaries."""

    def test_runs_follow_the_recorded_order_slot_by_slot(self):
        events, lock = [], threading.Lock()

        def roundtrip(binary, provider, *_arguments):
            with lock:
                events.append(('start', binary.name, provider))
            with lock:
                events.append(('end', binary.name, provider))
            return {'provider': provider, 'status': 'PASS', 'payload_bytes': 1024,
                    'mode': 'streaming_roundtrip', 'measurements': []}

        rounds = 4
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            for role in ('baseline', 'candidate'):
                (folder / role).write_bytes(role.encode())
            argv = ['qualify_transfers.py', '--bytes', '1024', '--workers', str(WORKERS), '--rounds', str(rounds),
                    '--baseline-binary', str(folder / 'baseline'), '--baseline-output', str(folder / 'b.json'),
                    '--output', str(folder / 'c.json')]
            with patch.object(sys, 'argv', argv), patch.object(qualify_transfers.sys, 'platform', 'linux'), \
                    patch.object(qualify_transfers, 'roundtrip', roundtrip), \
                    patch.object(qualify_transfers, 'measurement_environment', return_value=ENVIRONMENT), \
                    patch.dict('os.environ', {'PLENORA_CLI_BIN': str(folder / 'candidate')}):
                qualify_transfers.main()
            reports = {role: json.loads((folder / name).read_text())
                       for role, name in (('baseline', 'b.json'), ('candidate', 'c.json'))}
        expected = paired_order(rounds, PROVIDERS)
        for role, own in reports.items():
            self.assertEqual(own['paired_measurement']['order'], expected)
            self.assertEqual(own['paired_measurement']['role'], role)
            self.assertEqual(len(own['results']), rounds * len(PROVIDERS) * WORKERS)
        per_slot = 4 * WORKERS
        self.assertEqual(len(events), per_slot * len(expected))
        for index, slot in enumerate(expected):
            window = events[index * per_slot:(index + 1) * per_slot]
            second = 'candidate' if slot['first'] == 'baseline' else 'baseline'
            self.assertEqual([event[1] for event in window], [slot['first']] * 2 * WORKERS + [second] * 2 * WORKERS,
                             slot)
            self.assertTrue(all(event[2] == slot['provider'] for event in window))

    def test_a_paired_run_refuses_rounds_that_are_not_a_multiple_of_four(self):
        argv = ['qualify_transfers.py', '--rounds', '30', '--baseline-binary', 'b', '--baseline-output', 'b.json']
        with patch.object(sys, 'argv', argv), patch.object(qualify_transfers.sys, 'platform', 'linux'), \
                self.assertRaises(SystemExit):
            qualify_transfers.main()


class RetryTests(unittest.TestCase):
    def test_a_retry_of_a_passed_phase_is_refused_not_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            campaign = Campaign(Path(temporary), {'subject': 'same'})
            campaign.phase('performance-ab', lambda path: (path / 'report.json').write_text('passed'))
            with self.assertRaises(ValueError):
                campaign.validate_retries(['performance-ab'], run_vm_campaign.PHASES)
            with self.assertRaises(ValueError):
                campaign.phase('performance-ab', lambda path: self.fail('remeasured'), retry=True, reason='again')
            with self.assertRaises(ValueError):
                campaign.validate_retries(['performance-baseline'], run_vm_campaign.PHASES)

            def failure(path):
                (path / 'report.json').write_text('failed')
                raise RuntimeError('budget')
            with self.assertRaises(RuntimeError):
                campaign.phase('performance-compare', failure)
            campaign.validate_retries(['performance-compare'], run_vm_campaign.PHASES)
            result = campaign.phase('performance-compare', lambda path: (path / 'report.json').write_text('ok'),
                                    retry=True, reason='compare again')
            self.assertEqual(result.name, '2')

    def test_vm_retries_need_a_new_vm_attempt_and_an_executed_phase(self):
        release_campaign.validate_vm_retries([], [], '3.0.0')
        release_campaign.validate_vm_retries(['soak'], ['qualify-vm'], '3.0.0')
        with self.assertRaises(ValueError):
            release_campaign.validate_vm_retries(['soak'], [], '3.0.0')
        with self.assertRaises(ValueError):
            release_campaign.validate_vm_retries(['spooled-large'], ['qualify-vm'], '2.0.1')
        with self.assertRaises(ValueError):
            release_campaign.validate_vm_retries(['performance-baseline'], ['qualify-vm'], '3.0.0')
        with tempfile.TemporaryDirectory() as temporary:
            campaign = Campaign(Path(temporary), {'subject': 'same'})
            campaign.phase('qualify-vm', lambda path: (path / 'report.json').write_text('passed'))
            with self.assertRaises(ValueError):
                campaign.validate_retries(['qualify-vm'], release_campaign.LOCAL_PHASES)

    def test_phases_follow_the_version(self):
        self.assertIn('performance-ab', run_vm_campaign.PHASES)
        self.assertNotIn('performance-baseline', run_vm_campaign.PHASES)
        self.assertEqual(run_vm_campaign.PERFORMANCE_ORDER, SCHEME)
        self.assertNotIn('spooled-large', run_vm_campaign.phases_for('2.0.1'))
        self.assertIn('spooled-large', run_vm_campaign.phases_for('3.0.0'))


class FakeChannel:
    def __init__(self, code=0, finished=False):
        self.closed, self.code, self.finished = False, code, finished

    def close(self):
        self.closed = True

    def exit_status_ready(self):
        return self.finished

    def recv_exit_status(self):
        return self.code


class FakeSession:
    """An admission that holds the VM until `lose()` is called."""

    def __init__(self, epoch=1, directory='/srv/q/.campaign'):
        self.epoch, self.directory, self.lost = epoch, directory, False

    def alive(self):
        return not self.lost

    def check(self, remote):
        if self.lost:
            raise campaign_fence.CampaignLost('lost')

    def lose(self):
        self.lost = True


class FakeRemote:
    """A VM whose fixture preparation finishes after a few polls with a
    scripted outcome; files are kept in memory by remote path."""

    def __init__(self, root, *, code='0', check='PASS', polls=2):
        self.root, self.code, self.check, self.polls = root, code, check, polls
        self.files, self.commands = {}, []

    def write(self, remote, text):
        self.files[remote] = text

    def download(self, remote, path):
        path.write_text(self.files[remote])

    def run(self, command):
        self.commands.append(command)
        if '(nohup bash .fixtures/' in command:
            self.label_nonce = command.split('(nohup bash .fixtures/', 1)[1].split('-run.sh', 1)[0]
            return 'started'
        if 'then cat ' in command:
            signal = command.split('if test -f ', 1)[1].split(';', 1)[0]
            if signal in [f'.fixtures/signals/{self.label_nonce}.exit'] and self.polls == 0:
                self.finish()
            self.polls -= 1
            return self.files.get(f'{self.root}/{signal}', 'running') if signal in self.local_signals() else 'running'
        if 'then echo yes' in command:
            path = command.split('if test -f ', 1)[1].split(';', 1)[0]
            return 'yes' if f'{self.root}/{path}' in self.files else 'no'
        return ''

    def local_signals(self):
        return {key[len(self.root) + 1:] for key in self.files if key.endswith('.exit')}

    def finish(self):
        signal = f'{self.root}/.fixtures/signals/{self.label_nonce}'
        self.files[signal + '.exit'] = self.code
        self.files[signal + '.log'] = 'log'
        if self.code != str(campaign_fence.LOCK_HELD):
            self.files[f'{self.root}/.fixtures/diagnostics/{self.label_nonce}.tar.gz'] = 'archive'
        self.files[signal + '-check.json'] = json.dumps({'status': self.check})


class FixtureResetTests(unittest.TestCase):
    ROOT = '/srv/root'

    def reset(self, remote, folder, session=None):
        return release_campaign.reset_fixtures(remote, self.ROOT, 'storage-q-abc', 'vm.invalid', folder,
                                               session or FakeSession(), poll=0, deadline=60)

    def test_a_reset_is_recorded_with_its_nonce_and_epoch_only_after_every_fixture_answers(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            remote = FakeRemote(self.ROOT)
            nonce = self.reset(remote, folder, FakeSession(epoch=7))
            record = json.loads((folder / 'fixture-reset.json').read_text())
            self.assertEqual((record['nonce'], record['epoch'], record['recreated']), (nonce, 7, True))
            self.assertEqual(json.loads((folder / 'fixture-reset-nonce.json').read_text())['nonce'], nonce)
            self.assertTrue((folder / 'pre-reset.tar.gz').is_file())
            script = remote.files[f'{self.ROOT}/.fixtures/fixture-reset-{nonce}.sh']
            self.assertIn(f'.fixtures/signals/fixture-reset-{nonce}', script)
            self.assertNotIn('|| true', script)
            wrapper = remote.files[f'{self.ROOT}/.fixtures/fixture-reset-{nonce}-run.sh']
            self.assertIn('CAMPAIGN_EPOCH=7', wrapper)

    def test_a_stale_exit_signal_is_never_read_as_this_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            remote = FakeRemote(self.ROOT, code='1')
            # A successful signal left by an earlier execution of the same label.
            remote.files[f'{self.ROOT}/.fixtures/signals/fixture-reset-{"0" * 32}.exit'] = '0'
            with self.assertRaises(ValueError):
                self.reset(remote, folder)
            self.assertFalse((folder / 'fixture-reset.json').exists())

    def test_every_refusal_is_typed_and_records_no_reset(self):
        cases = ((str(campaign_fence.LOCK_HELD), 'PASS', campaign_fence.CampaignBusy),
                 (str(release_campaign.RUNNER_ACTIVE), 'PASS', campaign_fence.CampaignBusy),
                 (str(campaign_fence.FENCED), 'PASS', campaign_fence.CampaignFenced),
                 ('0', 'FAIL', ValueError))
        for code, check, error in cases:
            with self.subTest(code=code, check=check), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                with self.assertRaises(error):
                    self.reset(FakeRemote(self.ROOT, code=code, check=check), folder)
                self.assertFalse((folder / 'fixture-reset.json').exists())

    def test_a_lost_admission_starts_no_preparation(self):
        session = FakeSession()
        session.lose()
        remote = FakeRemote(self.ROOT)
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(campaign_fence.CampaignLost):
            self.reset(remote, Path(temporary), session)
        self.assertEqual(remote.files, {})

    def test_the_wrapper_holds_the_admission_lock_and_the_script_fences_every_change(self):
        script, wrapper = release_campaign.fixture_scripts(self.ROOT, 'storage-q-abc', 'vm.invalid', 'fixture-reset',
                                                           'n0nce', recreate=True, directory='/srv/q/.campaign',
                                                           epoch=3)
        self.assertIn('exec 9</srv/q/.campaign/lock', wrapper)
        self.assertIn(f'flock -n -s 9 || exit {campaign_fence.LOCK_HELD}', wrapper)
        self.assertIn(f'flock -n -E {campaign_fence.LOCK_HELD} .fixtures/campaign/campaign.lock', wrapper)
        self.assertIn('mv .fixtures/signals/fixture-reset-n0nce.exit.pending .fixtures/signals/fixture-reset-n0nce.exit',
                      wrapper)
        self.assertTrue(wrapper.rstrip().endswith('exit "$code"'))
        steps = script.splitlines()
        fences = [i for i, line in enumerate(steps) if line == 'fence']
        runner = next(i for i, line in enumerate(steps) if "grep -q '^storage-q-abc-campaign-'" in line)
        archive = next(i for i, line in enumerate(steps) if 'logs --no-color' in line)
        recreate = steps.index('export PLENORA_FIXTURE_RECREATE=1')
        check = next(i for i, line in enumerate(steps) if 'check_fixtures.py' in line)
        states = [i for i, line in enumerate(steps) if line.startswith('printf') and 'fixture-state' in line]
        memory = next(i for i, line in enumerate(steps) if 'check_memory.py' in line)
        self.assertIn('in-progress', steps[states[0]])
        self.assertIn('reset', steps[states[-1]])
        # A fence first of all, then before the first change, before the
        # recreation and before the final record.
        self.assertLess(fences[0], runner)
        self.assertTrue(any(runner < f < states[0] for f in fences))
        self.assertTrue(any(memory < f < recreate for f in fences))
        self.assertTrue(any(check < f < states[-1] for f in fences))
        self.assertLess(states[0], archive)
        self.assertLess(archive, memory)
        self.assertLess(recreate, check)

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


class FixtureDefinitionTests(unittest.TestCase):
    def test_fake_gcs_keeps_no_state_that_slows_listing_down(self):
        # Plain text: the test tooling has no YAML parser.
        lines = (ROOT / 'compose.extended.yml').read_text().splitlines()
        service = lines.index('  gcs:')
        command = next(line for line in lines[service:] if line.strip().startswith('command:'))
        self.assertIn('"-backend", "memory"', command)


STUB_DOCKER = """#!/usr/bin/env bash
# Stub: `ps` lists the ids in DOCKER_IDS; `compose logs` fails when FAIL_LOGS is set.
if [ "$1" = ps ]; then printf '%s' "${DOCKER_IDS:-}"; exit 0; fi
if [ "$1" = compose ] && [[ " $* " == *" logs "* ]] && [ -n "${FAIL_LOGS:-}" ]; then exit 1; fi
exit 0
"""

LINUX = sys.platform != 'win32' and bool(shutil.which('bash')) and bool(shutil.which('flock'))


def stub_docker(folder):
    (folder / 'bin').mkdir(parents=True, exist_ok=True)
    (folder / 'bin/docker').write_text(STUB_DOCKER)
    (folder / 'bin/docker').chmod(0o755)


def stub_environment(folder, **values):
    import os
    return dict(os.environ, PATH=f'{folder / "bin"}:{os.environ["PATH"]}', **values)


@unittest.skipUnless(LINUX, 'runs the generated bash script')
class FixtureScriptBehaviourTests(unittest.TestCase):
    """Executes the generated preparation script with stubbed docker and
    preparation steps, and checks what it leaves behind."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        for path in ('.fixtures/signals', '.fixtures/campaign', '.fixtures/minio', '.fixtures/extended', 'scripts',
                     'campaign'):
            (self.root / path).mkdir(parents=True)
        for path in ('.fixtures/ca.crt', '.fixtures/minio/public.crt', '.fixtures/extended/server.crt',
                     '.fixtures/sftp-fingerprint'):
            (self.root / path).write_text('fixture')
        (self.root / 'campaign/epoch').write_text('1\n')
        stub_docker(self.root)
        (self.root / 'scripts/prepare-fixtures.sh').write_text('touch .fixtures/prepared\n')
        (self.root / 'scripts/prepare-extended-fixtures.sh').write_text('exit "${FAIL_EXTENDED:-0}"\n')
        report = "import json,sys; open(sys.argv[2],'w').write(json.dumps({'status': 'PASS'}))\n"
        (self.root / 'scripts/check_fixtures.py').write_text(report)
        (self.root / 'scripts/check_memory.py').write_text(report)

    def tearDown(self):
        self.folder.cleanup()

    def execute(self, nonce, epoch=1, **environment):
        import subprocess
        script, _ = release_campaign.fixture_scripts(str(self.root), 'storage-q-abc', 'vm.invalid', 'fixture-reset',
                                                     nonce, recreate=True, directory=str(self.root / 'campaign'),
                                                     epoch=epoch)
        path = self.root / f'.fixtures/fixture-reset-{nonce}.sh'
        path.write_text(script)
        env = stub_environment(self.root, CAMPAIGN_DIR=str(self.root / 'campaign'), CAMPAIGN_EPOCH=str(epoch),
                               **environment)
        return subprocess.run(['bash', str(path)], env=env, check=False).returncode

    def test_a_failed_preparation_invalidates_the_previous_reset(self):
        self.assertEqual(self.execute('a' * 32), 0)
        run_vm_campaign.check_fixture_state(self.root / '.fixtures/campaign', 'a' * 32)
        self.assertNotEqual(self.execute('b' * 32, FAIL_EXTENDED='1'), 0)
        with self.assertRaises(ValueError):
            run_vm_campaign.check_fixture_state(self.root / '.fixtures/campaign', 'a' * 32)
        with self.assertRaises(ValueError):
            run_vm_campaign.check_fixture_state(self.root / '.fixtures/campaign', 'b' * 32)

    def test_a_failed_diagnostic_collection_recreates_nothing(self):
        self.assertNotEqual(self.execute('c' * 32, FAIL_LOGS='1'), 0)
        self.assertFalse((self.root / '.fixtures/prepared').exists())
        with self.assertRaises(ValueError):
            run_vm_campaign.check_fixture_state(self.root / '.fixtures/campaign', 'c' * 32)

    def test_a_preparation_of_an_older_admission_touches_nothing(self):
        self.assertEqual(self.execute('a' * 32), 0)
        (self.root / '.fixtures/prepared').unlink()
        (self.root / 'campaign/epoch').write_text('2\n')
        self.assertEqual(self.execute('d' * 32, epoch=1), campaign_fence.FENCED)
        self.assertFalse((self.root / '.fixtures/prepared').exists())
        run_vm_campaign.check_fixture_state(self.root / '.fixtures/campaign', 'a' * 32)


RUNNER = """import sys, time
sys.path.insert(0, sys.argv[1])
import campaign_fence
with campaign_fence.held(sys.argv[2], int(sys.argv[3])):
    print('running', flush=True)
    sys.stdin.read()
"""

BLOCKING_PREPARATION = ': >.fixtures/started; read line\n'


class AdmissionTests(unittest.TestCase):
    """The controller side of the admission, with fake channels."""

    class Remote:
        def __init__(self, first, code=0, epoch='3'):
            self.first, self.code, self.epoch, self.channel = first, code, epoch, None

        def hold(self, command):
            self.channel = FakeChannel(self.code, finished=self.first == '')
            return self.channel, self.first

        def run(self, command):
            return self.epoch

    def test_an_admission_yields_its_epoch_and_directory(self):
        remote = self.Remote('locked 3 /srv/q/.campaign')
        with campaign_fence.admission(remote, '/srv/q') as session:
            self.assertEqual((session.epoch, session.directory), (3, '/srv/q/.campaign'))
            session.check(remote)
            remote.epoch = '4'
            with self.assertRaises(campaign_fence.CampaignFenced):
                session.check(remote)
        self.assertTrue(remote.channel.closed)

    def test_contention_is_busy_and_any_other_failure_is_reported_as_such(self):
        with self.assertRaises(campaign_fence.CampaignBusy):
            with campaign_fence.admission(self.Remote('', campaign_fence.LOCK_HELD), '/srv/q'):
                self.fail('admitted')
        for first, code in (('', 1), ('locked x /d', 1), ('unexpected', 2)):
            with self.subTest(first=first), self.assertRaises(RuntimeError) as failure:
                with campaign_fence.admission(self.Remote(first, code), '/srv/q'):
                    self.fail('admitted')
            self.assertNotIsInstance(failure.exception, campaign_fence.CampaignBusy)

    def test_a_finished_admission_channel_is_a_lost_admission(self):
        remote = self.Remote('locked 3 /srv/q/.campaign')
        with campaign_fence.admission(remote, '/srv/q') as session:
            remote.channel.finished = True
            with self.assertRaises(campaign_fence.CampaignLost):
                session.check(remote)

    def test_a_busy_vm_exits_with_the_lock_code(self):
        def busy():
            raise campaign_fence.CampaignBusy('held')
        self.assertEqual(release_campaign.entrypoint(busy), campaign_fence.LOCK_HELD)
        self.assertEqual(release_campaign.entrypoint(lambda: None), 0)
        for error in (campaign_fence.CampaignFenced('epoch'), RuntimeError('lock command failed')):
            def failing(error=error):
                raise error
            with self.assertRaises(type(error)):
                release_campaign.entrypoint(failing)

    def test_losing_the_admission_stops_the_windows_qualification(self):
        import threading
        session = FakeSession()
        threading.Timer(0.5, session.lose).start()
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(campaign_fence.CampaignLost):
                campaign_fence.supervised([sys.executable, '-c', 'import time; time.sleep(60)'],
                                          Path(temporary) / 'command.log', session, poll=0.1, grace=5)
        self.assertLess(time.monotonic() - started, 30)

    def test_a_supervised_command_reports_its_own_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / 'command.log'
            campaign_fence.supervised([sys.executable, '-c', 'pass'], log, FakeSession(), poll=0.05)
            with self.assertRaises(RuntimeError) as failure:
                campaign_fence.supervised([sys.executable, '-c', 'raise SystemExit(3)'], log, FakeSession(), poll=0.05)
            self.assertNotIsInstance(failure.exception, campaign_fence.CampaignLost)


@unittest.skipUnless(LINUX, 'needs bash and flock')
class AdmissionProtocolTests(unittest.TestCase):
    """The real admission command, preparation wrapper and runner hold on one
    VM root, run as processes."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        stub_docker(self.root)
        self.directory = self.root / 'q/.campaign'

    def tearDown(self):
        self.folder.cleanup()

    def admit(self, **environment):
        import subprocess
        process = subprocess.Popen(['sh', '-c', campaign_fence.admission_command(str(self.root / 'q'))],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                   env=stub_environment(self.root, **environment))
        return process, process.stdout.readline().split()

    def refused(self, **environment):
        process, line = self.admit(**environment)
        process.stdin.close()
        return line == [] and process.wait(10) == campaign_fence.LOCK_HELD

    def end(self, process):
        process.stdin.close()
        process.wait(10)

    def epoch(self):
        return (self.directory / 'epoch').read_text().strip()

    def test_one_admission_at_a_time_each_with_a_new_epoch(self):
        first, line = self.admit()
        self.assertEqual(line[:2], ['locked', '1'])
        self.assertTrue(self.refused())
        self.assertEqual(self.epoch(), '1')
        self.end(first)
        second, line = self.admit()
        self.assertEqual(line[:2], ['locked', '2'])
        self.end(second)

    def test_a_runner_of_a_lost_admission_is_fenced_whatever_its_revision(self):
        # A and B qualify different revisions, so their checkouts differ: the
        # admission state lives in the VM root and binds them both.
        controller_a, line = self.admit()
        epoch_a = int(line[1])
        self.end(controller_a)
        controller_b, line = self.admit()
        try:
            with self.assertRaises(campaign_fence.CampaignFenced):
                with campaign_fence.held(self.directory, epoch_a):
                    self.fail('a late runner of A measured')
            with campaign_fence.held(self.directory, int(line[1])) as fence:
                fence()
        finally:
            self.end(controller_b)

    def test_a_surviving_runner_keeps_every_admission_out(self):
        import subprocess
        controller_a, line = self.admit()
        runner = subprocess.Popen([sys.executable, '-c', RUNNER, str(ROOT / 'scripts'), str(self.directory), line[1]],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(runner.stdout.readline().strip(), 'running')
            self.end(controller_a)
            self.assertTrue(self.refused())
            self.assertEqual(self.epoch(), '1')
        finally:
            self.end(runner)
        controller_b, line = self.admit()
        self.assertEqual(line[:2], ['locked', '2'])
        self.end(controller_b)

    def test_a_surviving_preparation_keeps_every_admission_out(self):
        import subprocess
        controller_a, line = self.admit()
        checkout = self.root / 'run'
        (checkout / '.fixtures').mkdir(parents=True)
        _, wrapper = release_campaign.fixture_scripts(str(checkout), 'storage-q-abc', 'vm.invalid', 'prepare',
                                                      'n0nce', recreate=False, directory=str(self.directory),
                                                      epoch=int(line[1]))
        (checkout / '.fixtures/prepare-n0nce.sh').write_text(BLOCKING_PREPARATION)
        (checkout / '.fixtures/prepare-n0nce-run.sh').write_text(wrapper)
        preparation = subprocess.Popen(['bash', str(checkout / '.fixtures/prepare-n0nce-run.sh')],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            for _ in range(100):
                if (checkout / '.fixtures/started').exists():
                    break
                time.sleep(0.05)
            else:
                self.fail('the preparation did not start')
            self.end(controller_a)
            self.assertTrue(self.refused())
            self.assertEqual(self.epoch(), '1')
        finally:
            self.end(preparation)
        controller_b, line = self.admit()
        self.assertEqual(line[:2], ['locked', '2'])
        self.end(controller_b)

    def test_a_runner_container_not_yet_locked_keeps_admissions_out(self):
        self.assertTrue(self.refused(DOCKER_IDS='0123456789ab'))
        self.assertFalse((self.directory / 'epoch').exists())

    def test_an_epoch_change_during_a_phase_voids_it(self):
        controller, line = self.admit()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                campaign = Campaign(Path(temporary), {'subject': 'same'})
                with campaign_fence.held(self.directory, int(line[1])) as fence:
                    def measured(path):
                        (path / 'report.json').write_text('measured')
                        # Another admission happens while this phase runs.
                        (self.directory / 'epoch').write_text('99\n')
                    with self.assertRaises(campaign_fence.CampaignFenced):
                        run_vm_campaign.fenced_phase(campaign, fence, 'performance-ab', measured)
                attempt = campaign.state['phases']['performance-ab'][-1]
                self.assertEqual(attempt['status'], 'FAIL')
                self.assertEqual(attempt['failure_type'], 'CampaignFenced')
        finally:
            self.end(controller)

    def test_a_lock_error_that_is_not_contention_is_reported_as_such(self):
        import errno
        import fcntl
        (self.directory).mkdir(parents=True)
        (self.directory / 'lock').write_text('')
        (self.directory / 'epoch').write_text('1\n')
        with patch.object(fcntl, 'flock', side_effect=OSError(errno.EBADF, 'bad descriptor')):
            with self.assertRaises(OSError) as failure:
                with campaign_fence.held(self.directory, 1):
                    self.fail('held')
            self.assertNotIsInstance(failure.exception, campaign_fence.CampaignBusy)
        with patch.object(fcntl, 'flock', side_effect=OSError(errno.EWOULDBLOCK, 'busy')):
            with self.assertRaises(campaign_fence.CampaignBusy):
                with campaign_fence.held(self.directory, 1):
                    self.fail('held')


class MemoryTests(unittest.TestCase):
    def test_the_gcs_peak_and_reserve_must_be_available(self):
        import check_memory
        gib = check_memory.GIB
        self.assertEqual(check_memory.inspect(4 * gib)['status'], 'PASS')
        self.assertEqual(check_memory.inspect(4 * gib - 1)['status'], 'FAIL')
        self.assertEqual(check_memory.GCS_PEAK_BYTES, 2 * gib)
        with tempfile.TemporaryDirectory() as temporary:
            meminfo = Path(temporary) / 'meminfo'
            meminfo.write_text('MemTotal: 16000000 kB\nMemAvailable: 1024 kB\n')
            self.assertEqual(check_memory.available(meminfo), 1024 * 1024)
            for broken in ('MemTotal: 16000000 kB\n', 'MemAvailable: 1024 MB\n', 'MemAvailable: 1024\n',
                           'MemAvailable: 1024 kB\nMemAvailable: 2048 kB\n', 'MemAvailable: -1 kB\n',
                           'MemAvailable: x kB\n'):
                with self.subTest(broken=broken):
                    meminfo.write_text(broken)
                    with self.assertRaises(ValueError):
                        check_memory.available(meminfo)


class Server:
    """A local TCP server that answers every connection with fixed bytes."""

    def __init__(self, reply):
        self.reply = reply
        self.listener = socket.create_server(('127.0.0.1', 0))
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            with connection:
                connection.settimeout(2)
                try:
                    connection.sendall(self.reply)
                    connection.recv(4096)
                except OSError:
                    pass

    def close(self):
        self.listener.close()


BANNERS = {
    'http-503': b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n',
    'http-200': b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n',
    'ftp': b'220 fixture ready\r\n',
    'ssh': b'SSH-2.0-fixture\r\n',
    'smb': b'\x00\x00\x00\x04junk',
}


class TlsFtpServer:
    """A minimal explicit-TLS FTP server: AUTH TLS, login, PBSZ/PROT and one
    passive NLST over TLS, enough for a real handshake on both channels."""

    def __init__(self, certificate, key, data=None):
        import ssl
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(certificate, key)
        self.data_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.data_context.load_cert_chain(*(data or (certificate, key)))
        self.listener = socket.create_server(('127.0.0.1', 0))
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        try:
            connection, _ = self.listener.accept()
        except OSError:
            return
        try:
            self.session(connection)
        except OSError:
            pass
        finally:
            connection.close()

    def session(self, connection):
        def send(line):
            connection.sendall(line.encode() + b'\r\n')

        def receive():
            data = b''
            while not data.endswith(b'\r\n'):
                chunk = connection.recv(1)
                if not chunk:
                    raise OSError('closed')
                data += chunk
            return data.decode().strip()

        send('220 fixture')
        data_listener = None
        while True:
            command = receive().split(' ', 1)[0].upper()
            if command == 'AUTH':
                send('234 TLS')
                connection = self.context.wrap_socket(connection, server_side=True)
            elif command == 'USER':
                send('331 password')
            elif command == 'PASS':
                send('230 logged in')
            elif command in ('PBSZ', 'PROT', 'TYPE'):
                send('200 ok')
            elif command == 'PASV':
                data_listener = socket.create_server(('127.0.0.1', 0))
                port = data_listener.getsockname()[1]
                send(f'227 Entering Passive Mode (127,0,0,1,{port // 256},{port % 256})')
            elif command == 'NLST':
                data, _ = data_listener.accept()
                send('150 listing')
                data = self.data_context.wrap_socket(data, server_side=True)
                data.sendall(b'file\r\n')
                data.unwrap().close()
                send('226 done')
            else:
                send('221 bye')
                return

    def close(self):
        self.listener.close()


def certificate(folder, name):
    """A self-signed certificate for `name`, made with the openssl CLI."""
    import subprocess
    certificate, key = folder / (name + '.crt'), folder / (name + '.key')
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-keyout', str(key),
                    '-out', str(certificate), '-subj', '/CN=' + name, '-addext', 'subjectAltName=DNS:' + name],
                   check=True, capture_output=True)
    return certificate, key


@unittest.skipUnless(shutil.which('openssl'), 'needs the openssl CLI')
class FtpsHandshakeTests(unittest.TestCase):
    """Real TLS handshakes against the FTPS probe. The probe connects to
    127.0.0.1, like a probe after --connect-host, and verifies the certificate
    for the fixture's own name."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        folder = Path(self.folder.name)
        self.trusted = certificate(folder, 'fixture.invalid')
        self.stranger = certificate(folder, 'stranger.invalid')

    def tearDown(self):
        self.folder.cleanup()

    def probe(self, served, ca, name, data=None):
        import check_fixtures
        server = TlsFtpServer(*served, data=data)
        try:
            check_fixtures.ftp_list('127.0.0.1', server.port, 'user', 'secret', tls_ca=ca, tls_name=name)
        finally:
            server.close()

    def test_the_fixture_identity_is_verified_on_another_address(self):
        self.probe(self.trusted, self.trusted[0], 'fixture.invalid')

    def test_an_untrusted_certificate_fails_the_handshake(self):
        import check_fixtures
        with self.assertRaises(check_fixtures.ProbeFailure):
            self.probe(self.stranger, self.trusted[0], 'fixture.invalid')

    def test_a_wrong_certificate_on_the_data_channel_alone_fails(self):
        import check_fixtures
        with self.assertRaises(check_fixtures.ProbeFailure):
            self.probe(self.trusted, self.trusted[0], 'fixture.invalid', data=self.stranger)

    def test_a_certificate_for_another_name_fails(self):
        import check_fixtures
        with self.assertRaises(check_fixtures.ProbeFailure):
            self.probe(self.trusted, self.trusted[0], 'other.invalid')


class ProbeTests(unittest.TestCase):
    """Every application probe fails against a server that only answers a
    banner, an error status or a bare success."""

    def failing(self, banner, probe):
        import check_fixtures
        server = Server(BANNERS[banner])
        try:
            with self.assertRaises((check_fixtures.ProbeFailure, OSError)):
                probe(check_fixtures, server.port)
        finally:
            server.close()

    def test_http_probes_need_their_authenticated_answer(self):
        for banner in ('http-503', 'http-200'):
            with self.subTest(banner=banner):
                self.failing(banner, lambda c, port: c.webdav_propfind(f'http://127.0.0.1:{port}/'))
        self.failing('http-503', lambda c, port: c.s3_head_bucket(f'http://127.0.0.1:{port}'))
        self.failing('http-503', lambda c, port: c.azure_list(f'http://127.0.0.1:{port}/devstoreaccount1'))
        self.failing('http-503', lambda c, port: c.gcs_bucket(f'http://127.0.0.1:{port}'))

    def test_tls_probe_refuses_a_plain_banner(self):
        import ssl
        self.failing('http-200', lambda c, port: c.s3_head_bucket(f'https://127.0.0.1:{port}',
                                                                   ssl.create_default_context()))

    def test_ftp_probes_need_login_and_listing(self):
        self.failing('ftp', lambda c, port: c.ftp_list('127.0.0.1', port, 'user', 'secret'))
        self.failing('ftp', lambda c, port: c.ftp_list('127.0.0.1', port, 'user', 'secret',
                                                       tls_ca=ROOT / 'scripts/check_fixtures.py'))

    @unittest.skipUnless(shutil.which('ssh-keyscan') and shutil.which('sftp'), 'OpenSSH client not available')
    def test_sftp_probe_needs_the_pinned_key_and_a_listing(self):
        self.failing('ssh', lambda c, port: c.sftp_list('127.0.0.1', port, Path('missing-key'), 'SHA256:pin'))

    @unittest.skipUnless(shutil.which('smbclient'), 'smbclient not available')
    def test_smb_probe_needs_a_session_and_a_listing(self):
        self.failing('smb', lambda c, port: c.run_probe(c.smbclient_command('127.0.0.1', port, 'storage'),
                                                        'SMB listing failed'))

    def test_a_failed_container_probe_fails_the_check(self):
        import check_fixtures
        healthy = {service: {'Service': service, 'State': 'running', 'Health': 'healthy'}
                   for service in check_fixtures.SERVICES}
        passing = {service: (lambda: None) for service in check_fixtures.SERVICES}
        with patch.object(check_fixtures, 'containers', return_value=healthy), \
                patch.object(check_fixtures, 'probes', return_value=passing):
            self.assertEqual(check_fixtures.inspect('vm.invalid')['status'], 'PASS')

        def refused():
            raise check_fixtures.ProbeFailure('banner only')
        for service in check_fixtures.SERVICES:
            with self.subTest(service=service), patch.object(check_fixtures, 'containers', return_value=healthy), \
                    patch.object(check_fixtures, 'probes', return_value=dict(passing, **{service: refused})):
                report = check_fixtures.inspect('vm.invalid')
                self.assertEqual(report['status'], 'FAIL')
                self.assertEqual([r['service'] for r in report['results'] if r['status'] == 'FAIL'], [service])
        unhealthy = dict(healthy, webdav={'Service': 'webdav', 'State': 'running', 'Health': 'starting'})
        with patch.object(check_fixtures, 'containers', return_value=unhealthy), \
                patch.object(check_fixtures, 'probes', return_value=passing):
            self.assertEqual(check_fixtures.inspect('vm.invalid')['status'], 'FAIL')
        with patch.object(check_fixtures.subprocess, 'run') as run:
            run.return_value.returncode = 1
            with self.assertRaises(check_fixtures.ProbeFailure):
                check_fixtures.run_probe(['smbclient'], 'SMB listing failed')


if __name__ == '__main__':
    unittest.main()
