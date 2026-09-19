import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from versioning import parse_version


class VersionTests(unittest.TestCase):
    def test_release_and_wheel_identity(self):
        for native, wheel in [('0.2.2', '0.2.2'), ('1.0.0-rc.1', '1.0.0rc1'),
                              ('1.0.0-rc.2', '1.0.0rc2'), ('1.0.0', '1.0.0'),
                              ('1.1.0-alpha.2', '1.1.0a2'), ('1.1.0-beta.1', '1.1.0b1')]:
            with self.subTest(native=native):
                version = parse_version(native)
                self.assertEqual(version.native, native)
                self.assertEqual(version.python, wheel)

    def test_prereleases_cannot_bypass_qualification_gates(self):
        for native in ['0.2.1-rc.1', '1.0.0-rc.1', '1.0.0']:
            self.assertTrue(parse_version(native).requires((0, 2, 1)))
        self.assertFalse(parse_version('0.2.0').requires((0, 2, 1)))

    def test_ambiguous_or_unsupported_versions_are_rejected(self):
        for value in ['v1.0.0', '1.0', '01.0.0', '1.0.0rc1', '1.0.0-rc.01',
                      '1.0.0-rc.0', '1.0.0-preview.1', '1.0.0+build', '../1.0.0', '1.0.0\n']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_version(value)


if __name__ == '__main__':
    unittest.main()
