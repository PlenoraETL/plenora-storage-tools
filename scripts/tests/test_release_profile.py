"""Offline negative gates for verified dependency-only qualification."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release_profile as profile
from release_publication import validate_profile_receipt
from release_evidence import validate_bundle
from run_vm_campaign import phases_for
import test_release_evidence as fixtures
from test_release_evidence import REVISION, WHEEL


class DependencyPatchTests(unittest.TestCase):
    def setUp(self):
        self.before = {'Cargo.toml': '[workspace.package]\nversion="3.0.2"\n',
                       'Cargo.lock': 'old', 'fuzz/Cargo.lock': 'old',
                       'docs/STATO.md': 'Generated version 3.0.2'}
        self.after = {key: value.replace('3.0.2', '3.0.3') for key, value in self.before.items()}
        self.after['Cargo.lock'] = 'new'

    def git(self, root, *args):
        if args[0] == 'rev-parse':
            return 'b' * 40 if args[1].startswith('v3.0.2') else 'a' * 40
        if args[0] == 'show':
            revision, name = args[1].split(':', 1)
            return (self.before if revision == 'b' * 40 else self.after)[name]
        if '--name-status' in args:
            return '\n'.join('M\t' + name for name in self.after if self.after[name] != self.before.get(name))
        if '--raw' in args:
            return ':100644 100644 a b M ' + args[-1]
        self.fail(args)

    def verify(self):
        with patch.object(profile, 'git', side_effect=self.git), patch.object(profile.subprocess, 'run'):
            return profile.verify_diff('HEAD')

    def test_allowed_diff_accepted(self):
        self.assertEqual(self.verify()['changed_files'], ['Cargo.lock', 'Cargo.toml', 'docs/STATO.md'])

    def test_verified_profile_accepted_then_source_change_refused(self):
        with patch.object(profile, 'git', side_effect=self.git), patch.object(profile.subprocess, 'run'), \
                patch.object(profile, 'workflow', side_effect=lambda run, revision, name:
                             {'run_id': str(run), 'run_attempt': 1}):
            report = profile.declaration('a' * 40, 123, 456)
            profile.validate(report, 'a' * 40)
            self.after['src/lib.rs'] = 'changed'
            with self.assertRaisesRegex(ValueError, 'non-dependency'):
                profile.validate(report, 'a' * 40)

    def test_rust_python_workflow_and_contract_refused(self):
        for name in ('src/lib.rs', 'scripts/tool.py', '.github/workflows/ci.yml', 'contracts/api.json'):
            with self.subTest(name=name):
                self.after[name] = 'changed'
                with self.assertRaisesRegex(ValueError, 'non-dependency'):
                    self.verify()
                del self.after[name]

    def test_manifest_dependency_change_and_inventory_change_refused(self):
        self.after['Cargo.toml'] += '[dependencies]\ntokio="1.53.3"\n'
        with self.assertRaises(ValueError):
            self.verify()
        self.after['Cargo.toml'] = self.before['Cargo.toml'].replace('3.0.2', '3.0.3')
        self.after['docs/STATO.md'] += '\nnew capability'
        with self.assertRaises(ValueError):
            self.verify()

    def test_minor_major_and_prerelease_refused(self):
        for version in ('3.1.0', '4.0.0', '3.0.3-rc.1', '3.0.4'):
            self.after['Cargo.toml'] = self.before['Cargo.toml'].replace('3.0.2', version)
            with self.assertRaises(ValueError):
                self.verify()

    def test_reduced_receipt_cannot_claim_full(self):
        with self.assertRaisesRegex(ValueError, 'full profile'):
            validate_profile_receipt({'qualification_profile': {'profile': 'full'},
                                      'additional_gates': [{'name': 'profile.json'}]})

    def test_phase_selection_omits_only_performance_and_soak(self):
        self.assertEqual(set(phases_for('3.0.3')) - set(phases_for('3.0.3', 'dependency-patch')),
                         set(profile.OMITTED))
        self.assertIn('install-sdk', phases_for('3.0.3', 'dependency-patch'))
        self.assertIn('spooled-large', phases_for('3.0.3', 'dependency-patch'))

    def test_forged_diff_proof_and_phase_inventory_refused(self):
        report = dict(profile='dependency-patch', eligibility=self.verify(), executed=profile.FUNCTIONAL,
                      omitted=profile.OMITTED, workflows={})
        with patch.object(profile, 'verify_diff', return_value={}), self.assertRaises(ValueError):
            profile.validate(report, REVISION)
        report['executed'] = ['qualify-linux']
        with self.assertRaises(ValueError):
            profile.validate(report, REVISION)

    def test_workflow_identity_jobs_and_attempt_verified(self):
        run = dict(head_sha=REVISION, status='completed', conclusion='success', run_attempt=1,
                   path='.github/workflows/ci.yml', repository={'full_name': profile.REPOSITORY})
        jobs = {'jobs': [{'name': name, 'conclusion': 'success'} for name in ('docker-conformance', 'native-windows')]}
        def call(command, **kwargs):
            return json.dumps(jobs if '/jobs?' in command[-1] else run)
        with patch.object(profile.subprocess, 'check_output', side_effect=call):
            self.assertEqual(profile.workflow(123, REVISION, 'ci.yml')['run_attempt'], 1)
            jobs['jobs'][0]['conclusion'] = 'skipped'
            with self.assertRaises(ValueError):
                profile.workflow(123, REVISION, 'ci.yml')
            jobs['jobs'][0]['conclusion'] = 'success'
            run['path'] = '.github/workflows/other.yml'
            with self.assertRaises(ValueError):
                profile.workflow(123, REVISION, 'ci.yml')

    def test_reduced_evidence_requires_verified_profile_and_functional_reports(self):
        fixture = fixtures.ReleaseEvidenceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.bundle()
        (fixture.root / 'soak/report.json').unlink()
        for name in ('baseline.json', 'candidate.json', 'report.json'):
            (fixture.root / 'performance' / name).unlink()
        # Without a profile the unchanged full validator must reject missing soak.
        with self.assertRaises((ValueError, FileNotFoundError)):
            validate_bundle(fixture.root, REVISION, fixture.subjects, '1.0.0rc1')
        fixture.write('profile.json', {'profile': 'dependency-patch'})
        log = fixture.write('install-sdk.log', b'Successfully installed plenora-storage-1.0.0rc1\n')
        fixture.write('install-sdk.json', {'sha256': WHEEL, 'log_sha256': log})
        with patch.object(profile, 'validate') as validate:
            inventory = validate_bundle(fixture.root, REVISION, fixture.subjects, '1.0.0rc1')
            validate.assert_called_once()
            self.assertIn('profile.json', {row['name'] for row in inventory})
            (fixture.root / 'transfers/workers4.json').unlink()
            with self.assertRaises((ValueError, FileNotFoundError)):
                validate_bundle(fixture.root, REVISION, fixture.subjects, '1.0.0rc1')
        with self.assertRaises(ValueError):
            validate_bundle(fixture.root, REVISION, fixture.subjects, '1.0.0rc1')
