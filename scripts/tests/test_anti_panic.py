from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_anti_panic import command


class AntiPanicGateTests(unittest.TestCase):
    def test_gate_covers_every_library_and_every_panic_primitive(self):
        argv = command()
        self.assertEqual(argv[:5], ['cargo', 'clippy', '--workspace', '--lib', '--locked'])
        denied = {argv[i + 1] for i, part in enumerate(argv) if part == '-D'}
        self.assertEqual(denied, {
            'unsafe-code', 'clippy::unwrap_used', 'clippy::expect_used', 'clippy::panic',
            'clippy::unreachable', 'clippy::todo', 'clippy::unimplemented'})


if __name__ == '__main__':
    unittest.main()
