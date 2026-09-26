import json
import ast
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_rust_api import differences
from check_api_metadata import snapshot, ROOT
from check_python_api import syntax


class ApiSnapshotTests(unittest.TestCase):
    def test_python_ast_empty_fields_do_not_depend_on_interpreter_version(self):
        old = ast.parse('class Token:\n def cancel(self) -> None: ...').body[0]
        new = ast.parse('class Token:\n def cancel(self) -> None: ...').body[0]
        # Model the fields introduced by Python 3.12, even when this test is
        # running on an older interpreter.
        new._fields = (*new._fields, 'type_params') if 'type_params' not in new._fields else new._fields
        new.type_params = []
        self.assertEqual(syntax(old), syntax(new))
        new.name = 'DifferentToken'
        self.assertNotEqual(syntax(old), syntax(new))

    def test_rust_comparison_retains_duplicate_items_and_ignores_only_order(self):
        self.assertFalse(differences('pub fn a()\npub fn b()\n', 'pub fn b()\npub fn a()\n'))
        self.assertTrue(differences('pub fn a()\npub fn a()\n', 'pub fn a()\n'))
        self.assertTrue(differences('pub fn a() -> u32\n', 'pub fn a() -> u64\n'))

    def test_api_requirements_and_contracts_match(self):
        self.assertEqual(snapshot(), json.loads((ROOT / 'api/metadata.json').read_text()))
