import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_packages as gate
from package_source import run_consumer


class ReleaseLogTests(unittest.TestCase):
    def test_real_cargo_logs_are_text_even_when_runner_forces_colors(self):
        root = Path(__file__).resolve().parents[2]
        (root / 'target').mkdir(exist_ok=True)
        toolchain = tomllib.loads((root / 'rust-toolchain.toml').read_text())['toolchain']['channel']
        with tempfile.TemporaryDirectory(dir=root / 'target') as temporary:
            work = Path(temporary)
            (work / 'src').mkdir()
            (work / 'Cargo.toml').write_text('[package]\nname="plain-log-regression"\nversion="0.0.0"\nedition="2024"\n[workspace]\n')
            (work / 'src/main.rs').write_text('fn main() { println!("consumer completed"); }\n')
            for surface in ('source-consumer', 'run-logged'):
                log = work / (surface + '.log')
                with self.subTest(surface=surface), patch.dict(os.environ, {'CARGO_TERM_COLOR': 'always'}):
                    if surface == 'source-consumer':
                        result = run_consumer(work, toolchain, work / 'build', log)
                        self.assertEqual(result.returncode, 0)
                    else:
                        subprocess.run([sys.executable, str(root / 'scripts/run_logged.py'), str(log),
                                        'cargo', 'run', '--offline', '--manifest-path', str(work / 'Cargo.toml'),
                                        '--target-dir', str(work / 'build')], check=True)
                    data = log.read_bytes()
                    self.assertIn(b'consumer completed', data)
                    self.assertNotIn(b'\x1b', data)
                    for target in ('x86_64-pc-windows-msvc', 'x86_64-unknown-linux-gnu'):
                        gate.classify_member(data, target + '/source-consumer.log', distribution='qualification')

    def test_generated_logs_still_reject_controls_rules_and_private_material(self):
        for target in ('x86_64-pc-windows-msvc', 'x86_64-unknown-linux-gnu'):
            name = target + '/source-consumer.log'
            for newline in (b'\n', b'\r\n'):
                gate.classify_member(b'plain' + newline, name, distribution='qualification')
            for data, rule in [(b'\x1b[32mcolored\x1b[0m', {}), (b'plain\x00', {}),
                               (b'plain', {'source': 'README.md'}),
                               (b'-----BEGIN PRIVATE KEY-----\nYWJj\n', {})]:
                with self.subTest(target=target), self.assertRaises(ValueError):
                    gate.classify_member(data, name, rule=rule, distribution='qualification')
