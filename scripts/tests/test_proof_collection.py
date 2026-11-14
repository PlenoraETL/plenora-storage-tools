import sys
from pathlib import Path
import tempfile
import unittest
import json
import subprocess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proof_collection import Collection
import qualify_target
import qualify_windows_candidate
import qualify_extended_faults


class CollectionTests(unittest.TestCase):
    def test_collects_all_failures_and_redacts_callbacks(self):
        collection = Collection()
        for name in ('first', 'second', 'third'):
            with collection.case(name):
                if name != 'second':
                    raise ValueError('private endpoint and payload')
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            with self.assertRaises(RuntimeError):
                collection.finish(output)
            self.assertNotIn('private endpoint', output.read_text())
        self.assertEqual([r['status'] for r in collection.results], ['FAIL', 'PASS', 'FAIL'])

    def test_interrupt_is_not_swallowed(self):
        with self.assertRaises(KeyboardInterrupt):
            with Collection().case('interrupted'):
                raise KeyboardInterrupt()

    def test_empty_is_not_success(self):
        with self.assertRaises(RuntimeError):
            Collection().finish()

    def test_fatal_admission_errors_stop_collection(self):
        class Fenced(RuntimeError):
            pass
        with self.assertRaises(Fenced):
            with Collection(fatal=(Fenced,)).case('admission'):
                raise Fenced()

    def test_target_attempts_remaining_scripts_after_multiple_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / 'candidate'
            folder.mkdir()
            target = 'x86_64-pc-windows-msvc' if sys.platform == 'win32' else 'x86_64-unknown-linux-gnu'
            (folder / 'release-manifest.json').write_text(json.dumps({
                'target': target, 'version': '3.0.5', 'artifacts': []}))
            pin = root / 'pin'
            pin.write_text('fixture')
            seen = []

            def fail(command, **kwargs):
                seen.append(Path(command[1]).name)
                raise subprocess.CalledProcessError(1, 'private-command')

            with patch.object(qualify_target, 'ROOT', root), patch.object(qualify_target.subprocess, 'run', side_effect=fail):
                with self.assertRaises(RuntimeError):
                    qualify_target.qualify(folder, pin=pin)
            self.assertIn('audit_release_readiness.py', seen)
            self.assertIn('qualify_extended.py', seen)
            self.assertEqual(seen[-1], 'verify_release.py')
            report = json.loads((root / 'target/release-readiness/candidate-collection.json').read_text())
            self.assertEqual(report['status'], 'FAIL')
            self.assertGreaterEqual(len(report['results']), 8)

    def test_windows_does_not_copy_stale_reports_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / 'x86_64-pc-windows-msvc'
            folder.mkdir()
            (folder / 'plenora-storage.exe').write_bytes(b'fixture')
            stale = root / 'target/release-readiness/results.json'
            stale.parent.mkdir(parents=True)
            stale.write_text('stale PASS')
            with patch.object(qualify_windows_candidate, 'ROOT', root), patch.object(sys, 'platform', 'win32'), \
                 patch.object(qualify_windows_candidate.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'private')) as run:
                with self.assertRaises(RuntimeError):
                    qualify_windows_candidate.check(root)
            self.assertEqual(run.call_count, 4)
            self.assertFalse((folder / 'cli-regressions.json').exists())
            self.assertFalse(stale.exists())

    def test_protocol_failures_emit_complete_failed_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / 'binary'
            binary.write_bytes(b'fixture')
            output = root / 'faults.json'
            with patch.object(qualify_extended_faults, 'BINARY', binary), \
                 patch.object(sys, 'argv', ['qualify_extended_faults.py', '--output', str(output)]), \
                 patch.object(qualify_extended_faults.subprocess, 'run', side_effect=OSError('private')), \
                 patch('builtins.print'):
                with self.assertRaises(RuntimeError):
                    qualify_extended_faults.main()
            report = json.loads(output.read_text())
            self.assertEqual({r['name'] for r in report['results']}, qualify_extended_faults.EXPECTED_TESTS)
            self.assertTrue(all(r['status'] == 'FAIL' for r in report['results']))
            self.assertNotIn('private', output.read_text())
