"""A release must not substitute default-mode or unrelated artifact evidence."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from spooled_evidence import EXTENDED, PROVIDERS, EXPECTED_TESTS, validate_reports


class SpooledEvidenceTests(unittest.TestCase):
    def test_rejects_substituted_incomplete_and_failed_evidence(self):
        cli = {'spool_uploads': True, 'binary_sha256': 'a' * 64,
               'results': [{'provider': name, 'operations': 7, 'status': 'PASS'} for name in sorted(EXTENDED)]}
        faults = {'spool_uploads': True, 'binary_sha256': 'a' * 64,
                  'results': [{'name': name, 'status': 'PASS'} for name in sorted(EXPECTED_TESTS)]}
        sdk = {'spool_uploads': True, 'version': '2.1.0', 'wheel': 'sdk.whl', 'wheel_sha256': 'b' * 64,
               'results': [{'provider': name, 'operations': 7, 'status': 'PASS', 'async_stat': 'PASS'}
                           for name in sorted(PROVIDERS)]}
        reports = [cli, faults, sdk]
        names = ['spooled-qualification.json', 'spooled-regressions.json', 'spooled-python-qualification.json']
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)

            def verify(documents):
                for name, document in zip(names, documents, strict=True):
                    (folder / name).write_text(json.dumps(document), encoding='utf-8')
                return validate_reports(folder, '2.1.0', 'a' * 64, 'sdk.whl', 'b' * 64)

            self.assertEqual(len(verify(reports)), 3)
            variants = []
            for index, report in enumerate(reports):
                for field, value in [('spool_uploads', False), ('spool_uploads', 1),
                                     ('results', report['results'][:-1]),
                                     ('results', report['results'] + [report['results'][0]])]:
                    variant = copy.deepcopy(reports)
                    variant[index][field] = value
                    variants.append(variant)
                variant = copy.deepcopy(reports)
                variant[index]['results'][0]['status'] = 'FAIL'
                variants.append(variant)
            for index, field, value in [(0, 'binary_sha256', 'c' * 64), (1, 'binary_sha256', 'c' * 64),
                                        (2, 'wheel_sha256', 'c' * 64), (2, 'wheel', 'another.whl'),
                                        (2, 'version', '2.0.1')]:
                variant = copy.deepcopy(reports)
                variant[index][field] = value
                variants.append(variant)
            for index, field, value in [(0, 'operations', 6), (2, 'operations', 6), (2, 'async_stat', 'SKIP')]:
                variant = copy.deepcopy(reports)
                variant[index]['results'][0][field] = value
                variants.append(variant)
            for index, variant in enumerate(variants):
                with self.subTest(variant=index), self.assertRaises(ValueError):
                    verify(variant)
            verify(reports)
            (folder / names[1]).unlink()
            with self.assertRaises(FileNotFoundError):
                validate_reports(folder, '2.1.0', 'a' * 64, 'sdk.whl', 'b' * 64)
