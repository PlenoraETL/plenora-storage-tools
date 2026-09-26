import copy
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_installed_sdk import enforce_coverage, measured_coverage, test_count


class PythonCoverageTests(unittest.TestCase):
    def test_empty_skipped_or_failed_tests_cannot_qualify_a_wheel(self):
        self.assertEqual(test_count('Ran 25 tests in 1.0s\nOK', 0), 25)
        for log, status in [('Ran 0 tests in 0.0s\nOK', 0), ('', 0),
                            ('Ran 25 tests in 1.0s\nOK (skipped=1)', 0),
                            ('Ran 25 tests in 1.0s', 1)]:
            with self.assertRaises(ValueError):
                test_count(log, status)

    def test_missing_or_wrong_wheel_source_cannot_count_as_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'plenora_storage/__init__.py'
            source.parent.mkdir()
            source.write_bytes(b'x = 1\n')
            wheel = root / 'fixture.whl'
            with zipfile.ZipFile(wheel, 'w') as archive:
                archive.writestr('plenora_storage/__init__.py', source.read_bytes())
            document = {'meta': {'branch_coverage': True, 'version': '7.16.1'},
                        'files': {str(source): {'summary': {'num_statements': 1, 'covered_lines': 1,
                                                           'num_branches': 0, 'covered_branches': 0}}}}
            self.assertEqual(measured_coverage(document, wheel)['plenora_storage/__init__.py']['line_percent'], 100)
            missing = copy.deepcopy(document)
            missing['files'] = {}
            with self.assertRaises(ValueError):
                measured_coverage(missing, wheel)
            source.write_bytes(b'x = 2\n')
            with self.assertRaises(ValueError):
                measured_coverage(document, wheel)

    def test_one_good_module_cannot_hide_an_uncovered_module(self):
        with self.assertRaises(ValueError):
            enforce_coverage({'good': {'line_percent': 100, 'branch_percent': 100},
                              'bad': {'line_percent': 97, 'branch_percent': 100}},
                             {'line_percent': 98, 'branch_percent': 95})
