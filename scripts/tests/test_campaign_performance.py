import json
import math
from pathlib import Path
from statistics import median
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campaign_state import Campaign
from check_performance import compare
from fixture_connections import BUFFERED, PROVIDERS
from performance_order import SCHEME, paired_order, validate_pairing
import qualify_transfers
import release_campaign
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


def measured(runs, rounds, drift):
    """Reports of a run executing `runs` (round, provider, role) in order: the
    workers of one run are concurrent, runs are sequential, and the
    environment slows down linearly by `drift` over the whole run."""
    reports = {name: report(name, rounds) for name in BINARIES}
    for clock, (iteration, provider, name) in enumerate(runs):
        elapsed = 0.2 * (1 + drift * clock / len(runs))
        for _ in range(WORKERS):
            reports[name]['results'].append({
                'provider': provider, 'round': iteration, 'status': 'PASS', 'payload_bytes': 1024**2,
                'mode': 'buffered_roundtrip' if provider in BUFFERED else 'streaming_roundtrip',
                'measurements': [{'operation': operation, 'status': 'PASS', 'elapsed_seconds': elapsed,
                                  'peak_rss_bytes': 12 * 1024**2} for operation in OPERATIONS]})
    return reports['baseline'], reports['candidate']


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


def statistics(report_, provider, operation):
    """Median and p95 exactly as check_performance computes them."""
    values = sorted(m['elapsed_seconds'] for row in report_['results'] if row['provider'] == provider
                    for m in row['measurements'] if m['operation'] == operation)
    return median(values), values[math.ceil(len(values) * .95) - 1]


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

    def test_linear_drift_is_balanced_in_the_compared_statistics(self):
        drift = 0.5
        baseline, candidate = measured(alternated(ROUNDS), ROUNDS, drift)
        pair(baseline, candidate)
        self.assertEqual(compare(baseline, candidate, BINARIES['candidate'], POLICY)['status'], 'PASS')
        # Runs are sequential, so at any order statistic the samples of the two
        # binaries are at most one run apart in time: for every provider,
        # operation and compared statistic the residual difference is at most
        # one run's share of the drift (0.02 % here), against half of the
        # whole drift when the binaries run one after the other.
        step = 0.2 * drift / len(alternated(ROUNDS))
        for provider in PROVIDERS:
            for operation in set(OPERATIONS):
                for old, new in zip(statistics(baseline, provider, operation),
                                    statistics(candidate, provider, operation)):
                    self.assertLessEqual(abs(new - old), step + 1e-12, (provider, operation))
        baseline, candidate = measured(sequential(ROUNDS), ROUNDS, drift)
        self.assertEqual(compare(baseline, candidate, BINARIES['candidate'], POLICY)['status'], 'FAIL')

    def test_a_real_regression_still_fails_when_alternated(self):
        baseline, candidate = measured(alternated(ROUNDS), ROUNDS, drift=0.0)
        for row in candidate['results']:
            for measure in row['measurements']:
                measure['elapsed_seconds'] *= 1.3
        pair(baseline, candidate)
        self.assertEqual(compare(baseline, candidate, BINARIES['candidate'], POLICY)['status'], 'FAIL')

    def test_paired_metadata_is_validated_never_read_as_absent(self):
        baseline, candidate = measured(alternated(ROUNDS), ROUNDS, drift=0.0)
        self.assertFalse(validate_pairing(baseline, candidate, PROVIDERS))
        pair(baseline, candidate)
        self.assertTrue(validate_pairing(baseline, candidate, PROVIDERS))
        for broken in (None, {}, [], 'ABBA'):
            with self.subTest(broken=broken):
                altered = dict(candidate, paired_measurement=broken)
                with self.assertRaises(ValueError):
                    compare(baseline, altered, BINARIES['candidate'], POLICY)
        for field, value in (('order', paired_order(ROUNDS, PROVIDERS)[::-1]), ('order', []),
                             ('order_scheme', 'ABAB'), ('role', 'baseline'), ('partner_campaign_id', 'x')):
            with self.subTest(field=field):
                altered = dict(candidate, paired_measurement=dict(candidate['paired_measurement'], **{field: value}))
                with self.assertRaises(ValueError):
                    compare(baseline, altered, BINARIES['candidate'], POLICY)
        with self.assertRaises(ValueError):
            compare(baseline, {k: v for k, v in candidate.items() if k != 'paired_measurement'},
                    BINARIES['candidate'], POLICY)


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
        # In every slot all workers of the first binary end before any worker
        # of the second binary starts.
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


class FixtureResetTests(unittest.TestCase):
    def test_reset_runs_under_the_lock_and_checks_before_and_after(self):
        script, wrapper = release_campaign.fixture_scripts('/srv/root', 'storage-q-abc', 'vm.invalid',
                                                           'fixture-reset', '3', recreate=True)
        self.assertIn(f'flock -n -E {release_campaign.LOCK_HELD} .fixtures/campaign/campaign.lock', wrapper)
        self.assertIn('test -f .fixtures/fixture-reset-3.exit || echo "$code"', wrapper)
        steps = script.splitlines()
        runner = next(i for i, line in enumerate(steps) if "grep -q '^storage-q-abc-campaign-'" in line)
        archive = next(i for i, line in enumerate(steps) if 'logs --no-color' in line)
        recreate = steps.index('export PLENORA_FIXTURE_RECREATE=1')
        check = next(i for i, line in enumerate(steps) if 'check_fixtures.py' in line)
        self.assertLess(runner, archive)
        self.assertLess(archive, recreate)
        self.assertLess(recreate, check)
        self.assertIn(f'exit {release_campaign.RUNNER_ACTIVE}', script)
        plain, _ = release_campaign.fixture_scripts('/srv/root', 'storage-q-abc', 'vm.invalid', 'prepare', '1',
                                                    recreate=False)
        self.assertNotIn('PLENORA_FIXTURE_RECREATE', plain)
        self.assertNotIn('check_fixtures', plain)


    def test_reset_is_recorded_only_when_every_fixture_answers(self):
        import check_fixtures
        healthy = {service: {'Service': service, 'State': 'running', 'Health': 'healthy'}
                   for service in check_fixtures.SERVICES}
        with patch.object(check_fixtures, 'containers', return_value=healthy),                 patch.object(check_fixtures, 'probe', return_value=True):
            self.assertEqual(check_fixtures.inspect()['status'], 'PASS')
        unhealthy = dict(healthy, webdav={'Service': 'webdav', 'State': 'running', 'Health': 'starting'})
        with patch.object(check_fixtures, 'containers', return_value=unhealthy),                 patch.object(check_fixtures, 'probe', return_value=True):
            self.assertEqual(check_fixtures.inspect()['status'], 'FAIL')
        missing = {key: value for key, value in healthy.items() if key != 'smb'}
        with patch.object(check_fixtures, 'containers', return_value=missing),                 patch.object(check_fixtures, 'probe', return_value=True):
            self.assertEqual(check_fixtures.inspect()['status'], 'FAIL')
        with patch.object(check_fixtures, 'containers', return_value=healthy),                 patch.object(check_fixtures, 'probe', side_effect=lambda port, kind: port != 2122):
            report = check_fixtures.inspect()
        self.assertEqual(report['status'], 'FAIL')
        self.assertEqual([r['service'] for r in report['results'] if r['status'] == 'FAIL'], ['ftps'])


if __name__ == '__main__':
    unittest.main()
