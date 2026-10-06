from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_dependencies import (check_deviation, check_manifest, check_python_pins,
                                check_range_motivations)


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

    def test_range_requires_the_motivation_next_to_it(self):
        motivated = ('[workspace.dependencies]\n'
                     '# Range, not an exact pin (deviation DEP-RANGE-1): public source and sink\n'
                     '# interfaces use Tokio I/O traits and must share the host Tokio.\n'
                     'tokio = "1.53.1"\n')
        self.assertEqual(check_range_motivations(motivated, {'tokio'}, 'DEP-RANGE-1'), [])
        bare = '[workspace.dependencies]\nhex = "=0.4.3"\ntokio = "1.53.1"\n'
        self.assertTrue(check_range_motivations(bare, {'tokio'}, 'DEP-RANGE-1'))
        # A comment that is not adjacent, or does not name the deviation, is not a motivation.
        detached = ('[workspace.dependencies]\n# Range (deviation DEP-RANGE-1): Tokio traits cross the API.\n'
                    'hex = "=0.4.3"\ntokio = "1.53.1"\n')
        self.assertTrue(check_range_motivations(detached, {'tokio'}, 'DEP-RANGE-1'))
        unnamed = '[workspace.dependencies]\n# Public source and sink interfaces use Tokio I/O traits.\ntokio = "1.53.1"\n'
        self.assertTrue(check_range_motivations(unnamed, {'tokio'}, 'DEP-RANGE-1'))
        self.assertTrue(check_range_motivations('[workspace.dependencies]\n# DEP-RANGE-1\ntokio = "1.53.1"\n',
                                                {'tokio'}, 'DEP-RANGE-1'))

    def test_range_exception_is_a_declared_deviation(self):
        complete = {'id': 'DEP-RANGE-1', 'rule': 'r', 'scope': 's', 'hazard': 'h', 'reentry': 'e'}
        self.assertEqual(check_deviation(complete), [])
        self.assertTrue(check_deviation(None))
        for field in complete:
            self.assertTrue(check_deviation({**complete, field: ' '}), field)
