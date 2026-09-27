from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_comments import violations


class CommentTests(unittest.TestCase):
    def test_rust_strings_are_not_debt_but_real_comments_are(self):
        source = 'let text = r###"// TODO }"###;\n// TODO document failure\n/* FIXME #42: limit */\n'
        self.assertEqual(violations(Path('lib.rs'), source), [2])

    def test_python_docstrings_and_comments_require_references(self):
        source = '"""TODO define recovery"""\nvalue = "FIXME"\n# HACK STORE-42: fixture limit\n'
        self.assertEqual(violations(Path('sdk.py'), source), [1])
