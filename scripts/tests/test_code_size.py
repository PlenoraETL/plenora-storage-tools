import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from code_size import check_budget, count_python, count_rust, measure


class CodeSizeTests(unittest.TestCase):
    def test_test_bodies_and_comment_braces_do_not_change_product_count(self):
        product = 'pub fn value() -> u8 { 1 }\n'
        tests = '#[cfg(test)]\nmod tests { /* } */ fn test() { let s = r#"}"#; } }\n'
        self.assertEqual(count_rust(product + tests)['code_lines'], count_rust(product)['code_lines'])

    def test_docstrings_do_not_enter_python_code_denominator(self):
        self.assertEqual(count_python('"""description\ncontinued"""\nx = "# data"\n')['code_lines'], 1)

    def test_external_tests_excluded_but_fork_visible_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / 'crates/plenora-storage-core/src'
            folder.mkdir(parents=True)
            (folder / 'lib.rs').write_text('pub fn value() {}\n#[cfg(test)]\n#[path = "lib_tests.rs"]\nmod tests;\n')
            (folder / 'lib_tests.rs').write_text('#[test]\nfn test() {}\n')
            fork = root / 'crates/plenora-smb2/src'
            fork.mkdir(parents=True)
            (fork / 'lib.rs').write_text('pub fn fork() {}\n#[cfg(test)]\npub(crate) mod helper;\n')
            (fork / 'helper.rs').write_text('fn fixture() {}\n' * 50)
            report = measure(root)
            self.assertNotIn('crates/plenora-storage-core/src/lib_tests.rs', report['files'])
            self.assertNotIn('crates/plenora-smb2/src/helper.rs', report['files'])
            self.assertEqual(report['areas']['plenora-smb2']['code_lines'], 3)
            self.assertEqual(report['owned_total'], report['areas']['plenora-storage-core'])
            (root / 'target/generated').mkdir(parents=True)
            (root / 'target/generated/large.rs').write_text('pub fn ignored() {}\n' * 100)
            self.assertEqual(report, measure(root))

    def test_budget_rejects_growth_new_areas_and_single_file_concentration(self):
        report = {'areas': {'core': {'code_lines': 10, 'physical_lines': 20}},
                  'files': {'lib.rs': {'area': 'core', 'code_lines': 10, 'physical_lines': 20}}}
        budget = {'areas': {'core': {'code_lines': 10, 'physical_lines': 20, 'max_file_code_lines': 10}}}
        self.assertEqual(check_budget(report, budget), [])
        for metric in ('code_lines', 'physical_lines'):
            grown = copy.deepcopy(report)
            grown['areas']['core'][metric] += 1
            self.assertTrue(check_budget(grown, budget))
        concentrated = copy.deepcopy(budget)
        concentrated['areas']['core']['max_file_code_lines'] = 9
        self.assertTrue(check_budget(report, concentrated))
        self.assertTrue(check_budget(report, {'areas': {}}))
