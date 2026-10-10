import json
import math
from pathlib import Path
import shutil
import socket
from statistics import median
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campaign_state import Campaign
from check_performance import UNRELIABLE, compare
from fixture_connections import BUFFERED, PROVIDERS
from performance_order import SCHEME, paired_order, validate_pairing
import qualify_transfers
import release_campaign
import release_evidence
import run_vm_campaign

ROOT = Path(__file__).resolve().parents[2]
POLICY = json.loads((ROOT / 'scripts/performance-policy.json').read_text())
ROUNDS, WORKERS = run_vm_campaign.PERFORMANCE_ROUNDS, 4
BINARIES = {'baseline': 'a' * 64, 'candidate': 'b' * 64}
ENVIRONMENT = {'machine': 'x86_64', 'kernel': 'test', 'cpu_count': 8, 'cpu_model': 'test', 'fixture_sha256': 'c' * 64}
OPERATIONS = ('test', 'put', 'get', 'stat', 'copy', 'delete', 'delete')


def report(name, rounds):
    return {'schema_version': 1, 'binary_sha256': BINARIES[name], 'platform': 'linux',
            'campaign_id': str(uuid.uuid4()), 'environment': ENVIRONMENT, 'source_revision': 'd' * 40,
            'dirty': False, 'payload_bytes': 1024**2, 'workers': WORKERS, 'rounds': rounds, 'spool_uploads': False,
            'rss_limit_bytes': 256 * 1024**2, 'status': 'PASS', 'results': []}


def measured(runs, rounds, slowdown):
    """Reports of a run executing `runs` (round, provider, role) in order: the
    workers of one run are concurrent, runs are sequential, and the elapsed
    time of the run at position `clock` is 0.2 s times 1 + slowdown(fraction
    of the run already done)."""
    reports = {name: report(name, rounds) for name in BINARIES}
    for clock, (iteration, provider, name) in enumerate(runs):
        elapsed = 0.2 * (1 + slowdown(clock / len(runs)))
        for _ in range(WORKERS):
            reports[name]['results'].append({
                'provider': provider, 'round': iteration, 'status': 'PASS', 'payload_bytes': 1024**2,
                'mode': 'buffered_roundtrip' if provider in BUFFERED else 'streaming_roundtrip',
                'measurements': [{'operation': operation, 'status': 'PASS', 'elapsed_seconds': elapsed,
                                  'peak_rss_bytes': 12 * 1024**2} for operation in OPERATIONS]})
    return reports['baseline'], reports['candidate']


def linear(drift):
    return lambda fraction: drift * fraction


def sequential(rounds):
    return [(r, p, name) for name in ('baseline', 'candidate') for r in range(rounds) for p in PROVIDERS]


def alternated(rounds):
    runs = []
    for slot in paired_order(rounds, PROVIDERS):
        second = 'candidate' if slot['first'] == 'baseline' else 'baseline'
        runs += [(slot['round'], slot['provider'], slot['first']), (slot['round'], slot['provider'], second)]
    return runs


def pair(baseline, candidate):
    order = paired_order(baseline['rounds'], PROVIDERS)
    for name, own, other in (('baseline', baseline, candidate), ('candidate', candidate, baseline)):
        own['paired_measurement'] = {'role': name, 'order_scheme': SCHEME,
                                     'partner_campaign_id': other['campaign_id'], 'order': order}


def paired_run(slowdown):
    baseline, candidate = measured(alternated(ROUNDS), ROUNDS, slowdown)
    pair(baseline, candidate)
    return baseline, candidate


def statistics(report_, provider, operation):
    """Median and p95 exactly as check_performance computes them."""
    values = sorted(m['elapsed_seconds'] for row in report_['results'] if row['provider'] == provider
                    for m in row['measurements'] if m['operation'] == operation)
    return median(values), values[math.ceil(len(values) * .95) - 1]


def largest_residual(baseline, candidate):
    """Largest relative difference between the roles, per compared statistic."""
    worst = [0.0, 0.0]
    for provider in PROVIDERS:
        for operation in set(OPERATIONS):
            for index, (old, new) in enumerate(zip(statistics(baseline, provider, operation),
                                                   statistics(candidate, provider, operation))):
                worst[index] = max(worst[index], abs(new / old - 1))
    return worst


class PairedOrderTests(unittest.TestCase):
    def test_every_provider_follows_abba_on_its_own_rounds(self):
        order = paired_order(ROUNDS, PROVIDERS)
        self.assertEqual(order, paired_order(ROUNDS, PROVIDERS))
        self.assertEqual([(slot['round'], slot['provider']) for slot in order],
                         [(r, p) for r in range(ROUNDS) for p in PROVIDERS])
        for provider in PROVIDERS:
            firsts = [slot['first'] for slot in order if slot['provider'] == provider]
            self.assertEqual(firsts, ['baseline', 'candidate', 'candidate', 'baseline'] * (ROUNDS // 4))

    def test_rounds_must_be_a_multiple_of_four(self):
        for rounds in (0, 2, 30, 31):
            with self.assertRaises(ValueError):
                paired_order(rounds, PROVIDERS)
        self.assertEqual(ROUNDS % 4, 0)

    def test_a_drift_the_guard_accepts_leaves_only_a_one_run_residual(self):
        # 8 % over the whole run: each binary moves 4 % between its halves,
        # inside the guard (half of the 10 % budget).
        baseline, candidate = paired_run(linear(0.08))
        comparison = compare(baseline, candidate, BINARIES['candidate'], POLICY)
        self.assertEqual(comparison['status'], 'PASS')
        self.assertTrue(all(row['status'] == 'STABLE' for row in comparison['stability']))
        # Runs are sequential, so at any order statistic the two roles' samples
        # are one run apart: the residual is one run's share of the drift.
        step = 0.08 / len(alternated(ROUNDS))
        median_residual, p95_residual = largest_residual(baseline, candidate)
        self.assertLessEqual(median_residual, step * 1.01)
        self.assertLessEqual(p95_residual, step * 1.01)
        # The same drift, run one binary after the other, biases the candidate
        # by half of it.
        old, new = measured(sequential(ROUNDS), ROUNDS, linear(0.08))
        self.assertGreater(largest_residual(old, new)[0], 0.035)

    def test_the_documented_residual_of_a_large_linear_drift(self):
        # The figures quoted in docs/release-campaign.md, for 50 % over the run.
        baseline, candidate = paired_run(linear(0.5))
        median_residual, p95_residual = largest_residual(baseline, candidate)
        self.assertAlmostEqual(median_residual * 100, 0.07, delta=0.01)
        self.assertAlmostEqual(p95_residual * 100, 0.06, delta=0.01)
        # ... and that drift is far beyond what the guard accepts.
        self.assertEqual(compare(baseline, candidate, BINARIES['candidate'], POLICY)['status'], UNRELIABLE)

    def test_a_jump_mid_run_is_unreliable_never_pass_or_regression(self):
        baseline, candidate = paired_run(lambda fraction: 0.25 if fraction >= 0.5 else 0.0)
        comparison = compare(baseline, candidate, BINARIES['candidate'], POLICY)
        self.assertEqual(comparison['status'], UNRELIABLE)
        self.assertTrue(any(row['status'] == 'UNSTABLE' for row in comparison['stability']))
        # Even when the candidate also regressed, the verdict is unreliable.
        for row in candidate['results']:
            for measure in row['measurements']:
                measure['elapsed_seconds'] *= 1.3
        self.assertEqual(compare(baseline, candidate, BINARIES['candidate'], POLICY)['status'], UNRELIABLE)

    def test_the_stability_allowance_is_five_percent_with_a_ten_millisecond_floor(self):
        from check_performance import stability_checks
        self.assertEqual(POLICY['stability_allowance']['median_percent'], 5)
        self.assertEqual(POLICY['stability_allowance']['minimum_seconds'], 0.010)

        def halves(first, second):
            own = report('baseline', 4)
            for iteration in range(4):
                elapsed = first if iteration < 2 else second
                own['results'].append({'provider': 'azure', 'round': iteration, 'measurements': [
                    {'operation': 'copy', 'elapsed_seconds': elapsed}]})
            return stability_checks(own, 'baseline', POLICY)[0]['status']
        # The accepted 2.1.0 campaign: azure copy moved 5.45 ms on a quiet host.
        self.assertEqual(halves(0.06530, 0.05985), 'STABLE')
        # A real drift of 3.0.0 day: smb copy -11.7 % on 90 ms.
        self.assertEqual(halves(0.09065, 0.08005), 'UNSTABLE')
        # Long operations follow the percentage: ftp copy +26 % on 5.3 s.
        self.assertEqual(halves(5.2785, 6.65395), 'UNSTABLE')

    def test_a_real_regression_still_fails_when_alternated(self):
        baseline, candidate = paired_run(linear(0.0))
        for row in candidate['results']:
            for measure in row['measurements']:
                measure['elapsed_seconds'] *= 1.3
        self.assertEqual(compare(baseline, candidate, BINARIES['candidate'], POLICY)['status'], 'FAIL')

    def test_paired_metadata_is_validated_never_read_as_absent(self):
        baseline, candidate = measured(alternated(ROUNDS), ROUNDS, linear(0.0))
        self.assertFalse(validate_pairing(baseline, candidate, PROVIDERS))
        pair(baseline, candidate)
        self.assertTrue(validate_pairing(baseline, candidate, PROVIDERS))
        for broken in (None, {}, [], 'ABBA'):
            with self.subTest(broken=broken):
                altered = dict(candidate, paired_measurement=broken)
                with self.assertRaises(ValueError):
                    compare(baseline, altered, BINARIES['candidate'], POLICY)
        order = paired_order(ROUNDS, PROVIDERS)
        wrong_types = [dict(order[0], round=True), dict(order[0], round=0.0), dict(order[0], first='other'),
                       dict(order[0], provider=7), dict(order[0], extra=1)]
        for field, value in [('order', order[::-1]), ('order', []), ('order', None), ('order_scheme', 'ABAB'),
                             ('role', 'baseline'), ('partner_campaign_id', 'x'),
                             *(('order', [slot] + order[1:]) for slot in wrong_types)]:
            with self.subTest(field=field, value=str(value)[:40]):
                broken = dict(candidate['paired_measurement'], **{field: value})
                altered = dict(candidate, paired_measurement=broken)
                with self.assertRaises(ValueError):
                    compare(baseline, altered, BINARIES['candidate'], POLICY)
        with self.assertRaises(ValueError):
            compare(baseline, {k: v for k, v in candidate.items() if k != 'paired_measurement'},
                    BINARIES['candidate'], POLICY)

    def test_release_evidence_from_3_0_0_requires_a_paired_run(self):
        baseline, candidate = measured(alternated(ROUNDS), ROUNDS, linear(0.0))
        release_evidence.require_paired_performance((2, 1, 0), baseline, candidate)
        with self.assertRaises(ValueError):
            release_evidence.require_paired_performance((3, 0, 0), baseline, candidate)
        pair(baseline, candidate)
        release_evidence.require_paired_performance((3, 0, 0), baseline, candidate)
        with self.assertRaises(ValueError):
            release_evidence.validate_transfers(candidate, BINARIES['candidate'], size=1024**2, workers=WORKERS,
                                                rounds=1)


class ProducerTests(unittest.TestCase):
    """Runs the real qualify_transfers loop, concurrent workers included, with
    a recording roundtrip in place of the fixtures and binaries."""

    def test_runs_follow_the_recorded_order_slot_by_slot(self):
        events, lock = [], threading.Lock()

        def roundtrip(binary, provider, *_arguments):
            with lock:
                events.append(('start', binary.name, provider))
            with lock:
                events.append(('end', binary.name, provider))
            return {'provider': provider, 'status': 'PASS', 'payload_bytes': 1024,
                    'mode': 'streaming_roundtrip', 'measurements': []}

        rounds = 4
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            for role in ('baseline', 'candidate'):
                (folder / role).write_bytes(role.encode())
            argv = ['qualify_transfers.py', '--bytes', '1024', '--workers', str(WORKERS), '--rounds', str(rounds),
                    '--baseline-binary', str(folder / 'baseline'), '--baseline-output', str(folder / 'b.json'),
                    '--output', str(folder / 'c.json')]
            with patch.object(sys, 'argv', argv), patch.object(qualify_transfers.sys, 'platform', 'linux'), \
                    patch.object(qualify_transfers, 'roundtrip', roundtrip), \
                    patch.object(qualify_transfers, 'measurement_environment', return_value=ENVIRONMENT), \
                    patch.dict('os.environ', {'PLENORA_CLI_BIN': str(folder / 'candidate')}):
                qualify_transfers.main()
            reports = {role: json.loads((folder / name).read_text())
                       for role, name in (('baseline', 'b.json'), ('candidate', 'c.json'))}
        expected = paired_order(rounds, PROVIDERS)
        for role, own in reports.items():
            self.assertEqual(own['paired_measurement']['order'], expected)
            self.assertEqual(own['paired_measurement']['role'], role)
            self.assertEqual(len(own['results']), rounds * len(PROVIDERS) * WORKERS)
        per_slot = 4 * WORKERS
        self.assertEqual(len(events), per_slot * len(expected))
        for index, slot in enumerate(expected):
            window = events[index * per_slot:(index + 1) * per_slot]
            second = 'candidate' if slot['first'] == 'baseline' else 'baseline'
            self.assertEqual([event[1] for event in window], [slot['first']] * 2 * WORKERS + [second] * 2 * WORKERS,
                             slot)
            self.assertTrue(all(event[2] == slot['provider'] for event in window))

    def test_a_paired_run_refuses_rounds_that_are_not_a_multiple_of_four(self):
        argv = ['qualify_transfers.py', '--rounds', '30', '--baseline-binary', 'b', '--baseline-output', 'b.json']
        with patch.object(sys, 'argv', argv), patch.object(qualify_transfers.sys, 'platform', 'linux'), \
                self.assertRaises(SystemExit):
            qualify_transfers.main()


class RetryTests(unittest.TestCase):
    def test_a_retry_of_a_passed_phase_is_refused_not_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            campaign = Campaign(Path(temporary), {'subject': 'same'})
            campaign.phase('performance-ab', lambda path: (path / 'report.json').write_text('passed'))
            with self.assertRaises(ValueError):
                campaign.validate_retries(['performance-ab'], run_vm_campaign.PHASES)
            with self.assertRaises(ValueError):
                campaign.phase('performance-ab', lambda path: self.fail('remeasured'), retry=True, reason='again')
            with self.assertRaises(ValueError):
                campaign.validate_retries(['performance-baseline'], run_vm_campaign.PHASES)

            def failure(path):
                (path / 'report.json').write_text('failed')
                raise RuntimeError('budget')
            with self.assertRaises(RuntimeError):
                campaign.phase('performance-compare', failure)
            campaign.validate_retries(['performance-compare'], run_vm_campaign.PHASES)
            result = campaign.phase('performance-compare', lambda path: (path / 'report.json').write_text('ok'),
                                    retry=True, reason='compare again')
            self.assertEqual(result.name, '2')

    def test_vm_retries_need_a_new_vm_attempt_and_an_executed_phase(self):
        release_campaign.validate_vm_retries([], [], '3.0.0')
        release_campaign.validate_vm_retries(['soak'], ['qualify-vm'], '3.0.0')
        with self.assertRaises(ValueError):
            release_campaign.validate_vm_retries(['soak'], [], '3.0.0')
        with self.assertRaises(ValueError):
            release_campaign.validate_vm_retries(['spooled-large'], ['qualify-vm'], '2.0.1')
        with self.assertRaises(ValueError):
            release_campaign.validate_vm_retries(['performance-baseline'], ['qualify-vm'], '3.0.0')
        with tempfile.TemporaryDirectory() as temporary:
            campaign = Campaign(Path(temporary), {'subject': 'same'})
            campaign.phase('qualify-vm', lambda path: (path / 'report.json').write_text('passed'))
            with self.assertRaises(ValueError):
                campaign.validate_retries(['qualify-vm'], release_campaign.LOCAL_PHASES)

    def test_phases_follow_the_version(self):
        self.assertIn('performance-ab', run_vm_campaign.PHASES)
        self.assertNotIn('performance-baseline', run_vm_campaign.PHASES)
        self.assertEqual(run_vm_campaign.PERFORMANCE_ORDER, SCHEME)
        self.assertNotIn('spooled-large', run_vm_campaign.phases_for('2.0.1'))
        self.assertIn('spooled-large', run_vm_campaign.phases_for('3.0.0'))


class FixtureDefinitionTests(unittest.TestCase):
    def test_fake_gcs_keeps_no_state_that_slows_listing_down(self):
        # Plain text: the test tooling has no YAML parser.
        lines = (ROOT / 'compose.extended.yml').read_text().splitlines()
        service = lines.index('  gcs:')
        command = next(line for line in lines[service:] if line.strip().startswith('command:'))
        self.assertIn('"-backend", "memory"', command)


class MemoryTests(unittest.TestCase):
    def test_the_gcs_peak_and_reserve_must_be_available(self):
        import check_memory
        gib = check_memory.GIB
        self.assertEqual(check_memory.inspect(4 * gib)['status'], 'PASS')
        self.assertEqual(check_memory.inspect(4 * gib - 1)['status'], 'FAIL')
        self.assertEqual(check_memory.GCS_PEAK_BYTES, 2 * gib)
        with tempfile.TemporaryDirectory() as temporary:
            meminfo = Path(temporary) / 'meminfo'
            meminfo.write_text('MemTotal: 16000000 kB\nMemAvailable: 1024 kB\n')
            self.assertEqual(check_memory.available(meminfo), 1024 * 1024)
            for broken in ('MemTotal: 16000000 kB\n', 'MemAvailable: 1024 MB\n', 'MemAvailable: 1024\n',
                           'MemAvailable: 1024 kB\nMemAvailable: 2048 kB\n', 'MemAvailable: -1 kB\n',
                           'MemAvailable: x kB\n'):
                with self.subTest(broken=broken):
                    meminfo.write_text(broken)
                    with self.assertRaises(ValueError):
                        check_memory.available(meminfo)


class Server:
    """A local TCP server that answers every connection with fixed bytes."""

    def __init__(self, reply):
        self.reply = reply
        self.listener = socket.create_server(('127.0.0.1', 0))
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            with connection:
                connection.settimeout(2)
                try:
                    connection.sendall(self.reply)
                    connection.recv(4096)
                except OSError:
                    pass

    def close(self):
        self.listener.close()


BANNERS = {
    'http-503': b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n',
    'http-200': b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n',
    'ftp': b'220 fixture ready\r\n',
    'ssh': b'SSH-2.0-fixture\r\n',
    'smb': b'\x00\x00\x00\x04junk',
}


class TlsFtpServer:
    """A minimal explicit-TLS FTP server: AUTH TLS, login, PBSZ/PROT and one
    passive NLST over TLS, enough for a real handshake on both channels."""

    def __init__(self, certificate, key, data=None):
        import ssl
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(certificate, key)
        self.data_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.data_context.load_cert_chain(*(data or (certificate, key)))
        self.listener = socket.create_server(('127.0.0.1', 0))
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        try:
            connection, _ = self.listener.accept()
        except OSError:
            return
        opened = [connection]
        try:
            self.session(connection, opened)
        except OSError:
            pass
        finally:
            for item in reversed(opened):
                item.close()

    def session(self, connection, opened):
        def send(line):
            connection.sendall(line.encode() + b'\r\n')

        def receive():
            data = b''
            while not data.endswith(b'\r\n'):
                chunk = connection.recv(1)
                if not chunk:
                    raise OSError('closed')
                data += chunk
            return data.decode().strip()

        send('220 fixture')
        data_listener = None
        while True:
            command = receive().split(' ', 1)[0].upper()
            if command == 'AUTH':
                send('234 TLS')
                connection = self.context.wrap_socket(connection, server_side=True)
                opened.append(connection)
            elif command == 'USER':
                send('331 password')
            elif command == 'PASS':
                send('230 logged in')
            elif command in ('PBSZ', 'PROT', 'TYPE'):
                send('200 ok')
            elif command == 'PASV':
                data_listener = socket.create_server(('127.0.0.1', 0))
                opened.append(data_listener)
                port = data_listener.getsockname()[1]
                send(f'227 Entering Passive Mode (127,0,0,1,{port // 256},{port % 256})')
            elif command == 'NLST':
                data, _ = data_listener.accept()
                send('150 listing')
                data = self.data_context.wrap_socket(data, server_side=True)
                data.sendall(b'file\r\n')
                data.unwrap().close()
                send('226 done')
            else:
                send('221 bye')
                return

    def close(self):
        self.listener.close()


def certificate(folder, name):
    """A self-signed certificate for `name`, made with the openssl CLI."""
    import subprocess
    certificate, key = folder / (name + '.crt'), folder / (name + '.key')
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-keyout', str(key),
                    '-out', str(certificate), '-subj', '/CN=' + name, '-addext', 'subjectAltName=DNS:' + name],
                   check=True, capture_output=True)
    return certificate, key


@unittest.skipUnless(shutil.which('openssl'), 'needs the openssl CLI')
class FtpsHandshakeTests(unittest.TestCase):
    """Real TLS handshakes against the FTPS probe. The probe connects to
    127.0.0.1, like a probe after --connect-host, and verifies the certificate
    for the fixture's own name."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        folder = Path(self.folder.name)
        self.trusted = certificate(folder, 'fixture.invalid')
        self.stranger = certificate(folder, 'stranger.invalid')

    def tearDown(self):
        self.folder.cleanup()

    def probe(self, served, ca, name, data=None):
        import check_fixtures
        server = TlsFtpServer(*served, data=data)
        try:
            check_fixtures.ftp_list('127.0.0.1', server.port, 'user', 'secret', tls_ca=ca, tls_name=name)
        finally:
            server.close()

    def test_the_fixture_identity_is_verified_on_another_address(self):
        self.probe(self.trusted, self.trusted[0], 'fixture.invalid')

    def test_an_untrusted_certificate_fails_the_handshake(self):
        import check_fixtures
        with self.assertRaises(check_fixtures.ProbeFailure):
            self.probe(self.stranger, self.trusted[0], 'fixture.invalid')

    def test_a_wrong_certificate_on_the_data_channel_alone_fails(self):
        import check_fixtures
        with self.assertRaises(check_fixtures.ProbeFailure):
            self.probe(self.trusted, self.trusted[0], 'fixture.invalid', data=self.stranger)

    def test_a_certificate_for_another_name_fails(self):
        import check_fixtures
        with self.assertRaises(check_fixtures.ProbeFailure):
            self.probe(self.trusted, self.trusted[0], 'other.invalid')


class ProbeTests(unittest.TestCase):
    """Every application probe fails against a server that only answers a
    banner, an error status or a bare success."""

    def failing(self, banner, probe):
        import check_fixtures
        server = Server(BANNERS[banner])
        try:
            with self.assertRaises((check_fixtures.ProbeFailure, OSError)):
                probe(check_fixtures, server.port)
        finally:
            server.close()

    def test_http_probes_need_their_authenticated_answer(self):
        for banner in ('http-503', 'http-200'):
            with self.subTest(banner=banner):
                self.failing(banner, lambda c, port: c.webdav_propfind(f'http://127.0.0.1:{port}/'))
        self.failing('http-503', lambda c, port: c.s3_head_bucket(f'http://127.0.0.1:{port}'))
        self.failing('http-503', lambda c, port: c.azure_list(f'http://127.0.0.1:{port}/devstoreaccount1'))
        self.failing('http-503', lambda c, port: c.gcs_bucket(f'http://127.0.0.1:{port}'))

    def test_tls_probe_refuses_a_plain_banner(self):
        import ssl
        self.failing('http-200', lambda c, port: c.s3_head_bucket(f'https://127.0.0.1:{port}',
                                                                   ssl.create_default_context()))

    def test_ftp_probes_need_login_and_listing(self):
        self.failing('ftp', lambda c, port: c.ftp_list('127.0.0.1', port, 'user', 'secret'))
        self.failing('ftp', lambda c, port: c.ftp_list('127.0.0.1', port, 'user', 'secret',
                                                       tls_ca=ROOT / 'scripts/check_fixtures.py'))

    @unittest.skipUnless(shutil.which('ssh-keyscan') and shutil.which('sftp'), 'OpenSSH client not available')
    def test_sftp_probe_needs_the_pinned_key_and_a_listing(self):
        self.failing('ssh', lambda c, port: c.sftp_list('127.0.0.1', port, Path('missing-key'), 'SHA256:pin'))

    @unittest.skipUnless(shutil.which('smbclient'), 'smbclient not available')
    def test_smb_probe_needs_a_session_and_a_listing(self):
        self.failing('smb', lambda c, port: c.run_probe(c.smbclient_command('127.0.0.1', port, 'storage'),
                                                        'SMB listing failed'))

    def test_a_failed_container_probe_fails_the_check(self):
        import check_fixtures
        healthy = {service: {'Service': service, 'State': 'running', 'Health': 'healthy'}
                   for service in check_fixtures.SERVICES}
        passing = {service: (lambda: None) for service in check_fixtures.SERVICES}
        with patch.object(check_fixtures, 'containers', return_value=healthy), \
                patch.object(check_fixtures, 'probes', return_value=passing):
            self.assertEqual(check_fixtures.inspect('vm.invalid')['status'], 'PASS')

        def refused():
            raise check_fixtures.ProbeFailure('banner only')
        for service in check_fixtures.SERVICES:
            with self.subTest(service=service), patch.object(check_fixtures, 'containers', return_value=healthy), \
                    patch.object(check_fixtures, 'probes', return_value=dict(passing, **{service: refused})):
                report = check_fixtures.inspect('vm.invalid')
                self.assertEqual(report['status'], 'FAIL')
                self.assertEqual([r['service'] for r in report['results'] if r['status'] == 'FAIL'], [service])
        unhealthy = dict(healthy, webdav={'Service': 'webdav', 'State': 'running', 'Health': 'starting'})
        with patch.object(check_fixtures, 'containers', return_value=unhealthy), \
                patch.object(check_fixtures, 'probes', return_value=passing):
            self.assertEqual(check_fixtures.inspect('vm.invalid')['status'], 'FAIL')
        with patch.object(check_fixtures.subprocess, 'run') as run:
            run.return_value.returncode = 1
            with self.assertRaises(check_fixtures.ProbeFailure):
                check_fixtures.run_probe(['smbclient'], 'SMB listing failed')


if __name__ == '__main__':
    unittest.main()
