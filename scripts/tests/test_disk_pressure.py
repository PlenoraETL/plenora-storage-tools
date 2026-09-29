from copy import deepcopy
import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fixture_s3 import error_code
from qualify_s3_disk_pressure import validate
from release_evidence import validate_bundle


class DiskPressureTests(unittest.TestCase):
    def test_patch_and_prereleases_require_backend_pressure_evidence(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            for version in ('2.0.1', '2.0.1a1', '2.1.0'):
                with self.subTest(version=version), self.assertRaisesRegex(ValueError, 'disk-pressure'):
                    validate_bundle(Path(temporary), 'a' * 40,
                                    {'x86_64-unknown-linux-gnu': {'binary_sha256': 'b' * 64}}, version)

    def report(self):
        scripts = Path(__file__).resolve().parents[1]
        return {'status': 'PASS', 'source_revision': 'a' * 40, 'binary_sha256': 'b' * 64, 'dirty': False,
                'tool_sha256': {name: hashlib.sha256((scripts / name).read_bytes()).hexdigest()
                               for name in ('qualify_s3_disk_pressure.py', 'fixture_s3.py')},
                'disk': {'free_under_pressure': 2 * 1024**2, 'free_before': 240 * 1024**2, 'total_bytes': 256 * 1024**2},
                'backend_error': {'http_status': 507, 'code': 'XMinioStorageFull'},
                'results': [
                    {'case': 'disk_pressure', 'status': 'PASS', 'destination_preserved': True,
                     'public_error': {'code': 'PROVIDER_MUTATION_FAILED', 'category': 'execution', 'phase': 'commit',
                                      'remote_effect': 'unknown', 'retry': {'kind': 'requires_recovery'}}},
                    {'case': 'recovery', 'status': 'PASS', 'checksum_verified': True}]}

    def test_summary_cannot_hide_different_source_missing_recovery_or_unconfined_pressure(self):
        original = self.report()
        validate(original, 'a' * 40, 'b' * 64)
        for change in ('binary', 'dirty', 'tool', 'backend', 'recovery', 'effect', 'space'):
            report = deepcopy(original)
            if change == 'binary': report['binary_sha256'] = 'c' * 64
            elif change == 'dirty': report['dirty'] = True
            elif change == 'tool': report['tool_sha256'] = {}
            elif change == 'backend': report['backend_error']['code'] = 'Unclassified'
            elif change == 'recovery': report['results'].pop()
            elif change == 'effect': report['results'][0]['public_error']['remote_effect'] = 'none'
            elif change == 'space': report['disk']['total_bytes'] = 100 * 1024**3
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate(report, 'a' * 40, 'b' * 64)

    def test_backend_diagnostic_never_exports_unrecognized_response_data(self):
        self.assertEqual(error_code(b'<Error><Code>XMinioStorageFull</Code><Message>secret</Message></Error>'), 'XMinioStorageFull')
        for body in [b'private payload', b'<Error><Code>private endpoint</Code></Error>', b'<Error/>']:
            self.assertEqual(error_code(body), 'Unclassified')
