import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campaign_state import Campaign
from check_performance import compare
from fixture_connections import BUFFERED, PROVIDERS
from qualify_transfers import PAIRED_ORDER, paired_order
import run_vm_campaign

ROOT = Path(__file__).resolve().parents[2]
POLICY = json.loads((ROOT / 'scripts/performance-policy.json').read_text())
BINARIES = {'baseline': 'a' * 64, 'candidate': 'b' * 64}
ENVIRONMENT = {'machine': 'x86_64', 'kernel': 'test', 'cpu_count': 8, 'cpu_model': 'test', 'fixture_sha256': 'c' * 64}
OPERATIONS = ('test', 'put', 'get', 'stat', 'copy', 'delete', 'delete')


def report(name, rounds):
    return {'schema_version': 1, 'binary_sha256': BINARIES[name], 'platform': 'linux',
            'campaign_id': str(uuid.uuid4()), 'environment': ENVIRONMENT, 'source_revision': 'd' * 40,
            'dirty': False, 'payload_bytes': 1024**2, 'workers': 4, 'rounds': rounds, 'spool_uploads': False,
            'rss_limit_bytes': 256 * 1024**2, 'status': 'PASS', 'results': []}


def measured(slots, rounds, drift):
    """Reports of a run executing `slots` in order, on an environment that
    slows down linearly by `drift` over the whole run."""
    reports = {name: report(name, rounds) for name in BINARIES}
    total = len(slots) * 4 * len(OPERATIONS)
    clock = 0
    for iteration, provider, name in slots:
        for _ in range(4):
            measurements = []
            for operation in OPERATIONS:
                elapsed = 0.2 * (1 + drift * clock / total)
                clock += 1
                measurements.append({'operation': operation, 'status': 'PASS', 'elapsed_seconds': elapsed,
                                     'peak_rss_bytes': 12 * 1024**2})
            reports[name]['results'].append({
                'provider': provider, 'round': iteration, 'status': 'PASS', 'payload_bytes': 1024**2,
                'mode': 'buffered_roundtrip' if provider in BUFFERED else 'streaming_roundtrip',
                'measurements': measurements})
    return reports['baseline'], reports['candidate']


def sequential(rounds):
    return [(r, p, name) for name in ('baseline', 'candidate') for r in range(rounds) for p in PROVIDERS]


def alternated(rounds):
    slots = []
    for slot in paired_order(rounds, PROVIDERS):
        first = slot['first']
        second = 'candidate' if first == 'baseline' else 'baseline'
        slots += [(slot['round'], slot['provider'], first), (slot['round'], slot['provider'], second)]
    return slots


def pair(baseline, candidate, order):
    for name, own, other in (('baseline', baseline, candidate), ('candidate', candidate, baseline)):
        own['paired_measurement'] = {'role': name, 'order_scheme': PAIRED_ORDER,
                                     'partner_campaign_id': other['campaign_id'], 'order': order}


class PairedOrderTests(unittest.TestCase):
    def test_order_is_abba_deterministic_and_balanced(self):
        order = paired_order(30, PROVIDERS)
        self.assertEqual(order, paired_order(30, PROVIDERS))
        self.assertEqual(len(order), 30 * len(PROVIDERS))
        self.assertEqual([slot['first'] for slot in order[:8]],
                         ['baseline', 'candidate', 'candidate', 'baseline'] * 2)
        firsts = [slot['first'] for slot in order]
        self.assertLessEqual(abs(firsts.count('baseline') - firsts.count('candidate')), 2)
        self.assertEqual([(slot['round'], slot['provider']) for slot in order],
                         [(r, p) for r in range(30) for p in PROVIDERS])

    def test_monotonic_drift_fails_sequential_runs_and_not_alternated_ones(self):
        rounds = 10
        baseline, candidate = measured(sequential(rounds), rounds, drift=0.5)
        self.assertEqual(compare(baseline, candidate, BINARIES['candidate'], POLICY)['status'], 'FAIL')
        baseline, candidate = measured(alternated(rounds), rounds, drift=0.5)
        pair(baseline, candidate, paired_order(rounds, PROVIDERS))
        self.assertEqual(compare(baseline, candidate, BINARIES['candidate'], POLICY)['status'], 'PASS')

    def test_a_real_regression_still_fails_when_alternated(self):
        rounds = 10
        baseline, candidate = measured(alternated(rounds), rounds, drift=0.0)
        for row in candidate['results']:
            for measure in row['measurements']:
                measure['elapsed_seconds'] *= 1.3
        pair(baseline, candidate, paired_order(rounds, PROVIDERS))
        self.assertEqual(compare(baseline, candidate, BINARIES['candidate'], POLICY)['status'], 'FAIL')

    def test_paired_reports_must_describe_the_same_run(self):
        rounds = 5
        baseline, candidate = measured(alternated(rounds), rounds, drift=0.0)
        pair(baseline, candidate, paired_order(rounds, PROVIDERS))
        candidate['paired_measurement']['order'] = paired_order(rounds, PROVIDERS)[::-1]
        with self.assertRaises(ValueError):
            compare(baseline, candidate, BINARIES['candidate'], POLICY)
        del candidate['paired_measurement']
        with self.assertRaises(ValueError):
            compare(baseline, candidate, BINARIES['candidate'], POLICY)


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

    def test_performance_is_one_paired_phase(self):
        self.assertIn('performance-ab', run_vm_campaign.PHASES)
        self.assertNotIn('performance-baseline', run_vm_campaign.PHASES)
        self.assertNotIn('performance-candidate', run_vm_campaign.PHASES)
        self.assertEqual(run_vm_campaign.PERFORMANCE_ORDER, PAIRED_ORDER)


if __name__ == '__main__':
    unittest.main()
