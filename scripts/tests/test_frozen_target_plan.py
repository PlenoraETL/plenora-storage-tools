"""Both target orchestrators execute the reviewed checks on the same subjects."""
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qualify_target as target
import campaign_state
from frozen_proofs import detail

COMMON = ['check_webdav_fixture.py', 'qualify_cli.py', 'qualify_extended.py',
          'qualify_extended_faults.py', 'audit_release_readiness.py',
          'qualify_extended.py', 'qualify_extended_faults.py']
LINUX = ['qualify_commit_faults.py', 'qualify_local_faults.py', 'qualify_local_faults.py']
TAIL = ['pip-install', 'qualify_python.py', 'qualify_python.py', 'verify_release.py']


class TargetPlanTests(unittest.TestCase):
    def test_every_target_step_binds_binary_wheel_and_continues_after_failure(self):
        details = []
        for windows in (False, True):
            for fail in (False, True):
                with self.subTest(windows=windows, fail=fail), tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
                    root = Path(temp)
                    target_name = 'x86_64-pc-windows-msvc' if windows else 'x86_64-unknown-linux-gnu'
                    folder = root / 'dist/4.0.0' / target_name
                    folder.mkdir(parents=True)
                    binary = folder / ('plenora-storage.exe' if windows else 'plenora-storage')
                    wheel = folder / 'fixture.whl'
                    binary.write_bytes(b'binary')
                    wheel.write_bytes(b'wheel')
                    (folder / 'release-manifest.json').write_text(json.dumps({
                        'target': target_name, 'version': '4.0.0', 'artifacts': [{'name': wheel.name}]}))
                    pin = root / 'pin'
                    pin.write_text('synthetic')
                    reports = root / 'target/release-readiness'
                    reports.mkdir(parents=True)
                    observed = []
                    def run(argv, **kwargs):
                        argv = list(map(str, argv))
                        name = 'pip-install' if argv[1:4] == ['-m', 'pip', 'install'] else Path(argv[1]).name
                        row = {'ordinal': len(observed), 'script': name, 'argv': argv,
                               'binary': campaign_state.digest(binary), 'wheel': campaign_state.digest(wheel),
                               'status': 'FAIL' if fail and name == 'qualify_cli.py' else 'PASS'}
                        observed.append(row)
                        if name not in ('pip-install', 'verify_release.py'):
                            self.assertEqual(kwargs['env']['PLENORA_CLI_BIN'], str(binary.resolve()))
                        if name == 'qualify_python.py':
                            self.assertEqual(argv[argv.index('--wheel') + 1], str(wheel.resolve()))
                        if row['status'] == 'FAIL':
                            raise subprocess.CalledProcessError(3, 'synthetic failure')
                        if 'stdout' in kwargs:
                            kwargs['stdout'].write(json.dumps(row).encode())
                        if '--output' in argv:
                            Path(argv[argv.index('--output') + 1]).write_text(json.dumps(row))
                        conventional = {'qualify_extended.py': 'extended-qualification.json',
                                        'qualify_extended_faults.py': 'extended-regressions.json',
                                        'audit_release_readiness.py': 'results.json',
                                        'qualify_local_faults.py': 'local-faults.json'}
                        if name in conventional:
                            (reports / conventional[name]).write_text(json.dumps(row))
                    stack.enter_context(patch.object(target, 'ROOT', root))
                    stack.enter_context(patch.object(target.sys, 'platform', 'win32' if windows else 'linux'))
                    stack.enter_context(patch.object(target.venv.EnvBuilder, 'create'))
                    stack.enter_context(patch.object(target.subprocess, 'run', side_effect=run))
                    if fail:
                        with self.assertRaises(RuntimeError):
                            target.qualify(folder, pin=pin)
                    else:
                        target.qualify(folder, pin=pin)
                    self.assertEqual([row['script'] for row in observed], COMMON + ([] if windows else LINUX) + TAIL)
                    sdk = [row for row in observed if row['script'] == 'qualify_python.py']
                    self.assertEqual(['--spool-uploads' in row['argv'] for row in sdk], [False, True])
                    collection = json.loads((reports / (target_name + '-collection.json')).read_text())
                    self.assertEqual(collection['status'], 'FAIL' if fail else 'PASS')
                    self.assertEqual(sum(row['status'] == 'FAIL' for row in collection['results']), int(fail))
                    details.append({'target': target_name, 'injected_failure': fail,
                                    'scope': 'fake-tools-real-target-control-flow', 'calls': observed,
                                    'collection': collection})
        detail('target-step-receipts.json', details)


if __name__ == '__main__':
    unittest.main()
