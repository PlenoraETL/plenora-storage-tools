import importlib.util
from pathlib import Path
import tempfile
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

    def test_every_target_has_a_seed_directory_harness_and_boundary(self):
        root = Path(__file__).parents[2]
        manifest = (root / 'fuzz/Cargo.toml').read_text(encoding='utf-8')
        for target in MODULE.TARGETS:
            with self.subTest(target=target):
                self.assertTrue(any((root / 'fuzz/seeds' / target).iterdir()))
                self.assertIn(f'name = "{target}"', manifest)
                self.assertTrue((root / 'fuzz/fuzz_targets' / f'{target}.rs').is_file())
        with tempfile.TemporaryDirectory() as directory:
            for target in MODULE.TARGETS:
                corpus = Path(directory) / target
                corpus.mkdir()
                MODULE.boundary_seeds(target, corpus)
                self.assertTrue(any(corpus.iterdir()), target)
