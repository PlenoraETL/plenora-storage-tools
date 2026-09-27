import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from summarize_coverage import apply_thresholds, summarize


def source(crate, covered, count):
    return {'filename': f'/workspace/crates/{crate}/src/lib.rs',
            'summary': {'lines': {'count': count, 'covered': covered}}}


class CoverageTests(unittest.TestCase):
    def test_per_crate_gate_rejects_missing_and_undercovered_crates(self):
        report = summarize({'data': [{'files': [source('plenora-smb2', 999, 1000),
                                               source('plenora-storage-core', 8599, 10000)]}]})
        with self.assertRaises(ValueError):
            apply_thresholds(report, {'plenora-storage-core': 86})
        result = apply_thresholds(report, {'plenora-storage-core': 86, 'plenora-smb2': 86})
        self.assertEqual(result['threshold_status'], 'FAIL')
        self.assertEqual(result['thresholds']['plenora-smb2']['status'], 'PASS')
        # Compare exact counts, not rounded display percentages.
        report['crates']['plenora-storage-core']['covered'] = 8600
        self.assertEqual(apply_thresholds(report, {'plenora-storage-core': 86, 'plenora-smb2': 86})['threshold_status'], 'PASS')
    def test_fork_cannot_hide_uncovered_product_code(self):
        report = summarize({'data': [{'files': [source('plenora-smb2', 900, 1000),
                                               source('plenora-storage-core', 1, 10)]}]})
        self.assertEqual(report['product_excluding_smb_fork']['percent'], 10)
        self.assertEqual(report['crates']['plenora-smb2']['percent'], 90)

    def test_empty_or_duplicated_evidence_is_rejected(self):
        with self.assertRaises(ValueError):
            summarize({'data': [{'files': []}]})
        item = source('plenora-storage-core', 1, 10)
        with self.assertRaises(ValueError):
            summarize({'data': [{'files': [item, item]}]})
