import sys
from pathlib import Path
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from package_cli import build, verify_archive


class CLIPackageTests(unittest.TestCase):
    def test_archive_cannot_attest_a_different_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for target, name in [('x86_64-pc-windows-msvc', 'plenora-storage.exe'),
                                 ('x86_64-unknown-linux-gnu', 'plenora-storage')]:
                with self.subTest(target=target):
                    binary = root / name
                    binary.write_bytes(b'candidate executable')
                    archive = build(root, binary, '1.0.0-rc.1', target)
                    binary.write_bytes(b'another executable')
                    with self.assertRaisesRegex(ValueError, 'differs'):
                        verify_archive(archive, binary)

    def test_matching_executable_without_licenses_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / 'plenora-storage.exe'
            binary.write_bytes(b'candidate executable')
            archive = root / 'incomplete.zip'
            with zipfile.ZipFile(archive, 'w') as stream:
                stream.write(binary, binary.name)
            with self.assertRaisesRegex(ValueError, 'missing members'):
                verify_archive(archive, binary)
