import copy
import sys
from pathlib import Path
import unittest
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_release_guard_coverage as coverage


class ReleaseGuardCoverageTests(unittest.TestCase):
    def test_all_release_invocations_have_ci_coverage(self):
        coverage.check()

    def test_missing_platform_verification_or_inputs_fail_closed(self):
        original = coverage.jobs('ci.yml')
        for job, old, new in [
            ('native-windows', 'python scripts/qualify_windows_candidate.py dist/$version', 'echo omitted'),
            ('native-windows', 'runs-on: windows-latest', 'runs-on: ubuntu-latest'),
            ('docker-conformance', 'scripts/qualify_target.py', 'scripts/other.py'),
            ('docker-conformance', '--no-deps storage-rust bash -c', '--no-deps other bash -c'),
            ('release-package-recheck', ' --release', ''),
            ('release-package-recheck', 'merge-multiple: true', 'merge-multiple: false'),
        ]:
            ci = copy.deepcopy(original)
            self.assertIn(old, ci[job])
            ci[job] = ci[job].replace(old, new)
            with self.subTest(job=job, mutation=old), self.assertRaises(ValueError):
                coverage.check(ci=ci)

    def test_new_release_guard_invocation_requires_ci_mapping(self):
        for workflow in ('release-candidate.yml', 'release.yml'):
            jobs = coverage.jobs(workflow)
            jobs['new-guard'] = '    runs-on: windows-latest\n      - run: python scripts/verify_release.py dist/3.0.5\n'
            with self.subTest(workflow=workflow), self.assertRaises(ValueError):
                coverage.check(**{'candidate' if workflow == 'release-candidate.yml' else 'release': jobs})

    def test_new_wrappers_are_discovered_from_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'scripts').mkdir()
            (root / 'scripts/new_guard.py').write_text('from check_packages import verify_distribution\n')
            (root / 'scripts/outer.py').write_text("command = ['python', 'scripts/new_guard.py']\n")
            coverage.guard_scripts.cache_clear()
            try:
                with patch.object(coverage, 'ROOT', root):
                    self.assertEqual(coverage.invocations('python scripts/outer.py input'), [('outer', '')])
            finally:
                coverage.guard_scripts.cache_clear()
