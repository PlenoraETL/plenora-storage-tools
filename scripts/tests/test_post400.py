"""Regression tests run by the existing script unittest CI jobs."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from canonical_digest import canonical_digest
from proof_collection import Collection, export_reports
import render_sbom
import build_release


class Post400Tests(unittest.TestCase):
    def test_text_identity_is_lf_and_artifact_identity_is_exact(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'input'
            expected = hashlib.sha256(b'one\ntwo\n').hexdigest()
            for data in (b'one\ntwo\n', b'one\r\ntwo\r\n', b'one\rtwo\r'):
                path.write_bytes(data)
                self.assertEqual(canonical_digest(path), expected)
                self.assertEqual(build_release.digest(path), hashlib.sha256(data).hexdigest())

    def test_sbom_and_source_digest_survive_working_tree_conversion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = {'Cargo.toml': '[workspace.package]\nversion="4.0.0"\n',
                     'Cargo.lock': 'version = 4\npackage = []\n',
                     'rust-toolchain.toml': '[toolchain]\nchannel="1.98.1"\n',
                     'contracts/example.json': '{}\n'}
            for name, data in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data.encode())
            with patch.object(render_sbom, 'ROOT', root), patch.object(build_release, 'ROOT', root):
                baseline = render_sbom.render(), build_release.source_digest()
                for name, data in files.items():
                    (root / name).write_bytes(data.replace('\n', '\r\n').encode())
                self.assertEqual((render_sbom.render(), build_release.source_digest()), baseline)

    def test_failure_exports_fresh_redacted_collection_before_raising(self):
        with tempfile.TemporaryDirectory() as temp:
            source, exported = Path(temp) / 'temporary.json', Path(temp) / 'campaign/report.json'
            source.write_text('stale PASS')
            with self.assertRaises(RuntimeError):
                with export_reports([(source, exported)]):
                    self.assertFalse(source.exists())
                    collection = Collection()
                    with collection.case('internal-case'):
                        raise ValueError('secret endpoint and callback')
                    collection.finish(source)
            report = json.loads(exported.read_text())
            self.assertEqual(report['results'], [{'name': 'internal-case', 'status': 'FAIL',
                                                'failure_type': 'ValueError'}])
            self.assertNotIn('secret', exported.read_text())
            with self.assertRaises(OSError):
                with export_reports([(source, exported)]):
                    raise OSError('no report produced')
            self.assertFalse(exported.exists())

    def test_git_process_policy_is_private_without_changing_parent_mask(self):
        import os
        import private_git
        with patch.object(private_git.os, 'name', 'posix'):
            with patch.object(private_git.subprocess, 'run') as run:
                private_git.run(['git', 'status'], env={'FIXTURE': 'value'})
            self.assertEqual(run.call_args.kwargs['umask'], 0o077)
            self.assertEqual(run.call_args.kwargs['env'], {'FIXTURE': 'value', 'GIT_OPTIONAL_LOCKS': '0'})
        with patch.object(private_git.os, 'name', 'nt'):
            self.assertNotIn('umask', private_git.options({}))

    def test_vm_failed_case_reaches_attempt_inventory(self):
        from contextlib import ExitStack
        import run_vm_campaign as vm
        import campaign_state as state
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            root = Path(temp)
            candidate, private, output, fixtures = (root / n for n in ('candidate', 'private', 'output', 'fixtures'))
            for folder in (candidate, private, fixtures):
                folder.mkdir()
            binary, wheel = candidate / 'plenora-storage', candidate / 'fixture.whl'
            for path in (binary, wheel):
                path.write_bytes(b'fixture')
            subject = {'version': '4.0.0', 'source_revision': 'a' * 40,
                       'artifacts': {p.name: state.digest(p) for p in (binary, wheel)},
                       'policy_id': 'storage-release-no-soak-v1'}
            state.write_json(candidate / 'release-manifest.json', subject)
            state.write_json(fixtures / 'fixture-state.json', {'kind': 'reset', 'nonce': 'reset'})
            def external(argv, folder, **kwargs):
                if Path(str(argv[1])).name == 'qualify_target.py':
                    checks = Collection()
                    with checks.case('internal-vm-case'):
                        raise ValueError('secret callback')
                    checks.finish(root / 'target/release-readiness' / (vm.TARGET + '-collection.json'))
                if '--output' in argv:
                    state.write_json(Path(argv[argv.index('--output') + 1]), {'status': 'PASS'})
            for name, value in [('ROOT', root)]:
                stack.enter_context(patch.object(vm, name, value))
            stack.enter_context(patch.object(vm.sys, 'platform', 'linux'))
            stack.enter_context(patch.object(vm, 'identity', return_value=subject))
            stack.enter_context(patch.object(vm, 'logged', side_effect=external))
            stack.enter_context(patch.object(vm, 'qualification_copy', return_value=candidate))
            stack.enter_context(patch.object(vm.venv.EnvBuilder, 'create', side_effect=lambda p: p.mkdir()))
            stack.enter_context(patch.object(vm, 'inspect', return_value={'status': 'PASS'}))
            stack.enter_context(patch.object(vm, 'available_memory', return_value=10**15))
            stack.enter_context(patch.object(vm, 'inspect_memory', return_value={'status': 'PASS'}))
            with self.assertRaises(RuntimeError):
                vm.measure(candidate, private, output, fixtures, [], None, [], 'reset',
                           lambda: None, {'candidate': state.digest(binary)}, 'epoch')
            report = output / 'qualify-linux/1/collection.json'
            self.assertEqual(json.loads(report.read_text())['results'][0]['name'], 'internal-vm-case')
            self.assertNotIn('secret', report.read_text())
            ledger = json.loads((output / 'campaign.json').read_text())
            attempt = ledger['phases']['qualify-linux'][0]
            self.assertEqual(attempt['status'], 'FAIL')
            self.assertEqual(state.Campaign.inventory(report.parent)['collection.json'], state.digest(report))
