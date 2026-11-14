from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_python_version as gate


class PythonVersionTests(unittest.TestCase):
    def test_checkout(self):
        self.assertEqual(gate.check(), [])

    def test_divergent_projections_are_rejected(self):
        files = ['.python-version', 'crates/plenora-storage-py/pyproject.toml',
                 'crates/plenora-storage-py/Cargo.toml', 'scripts/package_files.json',
                 'scripts/package_generated.py', 'scripts/check_packages.py',
                 'scripts/release_evidence.py', 'scripts/install_python.sh',
                 'Dockerfile', 'fuzz/Dockerfile', 'docker/extended/Dockerfile',
                 'docker/minio/Dockerfile', 'scripts/python_historical_evidence.json',
                 'README.md', 'CHANGELOG.md']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(gate.ROOT / '.github', root / '.github')
            shutil.copytree(gate.ROOT / 'docs', root / 'docs')
            for name in files:
                dest = root / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(gate.ROOT / name, dest)
            for name, before, after in [
                ('.python-version', '3.14', '3.13'),
                (files[1], '>=3.14', '>=3.13'),
                (files[2], 'abi3-py314', 'abi3-py313'),
                (files[3], 'cp314', 'cp313'),
                ('scripts/release_evidence.py', "('3.14',)", "('3.13',)"),
                ('.github/workflows/ci.yml', "python: ['3.14']", "python: ['3.13']"),
                ('.github/workflows/release.yml', 'python-version-file: .python-version', "python-version: '3.13'"),
                ('docs/release-2.0.0.md', '3.10', '3.09'),
            ]:
                with self.subTest(name=name):
                    path = root / name
                    text = path.read_text()
                    self.assertIn(before, text)
                    path.write_text(text.replace(before, after))
                    self.assertTrue(gate.check(root))
                    path.write_text(text)
