from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rust_source import inline_test_modules, mask_noncode
from check_test_layout import check


class RustLayoutTests(unittest.TestCase):
    def test_literals_nested_comments_and_lifetimes_do_not_change_boundaries(self):
        source = '''// #[cfg(test)] mod fake { }
/* nested /* { */ } */
fn borrow<'a>(x: &'a str) -> &'a str { x }
#[cfg(test)]
mod tests {
    const RAW: &str = r###"} { // '"###;
    const JSON: &str = "{\\"key\\": 1}";
    const CHAR: char = '}';
}
fn after() {}
'''
        modules = inline_test_modules(source)
        self.assertEqual(len(modules), 1)
        start, opening, end, name = modules[0]
        self.assertEqual(name, 'tests')
        self.assertTrue(source[end:].startswith('\nfn after'))
        self.assertIn("borrow<'a>", mask_noncode(source))
        self.assertEqual(source.count('\n'), mask_noncode(source).count('\n'))

    def test_test_named_product_file_is_not_excluded_without_cfg_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'crates/plenora-storage-fixture/src'
            source.mkdir(parents=True)
            (source / 'lib.rs').write_text('fn product() {}\n')
            (source / 'lib_tests.rs').write_text('#[test]\nfn test() {}\n')
            with self.assertRaises(ValueError):
                check(root)
            (source / 'lib.rs').write_text('#[cfg(test)]\n#[path = "lib_tests.rs"]\nmod tests;\n')
            check(root)

    def test_inline_tests_and_truncated_literals_are_rejected(self):
        for value in ['/* comment', 'r##"unfinished', '"unfinished']:
            with self.assertRaises(ValueError):
                mask_noncode(value)
        self.assertEqual(len(inline_test_modules('#[cfg(test)]\nmod tests { #[test] fn a() {} }')), 1)
        self.assertEqual(len(inline_test_modules('#[cfg(test)]\npub(crate) mod tests { fn a() {} }')), 1)
