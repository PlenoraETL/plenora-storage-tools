from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_dependencies import check_manifest


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
