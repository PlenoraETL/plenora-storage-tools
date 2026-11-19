"""Hermetic preparation closure and deployment boundary, including on Windows."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import campaign_model as model
import release_campaign as controller
import test_campaign_fence as protocol

ROOT = Path(__file__).resolve().parents[2]


class PreparationSourcesTests(unittest.TestCase):
    def observe_checkout(self, overwrite_probe=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / 'checkout'
            (checkout / 'scripts').mkdir(parents=True)
            (root / 'bin').mkdir()
            stub = b'# isolated live-service probe\n'
            for name in model.preparation_sources('probes'):
                (checkout / name).write_bytes(stub)
            # Exercise exactly the installer used by the failing POSIX test.
            protocol.PreparationProtocolTests.install_real_preparation(
                SimpleNamespace(root=root, checkout=checkout))
            if overwrite_probe:
                name = 'scripts/check_fixtures.py'
                shutil.copyfile(ROOT / name, checkout / name)
            for name in model.preparation_sources('probes'):
                self.assertEqual((checkout / name).read_bytes(), stub)
            env = dict(os.environ, PYTHONPATH='', PYTHONDONTWRITEBYTECODE='1')
            for script, arguments, expected in (
                ('campaign_shell.py', ['fixture_file'], b'umask 022; profile_mode=644'),
                ('fixture_tls.py', ['--help'], b'--output'),
            ):
                result = subprocess.run([sys.executable, str(checkout / 'scripts' / script), *arguments],
                                        cwd=checkout, env=env, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)

    def test_runtime_imports_and_probe_isolation(self):
        self.observe_checkout()
        # The previous regex walker followed generated shell strings and
        # silently replaced precisely this live-service double.
        with self.assertRaises(AssertionError):
            self.observe_checkout(overwrite_probe=True)

    def test_omitted_import_dependency_is_detected_without_posix(self):
        self.observe_checkout()
        reduced = tuple(p for p in model.preparation_sources('runtime')
                        if p != 'scripts/campaign_model.py')
        with patch.dict(model.PREPARATION_SOURCES, {'runtime': reduced}):
            with self.assertRaises(AssertionError):
                self.observe_checkout()

    def test_both_bundle_paths_require_the_committed_preparation_closure(self):
        paths = model.preparation_sources()
        # Names preserve the two production call sites: preparation and runner.
        for log in ('command.log', 'bundle.log'):
            with patch.object(controller.subprocess, 'check_output',
                              return_value='\n'.join((*paths, 'unrelated-source'))) as tree, \
                    patch.object(controller, 'logged') as logged:
                controller.source_bundle(Path('source.bundle'), Path('attempt'), 'a' * 40, name=log)
                self.assertEqual(tree.call_args.args[0],
                                 ['git', 'ls-tree', '-r', '--name-only', 'a' * 40])
                self.assertEqual(logged.call_args.args[0],
                                 ['git', 'bundle', 'create', 'source.bundle', 'HEAD'])
                self.assertEqual(logged.call_args.kwargs['name'], log)

        def refuse_missing():
            for omitted in paths:
                with patch.object(controller.subprocess, 'check_output',
                                  return_value='\n'.join(p for p in paths if p != omitted)), \
                        patch.object(controller, 'logged') as logged:
                    with self.assertRaisesRegex(ValueError, 'required preparation files'):
                        controller.source_bundle(Path('source.bundle'), Path('attempt'), 'a' * 40)
                    logged.assert_not_called()
        refuse_missing()
        with patch.object(controller, 'require_preparation_sources'):
            with self.assertRaises(AssertionError):
                refuse_missing()
