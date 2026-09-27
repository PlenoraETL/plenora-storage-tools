from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from verify_release import validate_identity


class ReleaseIdentityTests(unittest.TestCase):
    def test_prerelease_cannot_be_renamed_as_stable(self):
        target = 'x86_64-pc-windows-msvc'
        manifest = {'version': '1.0.0-alpha.1', 'target': target}
        validate_identity(Path('dist/1.0.0-alpha.1') / target, manifest)
        with self.assertRaises(ValueError):
            validate_identity(Path('dist/1.0.0') / target, manifest)
        with self.assertRaises(ValueError):
            validate_identity(Path('dist/1.0.0-alpha.1/x86_64-unknown-linux-gnu'), manifest)

    def test_optimized_python_cannot_disable_release_assertions(self):
        with tempfile.TemporaryDirectory() as temporary:
            for script, extra in [('verify_release.py', []),
                                  ('qualify_release.py', ['--evidence', temporary])]:
                result = subprocess.run([sys.executable, '-O', str(ROOT / 'scripts' / script),
                                         temporary, *extra], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('cannot run with Python optimization enabled', result.stderr)
