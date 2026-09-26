import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location('fuzz_parsers', Path(__file__).parents[1] / 'fuzz_parsers.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FuzzEvidenceTests(unittest.TestCase):
    def test_different_product_dependency_version_is_rejected(self):
        product = {'package': [{'name': 'quick-xml', 'version': '1', 'checksum': 'abc'}]}
        MODULE.check_lock_alignment(product, product)
        with self.assertRaises(ValueError):
            MODULE.check_lock_alignment(product, {'package': [{'name': 'quick-xml', 'version': '2'}]})

    def test_crash_timeout_and_missing_instrumentation_fail_closed(self):
        completed = '#120 DONE cov: 54 ft: 98\nstat::number_of_executed_units: 120\n'
        self.assertEqual(MODULE.stats(completed, 0)['status'], 'PASS')
        for log, code in [(completed, 77), (completed, -1), ('', 0),
                          ('stat::number_of_executed_units: 120', 0),
                          ('cov: 54\nstat::number_of_executed_units: 0', 0)]:
            self.assertEqual(MODULE.stats(log, code)['status'], 'FAIL')
