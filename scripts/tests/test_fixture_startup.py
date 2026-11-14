"""Offline fixture startup regressions: real TLS, mocked application exchanges."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'docker'))
import fixture_tls
import fixture_readiness as ready


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


minio = load('minio_init', 'docker/minio/init.py')
cloud = load('cloud_init', 'docker/extended/init.py')
health = load('fixture_health', 'docker/extended/health.py')


class FixtureTlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.folder.cleanup)
        cls.root = Path(cls.folder.name)
        # Missing OpenSSL is a test failure: CI must execute the real handshake.
        fixture_tls.generate('minio', cls.root)
        fixture_tls.generate('ftps', cls.root / 'ftps')

    def test_generated_minio_and_ftps_pass_strict_python_verification(self):
        self.assertTrue(ssl.create_default_context().verify_flags & ssl.VERIFY_X509_STRICT)
        fixture_tls.verify_pair(self.root / 'minio/public.crt', self.root / 'minio/private.key',
                                self.root / 'ca.crt', 'minio-tls')
        for name in ('ftps', '127.0.0.1'):
            fixture_tls.verify_pair(self.root / 'ftps/server.crt', self.root / 'ftps/server.key',
                                    self.root / 'ftps/server.crt', name)

    def test_original_ca_without_key_usage_reproduces_regression(self):
        # Same CA key/subject as the valid chain, omitting only keyUsage.
        subprocess.run(['openssl', 'req', '-x509', '-key', str(self.root / 'ca.key'),
                        '-out', str(self.root / 'bad-ca.crt'), '-days', '2',
                        '-subj', '/CN=Plenora storage test CA',
                        '-addext', 'basicConstraints=critical,CA:TRUE',
                        '-addext', 'subjectKeyIdentifier=hash'], check=True, capture_output=True)
        legacy = subprocess.run(['openssl', 'verify', '-CAfile', str(self.root / 'bad-ca.crt'),
                                 str(self.root / 'minio/public.crt')], capture_output=True)
        self.assertEqual(legacy.returncode, 0)
        with self.assertRaises(ssl.SSLCertVerificationError) as failure:
            fixture_tls.verify_pair(self.root / 'minio/public.crt', self.root / 'minio/private.key',
                                    self.root / 'bad-ca.crt', 'minio-tls')
        self.assertEqual(failure.exception.verify_code, 92)

    def test_hostname_and_trust_are_not_relaxed(self):
        for trust, name in ((self.root / 'ca.crt', 'minio-tls-invalid'),
                            (self.root / 'ftps/server.crt', 'minio-tls')):
            with self.subTest(name=name), self.assertRaises(ssl.SSLCertVerificationError):
                fixture_tls.verify_pair(self.root / 'minio/public.crt', self.root / 'minio/private.key', trust, name)

    def test_ftps_dns_identity_and_invalid_san_input(self):
        fixture_tls.generate('ftps', self.root / 'dns', 'fixture.example')
        with self.assertRaises(ValueError):
            fixture_tls.generate('ftps', self.root / 'invalid', 'PRIVATE\nDNS:other')

    def test_generation_failure_is_redacted(self):
        with patch.object(sys, 'argv', ['fixture_tls', 'minio', '--output', 'unused']), \
                patch.object(fixture_tls, 'generate', side_effect=OSError('PRIVATE endpoint credentials')), \
                self.assertRaises(SystemExit) as failure:
            fixture_tls.main()
        self.assertNotIn('PRIVATE', str(failure.exception))
        self.assertIn('retry=never', str(failure.exception))


class FixtureTlsModeTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == 'win32', 'POSIX modes and umask')
    def test_direct_generation_creates_only_unshared_paths(self):
        for mask in (0o000, 0o002, 0o077):
            with self.subTest(umask=oct(mask)), tempfile.TemporaryDirectory() as temporary:
                previous = os.umask(mask)
                try:
                    root = Path(temporary) / 'new' / 'fixtures'
                    fixture_tls.generate('minio', root)
                    fixture_tls.generate('ftps', root / 'extended')
                    self.assertEqual(os.umask(mask), mask)
                    self.assertEqual({p.relative_to(root).as_posix() for p in root.rglob('*')},
                                     {'ca.key', 'ca.crt', 'ca.srl', 'minio', 'minio/private.key',
                                      'minio/public.crt', 'minio.csr', 'minio.ext', 'extended',
                                      'extended/server.key', 'extended/server.crt'})
                    for path in Path(temporary).rglob('*'):
                        with self.subTest(path=path.relative_to(temporary).as_posix()):
                            self.assertEqual(path.stat().st_mode & 0o022, 0)
                finally:
                    os.umask(previous)

    def test_umask_is_set_before_generation_and_restored_on_failure(self):
        with patch.object(fixture_tls.os, 'umask', return_value=0o002) as mask:
            def fail(*args):
                mask.assert_called_once_with(0o022)
                raise OSError('test failure')
            with patch.object(fixture_tls, '_generate', side_effect=fail), self.assertRaises(OSError):
                fixture_tls.generate('minio', 'unused')
            self.assertEqual([call.args for call in mask.call_args_list], [(0o022,), (0o002,)])


class FixtureReadinessTests(unittest.TestCase):
    def run_wait(self, operation):
        sleep = Mock()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            passed = ready.wait_ready('minio-tls', operation, sleep=sleep)
        return passed, output.getvalue(), sleep

    def test_permanent_tls_failures_do_not_sleep_or_retry(self):
        for error in (ssl.SSLCertVerificationError('PRIVATE'),
                      URLError(ssl.SSLCertVerificationError('PRIVATE')),
                      ssl.SSLError('PRIVATE'), FileNotFoundError('PRIVATE')):
            failure = ready.transport_failure(error, 'create_bucket')
            operation = Mock(side_effect=failure)
            passed, output, sleep = self.run_wait(operation)
            self.assertFalse(passed)
            self.assertEqual(operation.call_count, 1)
            sleep.assert_not_called()
            self.assertIn('retry=never', output)
            self.assertNotIn('PRIVATE', output)

    def test_transient_startup_is_bounded_without_sleep_after_last_attempt(self):
        failure = ready.transport_failure(URLError(ConnectionRefusedError('PRIVATE')), 'create_bucket')
        operation = Mock(side_effect=failure)
        passed, output, sleep = self.run_wait(operation)
        self.assertFalse(passed)
        self.assertEqual(operation.call_count, 30)
        self.assertEqual(sleep.call_count, 29)
        self.assertNotIn('PRIVATE', output)
        operation = Mock(side_effect=[failure, None])
        passed, output, sleep = self.run_wait(operation)
        self.assertTrue(passed)
        self.assertEqual(sleep.call_count, 1)
        self.assertIn('application_ready', output)

    def test_http_statuses_and_body_never_escape(self):
        for status in (301, 400, 401, 403, 409, 429, 500, 501, 502, 503, 504):
            with self.subTest(status=status):
                response = HTTPError('https://PRIVATE', status, 'PRIVATE', {}, io.BytesIO(b'PRIVATE'))
                with patch.object(ready, 'urlopen', side_effect=response) as call, \
                        self.assertRaises(ready.FixtureFailure) as failure:
                    ready.exchange(Request('http://fixture'), 'create_bucket')
                self.assertEqual(failure.exception.retry, status in ready.RETRY_STATUSES)
                self.assertNotIn('PRIVATE', failure.exception.diagnostic('minio', 1))
                self.assertTrue(response.closed)
                self.assertEqual(call.call_args.kwargs['timeout'], 5)

    def test_redirects_and_ambient_proxies_are_not_readiness(self):
        self.assertIsNone(ready.NoRedirect().redirect_request(None, None, 302, 'PRIVATE', {}, 'https://PRIVATE'))
        with patch.object(ready, 'build_opener') as opener:
            ready.urlopen(Request('http://fixture'), context=None, timeout=5)
        self.assertEqual(opener.call_args.args[0].proxies, {})

    def test_unexpected_exception_is_redacted_and_interrupt_is_not_swallowed(self):
        passed, output, sleep = self.run_wait(Mock(side_effect=ValueError('PRIVATE')))
        self.assertFalse(passed)
        self.assertIn('category=internal', output)
        self.assertNotIn('PRIVATE', output)
        sleep.assert_not_called()
        with self.assertRaises(KeyboardInterrupt):
            self.run_wait(Mock(side_effect=KeyboardInterrupt))

    def test_oversized_and_truncated_responses_are_permanent(self):
        from http.client import IncompleteRead
        response = Mock(code=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        for result in (b'x' * 65537, IncompleteRead(b'PRIVATE')):
            response.read.side_effect = result if isinstance(result, Exception) else None
            response.read.return_value = result
            with patch.object(ready, 'urlopen', return_value=response), self.assertRaises(ready.FixtureFailure) as failure:
                ready.exchange(Request('http://fixture'), 'verify_bucket')
            self.assertEqual(failure.exception.category, 'protocol')
            self.assertFalse(failure.exception.retry)

    def test_minio_success_requires_signed_head_after_creation_or_owned_conflict(self):
        for first in ((200, b''), (409, b'<Error><Code>BucketAlreadyOwnedByYou</Code></Error>')):
            with patch.object(minio, 'exchange', side_effect=[first, (200, b'')]) as call:
                minio.create_bucket('https://fixture', 'verified-context')
            requests = [c.args[0] for c in call.call_args_list]
            self.assertEqual([r.method for r in requests], ['PUT', 'HEAD'])
            self.assertNotEqual(requests[0].get_header('Authorization'), requests[1].get_header('Authorization'))
            self.assertTrue(all(c.kwargs['context'] == 'verified-context' for c in call.call_args_list))

    def test_minio_unowned_conflict_and_malformed_error_are_fatal(self):
        for body in (b'PRIVATE', b'<Error><Code>BucketAlreadyExists</Code></Error>'):
            with patch.object(minio, 'exchange', return_value=(409, body)) as call, \
                    self.assertRaises(ready.FixtureFailure) as failure:
                minio.create_bucket('http://fixture', None)
            self.assertEqual(call.call_count, 1)
            self.assertFalse(failure.exception.retry)

    def test_minio_context_failure_names_service_without_exception_data(self):
        output = io.StringIO()
        with patch.object(minio, 'wait_ready', return_value=True) as wait, \
                patch.object(minio.ssl, 'create_default_context', side_effect=ssl.SSLError('PRIVATE')), \
                contextlib.redirect_stdout(output):
            self.assertEqual(minio.main(), 1)
        self.assertEqual(wait.call_count, 1)
        self.assertIn('fixture=minio-tls phase=tls_setup', output.getvalue())
        self.assertNotIn('PRIVATE', output.getvalue())

    def test_successful_create_cannot_hide_failed_verification(self):
        for attempt, module, name in ((lambda: minio.create_bucket('http://fixture', None), minio, 'minio'),
                                      (cloud.gcs_attempt, cloud, 'gcs')):
            with self.subTest(service=name), patch.object(module, 'exchange',
                    side_effect=[(200, b''), ready.FixtureFailure('authentication', 'verify_bucket', status=403)]):
                passed, output, sleep = self.run_wait(attempt)
                self.assertFalse(passed)
                self.assertIn('verify_bucket', output)
                sleep.assert_not_called()

    def test_gcs_conflict_requires_metadata_probe(self):
        with patch.object(cloud, 'exchange', side_effect=[(409, b''), (200, b'')]) as call:
            cloud.gcs_attempt()
        self.assertEqual([c.args[0].get_method() for c in call.call_args_list], ['POST', 'GET'])

    def test_azure_rejects_conflict_and_authentication_and_checks_existing_container(self):
        class HttpError(Exception):
            status_code = 403

        class Exists(HttpError):
            error_code = 'ContainerAlreadyExists'

        client = Mock()
        client.create_container.side_effect = Exists('PRIVATE')
        cloud.azure_attempt(client, Exists, HttpError, (ConnectionError,))
        client.get_container_client.return_value.get_container_properties.assert_called_once()
        client.get_container_client.return_value.get_container_properties.side_effect = HttpError('PRIVATE')
        with self.assertRaises(ready.FixtureFailure) as failure:
            cloud.azure_attempt(client, Exists, HttpError, (ConnectionError,))
        self.assertFalse(failure.exception.retry)
        self.assertEqual(failure.exception.phase, 'verify_container')
        Exists.error_code = 'PRIVATE'
        with self.assertRaises(ready.FixtureFailure) as failure:
            cloud.azure_attempt(client, Exists, HttpError, (ConnectionError,))
        self.assertEqual(failure.exception.category, 'container_conflict')

    def test_scripts_and_images_use_the_tested_helpers(self):
        base = (ROOT / 'scripts/prepare-fixtures.sh').read_text()
        self.assertLess(base.index('scripts/fixture_tls.py'), base.index('docker compose build'))
        extended = (ROOT / 'scripts/prepare-extended-fixtures.sh').read_text()
        self.assertLess(extended.index('scripts/fixture_tls.py'), extended.index(' build ftps'))
        self.assertNotIn('for attempt', extended)
        self.assertNotIn('except', extended)
        for dockerfile in ('minio', 'extended'):
            self.assertIn('COPY fixture_readiness.py /fixture_readiness.py',
                          (ROOT / f'docker/{dockerfile}/Dockerfile').read_text())
        for workflow in ('ci.yml', 'coverage.yml'):
            self.assertIn('-p test_fixture_startup.py -v', (ROOT / '.github/workflows' / workflow).read_text())


class FixtureHealthTests(unittest.TestCase):
    def test_each_health_failure_has_no_response_exception_or_credentials(self):
        for service in ('ftps', 'webdav', 'smb'):
            for error in (OSError('PRIVATE'), ssl.SSLCertVerificationError('PRIVATE'),
                          health.ftplib.error_perm('PRIVATE'), ValueError('PRIVATE')):
                output = io.StringIO()
                with patch.object(sys, 'argv', ['health', service]), \
                        patch.object(health, 'probe', side_effect=error), contextlib.redirect_stdout(output):
                    self.assertEqual(health.main(), 1)
                self.assertNotIn('PRIVATE', output.getvalue())
                self.assertIn('fixture=' + service, output.getvalue())

    def test_ftps_checks_both_channels_and_listing_with_verified_context(self):
        client = Mock()
        with patch.object(health.ssl, 'create_default_context', return_value='verified') as context, \
                patch.object(health.ftplib, 'FTP_TLS', return_value=client) as factory:
            health.probe('ftps')
        context.assert_called_once_with(cafile='/certs/server.crt')
        factory.assert_called_once_with(context='verified', timeout=5)
        self.assertEqual([c[0] for c in client.mock_calls], ['connect', 'auth', 'login', 'prot_p', 'nlst', 'close'])

    def test_webdav_and_smb_require_application_success(self):
        with patch.object(health, 'exchange') as exchange:
            health.probe('webdav')
        self.assertEqual(exchange.call_args.args[0].method, 'PROPFIND')
        self.assertEqual(exchange.call_args.kwargs['allowed'], (207,))
        with patch.object(health.subprocess, 'run', return_value=Mock(returncode=1, stdout=b'PRIVATE')) as run, \
                self.assertRaises(ready.FixtureFailure):
            health.probe('smb')
        self.assertTrue(run.call_args.kwargs['capture_output'])
        self.assertIn('--option=client smb encrypt=required', run.call_args.args[0])

    def test_compose_healthchecks_use_redacted_probes(self):
        compose = (ROOT / 'compose.extended.yml').read_text()
        for service in ('ftps', 'webdav', 'smb'):
            self.assertIn(f'test: [CMD, python, /health.py, {service}]', compose)
