"""Adversarial cycle and work-budget oracles; discovered by product-quality CI."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from soak_policy import POLICY, analyze, validate_calibration
from release_evidence import validate_soak
from test_release_evidence import soak, WHEEL


class CyclePolicyTests(unittest.TestCase):
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
                     lambda r: r['lifecycle'][17][0]['close_after'].update(file_descriptors=11),
                     lambda r: r['lifecycle'][17][0]['cancel_after'].update(temporary_files=1),
                     lambda r: r['after_close'].update(threads=5),
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
        normal = dict(status='PASS', samples=samples, analysis=analyze(samples))
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
