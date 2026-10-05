from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_dependencies import check_manifest, check_python_pins


class DependencyPolicyTests(unittest.TestCase):
    def test_rejects_ranges_git_and_unpinned_target_dependencies(self):
        for spec in ('1.2', '^1.2.3', '*', {'git': 'https://example.invalid/repo'}, {'version': '1.2.3'}):
            self.assertTrue(check_manifest({'target': {'cfg(unix)': {'dependencies': {'x': spec}}}}))

    def test_exception_is_only_for_reviewed_workspace_boundary(self):
        document = {'workspace': {'dependencies': {'tokio': '1.53.1'}}}
        self.assertEqual(check_manifest(document, workspace=True, exceptions={'tokio'}), [])
        self.assertTrue(check_manifest(document))
        self.assertTrue(check_manifest({'dependencies': {'tokio': '1.53.1'}}, exceptions={'tokio'}))
        self.assertEqual(check_manifest({'dependencies': {'x': '=1.2.3', 'owned': {'path': '../owned'}}}), [])

    def test_python_requirements_and_build_backend_are_exact(self):
        self.assertEqual(check_python_pins('r', ['maturin==1.15.0', 'rpds-py==2026.9.1', 'typing_extensions==4.16.0']), [])
        for requirement in ('maturin>=1.7,<2.0', 'jsonschema', 'x==1.0; python_version < "3.13"',
                            'x~=1.0', 'x==1.*', '-r other.txt', 'x == 1.0'):
            self.assertTrue(check_python_pins('r', [requirement]), requirement)
