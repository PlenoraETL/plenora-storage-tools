from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release_evidence import Evidence, validate_bundle
from scan_artifacts import CONFIG, SYFT_VERSION, digest, native_inventory, unpack, validate


class ArtifactScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = 'x86_64-unknown-linux-gnu'
        self.prefix = 'native-components/' + self.target
        self.subjects = {'binary_sha256': 'b' * 64, 'wheel_sha256': 'c' * 64, 'cli_archive_sha256': 'd' * 64}
        self.revision = 'a' * 40

    def write(self, name, value):
        path = self.root / self.prefix / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return digest(path)

    def bundle(self):
        rows = []
        for role in ('cli', 'wheel'):
            name = 'plenora-storage' if role == 'cli' else 'package/native.so'
            native = {name: {'sha256': self.subjects['binary_sha256'] if role == 'cli' else 'e' * 64, 'size': 12}}
            raw = {'descriptor': {'name': 'syft', 'version': SYFT_VERSION}, 'artifacts': [],
                   'source': {'name': role, 'version': '2.0.0'},
                   'files': [{'location': {'path': '/' + name}, 'metadata': {'size': 12},
                              'executable': {'format': 'elf', 'importedLibraries': ['libc.so.6']}}]}
            cdx = {'bomFormat': 'CycloneDX', 'specVersion': '1.6',
                   'metadata': {'component': {'name': role, 'version': '2.0.0'}}}
            rows.append({'role': role, 'artifact': role,
                         'artifact_sha256': self.subjects['cli_archive_sha256' if role == 'cli' else 'wheel_sha256'],
                         'native_files': native, 'native_imports': native_inventory(raw, native),
                         'packages_detected': 0, 'raw': role + '.json',
                         'raw_sha256': self.write(role + '.json', raw),
                         'cyclonedx': role + '.cdx.json',
                         'cyclonedx_sha256': self.write(role + '.cdx.json', cdx)})
        report = {'status': 'PASS', 'source_revision': self.revision, 'dirty': False,
                  'version': '2.0.0', 'target': self.target,
                  'scanner': {'name': 'syft', 'version': SYFT_VERSION, 'config_sha256': digest(CONFIG)},
                  'results': rows}
        self.write('report.json', report)
        return report

    def test_records_files_and_rejects_missing_or_tampered_scan(self):
        self.bundle()
        evidence = Evidence(self.root)
        validate(evidence, self.prefix, self.revision, self.subjects)
        self.assertEqual(len(evidence.files), 5)
        for name in ('cli.json', 'wheel.json', 'cli.cdx.json', 'report.json'):
            path = self.root / self.prefix / name
            original = path.read_bytes()
            path.unlink()
            with self.assertRaises((ValueError, FileNotFoundError)):
                validate(Evidence(self.root), self.prefix, self.revision, self.subjects)
            path.write_bytes(original)
        (self.root / self.prefix / 'cli.json').write_text('{}')
        with self.assertRaises(ValueError):
            validate(Evidence(self.root), self.prefix, self.revision, self.subjects)

    def test_pass_cannot_hide_wrong_bytes_omitted_roles_or_native_files(self):
        original = self.bundle()
        for change in ('digest', 'role', 'count', 'imports', 'development', 'scanner'):
            report = deepcopy(original)
            if change == 'digest': report['results'][0]['artifact_sha256'] = 'f' * 64
            elif change == 'role': report['results'].pop()
            elif change == 'count': report['results'][0]['packages_detected'] = 1
            elif change == 'imports': report['results'][0]['native_imports'] = {}
            elif change == 'development': report['status'] = 'DEVELOPMENT'
            elif change == 'scanner': report['scanner']['version'] = '0.0.0'
            self.write('report.json', report)
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate(Evidence(self.root), self.prefix, self.revision, self.subjects)

    def test_release_two_requires_scans_before_accepting_other_evidence(self):
        with self.assertRaisesRegex(ValueError, 'native-components'):
            validate_bundle(self.root, self.revision, {self.target: self.subjects}, '2.0.0')

    def test_extraction_rejects_traversal_and_aliases(self):
        for name in ('../outside', '/absolute', 'C:/drive', 'a\\b', './alias'):
            archive = self.root / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as stream:
                member = zipfile.ZipInfo('placeholder')
                member.filename = name
                stream.writestr(member, b'payload')
            with self.subTest(name=name), self.assertRaises(ValueError):
                unpack(archive, self.root / 'extract')
