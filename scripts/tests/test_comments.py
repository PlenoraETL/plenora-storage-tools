from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_comments import violations


class CommentTests(unittest.TestCase):
    def test_stubs_and_development_history(self):
        self.assertEqual(violations(Path('sdk.pyi'), '# post-review: moved here\ndef close() -> None: ...\n'), [1])
        self.assertEqual(violations(Path('a.rs'), '// Reject old protocol versions to preserve framing.\n'), [])

    def test_hash_formats_preserve_strings_and_check_real_comments(self):
        for suffix in ('.sh', '.ps1', '.toml', '.yml', '.yaml'):
            with self.subTest(suffix=suffix):
                source = 'value = "# TODO data"\n# FIXME finish\n# TODO STORE-42: reconcile\n'
                self.assertEqual(violations(Path('a' + suffix), source), [2])

    def test_multiline_scalars_and_here_documents_are_data(self):
        samples = {
            '.sh': 'cat <<\'EOF\'\n# TODO payload\nEOF\n# TODO real\n',
            '.ps1': '$x = @\'\n# TODO payload\n\'@\n# TODO real\n',
            '.toml': 'value = """\n# TODO payload\n"""\n# TODO real\n',
            '.yml': 'run: |\n  # TODO embedded data\n  echo done\n# TODO real\n',
        }
        for suffix, source in samples.items():
            with self.subTest(suffix=suffix):
                self.assertEqual(violations(Path('a' + suffix), source), [4])

    def test_shell_parameter_expansion_and_powershell_block_comments(self):
        self.assertEqual(violations(Path('a.sh'), 'echo ${name#TODO}\necho foo#FIXME\n'), [])
        self.assertEqual(violations(Path('a.ps1'), '<#\nTODO finish\n#>\n'), [1])

    def test_rust_strings_are_not_debt_but_real_comments_are(self):
        source = 'let text = r###"// TODO }"###;\n// TODO document failure\n/* FIXME #42: limit */\n'
        self.assertEqual(violations(Path('lib.rs'), source), [2])

    def test_python_docstrings_and_comments_require_references(self):
        source = '"""TODO define recovery"""\nvalue = "FIXME"\n# HACK STORE-42: fixture limit\n'
        self.assertEqual(violations(Path('sdk.py'), source), [1])
