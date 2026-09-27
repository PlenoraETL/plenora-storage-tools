import copy
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qualify_local_faults import EXPECTED_AXES, validate_report


class LocalFaultEvidenceTests(unittest.TestCase):
    def test_rejects_failed_incomplete_rebound_or_incorrect_evidence(self):
        report = {'schema_version': 1, 'status': 'PASS', 'binary_sha256': 'a' * 64,
                  'results': [{'name': name, 'status': 'PASS', 'category': axes[0],
                               'phase': axes[1], 'remote_effect': axes[2], 'retry': {'kind': 'never'}}
                              for name, axes in EXPECTED_AXES.items()]}
        validate_report(report, 'a' * 64)
        variants = []
        for status in ('FAIL', 'RUNNING'):
            variants.append(dict(report, status=status))
        variants.append(dict(report, binary_sha256='b' * 64))
        variants.append(dict(report, results=report['results'][:-1]))
        variants.append(dict(report, results=report['results'] + [report['results'][0]]))
        for field, value in [('status', 'FAIL'), ('phase', 'commit'),
                             ('remote_effect', 'unknown'), ('retry', {'kind': 'immediate'})]:
            bad = copy.deepcopy(report)
            bad['results'][0][field] = value
            variants.append(bad)
        for bad in variants:
            with self.subTest(report=bad), self.assertRaises(ValueError):
                validate_report(bad, 'a' * 64)
