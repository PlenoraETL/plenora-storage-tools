"""Adversarial cycle and work-budget oracles; discovered by product-quality CI."""
from copy import deepcopy
from pathlib import Path
import sys
import json
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from soak_policy import POLICY, analyze, validate_calibration, validate_cycles
from release_evidence import validate_soak
from scripts.tests.test_release_evidence import soak, WHEEL


class CyclePolicyTests(unittest.TestCase):
    def real_replay(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/soak-campaign-4.0.0-resources.json').read_text())
        report = fixture['report']
        report['policy'] = deepcopy(POLICY)
        report['after_warmup'] = deepcopy(report['samples'][0])
        report['warmup_lifecycle'] = deepcopy(report['lifecycle'][0])
        return report

    def test_real_campaign_resources_pass_with_explicit_replay_reference(self):
        report = self.real_replay()
        validate_cycles(report)
        self.assertEqual(analyze(report['samples'])['rss_bytes']['window_growth'], 1306624)
        self.assertGreater(report['after_close']['rss_bytes'] - report['before_open']['rss_bytes'], 16 * 1024**2)

    def test_real_lifecycle_accumulation_fails_in_each_mode_and_phase(self):
        for mode in range(2):
            for phase in ('close', 'cancel'):
                for key, step in [('rss_bytes', 1024**2), ('file_descriptors', 1), ('threads', 1), ('temporary_files', 1)]:
                    report = self.real_replay()
                    for i, cycle in enumerate(report['lifecycle']):
                        cycle[mode][phase + '_after'][key] += (i + 1) * step
                    with self.subTest(mode=mode, phase=phase, key=key), self.assertRaises(ValueError):
                        validate_cycles(report)

    def test_warmup_references_required_and_temporary_files_always_zero(self):
        for mutate in (lambda r: r.pop('after_warmup'), lambda r: r.pop('warmup_lifecycle'),
                       lambda r: r['warmup_lifecycle'].pop(),
                       lambda r: r['after_warmup'].update(temporary_files=1),
                       lambda r: r['warmup_lifecycle'][0]['close_after'].update(temporary_files=1),
                       lambda r: r['policy'].update(id='storage-sdk-cycles-v1')):
            report = self.real_replay()
            mutate(report)
            with self.assertRaises((KeyError, ValueError)):
                validate_cycles(report)

    def test_stable_pool_allowance_and_final_accumulation(self):
        report = soak()
        for cycle in report['lifecycle']:
            for phase in ('close', 'cancel'):
                cycle[0][phase + '_after']['threads'] += 2
                cycle[0][phase + '_after']['file_descriptors'] += 2
        validate_cycles(report)
        for label in ('after_close', 'after_cancel'):
            for key, step in [('rss_bytes', 16 * 1024**2 + 1), ('threads', 3),
                              ('file_descriptors', 3), ('temporary_files', 1)]:
                bad = deepcopy(report)
                bad[label][key] += step
                with self.subTest(label=label, key=key), self.assertRaises(ValueError):
                    validate_cycles(bad)

    def test_calibration_uses_fixed_warm_reference(self):
        samples = soak()['samples']
        normal = dict(status='PASS', after_warmup=samples[0], samples=samples, analysis=analyze(samples))
        control = dict(status='FAIL', injected_bytes_per_cycle=1024**2,
                       samples=[dict(row, rss_bytes=row['rss_bytes']+i*1024**2) for i, row in enumerate(samples)])
        report = dict(status='PASS', policy=POLICY, normal=[deepcopy(normal) for _ in range(3)], leak_control=control)
        for run in report['normal']:
            for row in run['samples']:
                row['threads'] += 2
                row['file_descriptors'] += 2
            # Avoid sharing the synthetic reference with a measured sample.
            run['after_warmup'] = dict(soak()['samples'][0])
            run['analysis'] = analyze(run['samples'])
        validate_calibration(report)
        for key in ('threads', 'file_descriptors', 'temporary_files'):
            bad = deepcopy(report)
            bad['normal'][0]['samples'][20][key] += 1
            bad['normal'][0]['analysis'] = analyze(bad['normal'][0]['samples']) if key != 'temporary_files' else {}
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_calibration(bad)

    def test_growth_that_fits_old_absolute_budget_is_rejected(self):
        samples = soak()['samples']
        for key, step in [('rss_bytes', 1024**2), ('file_descriptors', 1), ('threads', 1), ('temporary_files', 1)]:
            rows = deepcopy(samples)
            for i, row in enumerate(rows):
                row[key] += i * step
            with self.subTest(resource=key), self.assertRaises(ValueError):
                analyze(rows)

    def test_matrix_lifecycle_summary_and_policy_cannot_be_omitted_or_weakened(self):
        mutations = [lambda r: r['cycle_matrix'][17].pop(),
                     lambda r: r['cycle_matrix'][17].append(r['cycle_matrix'][17][0]),
                     lambda r: r['lifecycle'].pop(),
                     lambda r: r['lifecycle'][17][0].update(cancel_phase='pre-cancelled'),
                     lambda r: r['lifecycle'][17][0]['close_after'].update(file_descriptors=13),
                     lambda r: r['lifecycle'][17][0]['cancel_after'].update(temporary_files=1),
                     lambda r: r['after_close'].update(threads=7),
                     lambda r: r['samples'][17].update(rss_bytes=float('nan')),
                     lambda r: r['policy']['window_growth'].update(rss_bytes=10**12),
                     lambda r: r['analysis']['rss_bytes'].update(window_growth=1),
                     lambda r: r['cycle_matrix'][17][0]['resources'].update(rss_bytes=10**12)]
        for mutate in mutations:
            report = soak()
            mutate(report)
            with self.assertRaises((ValueError, KeyError)):
                validate_soak(report, WHEEL, report['version'])

    def test_control_must_detect_real_trend_not_invalid_or_missing_samples(self):
        samples = soak()['samples']
        normal = dict(status='PASS', after_warmup=samples[0], samples=samples, analysis=analyze(samples))
        control = dict(status='FAIL', injected_bytes_per_cycle=1024**2,
                       samples=[dict(row, rss_bytes=row['rss_bytes']+i*1024**2) for i,row in enumerate(samples)])
        report = dict(status='PASS', policy=POLICY, normal=[normal]*3, leak_control=control)
        validate_calibration(report)
        for bad in (samples, samples[:-1], [dict(row, rss_bytes=-1) for row in samples]):
            altered = deepcopy(report)
            altered['leak_control']['samples'] = bad
            with self.assertRaises(ValueError):
                validate_calibration(altered)


if __name__ == '__main__':
    unittest.main()
