"""Verify that every qualification fixture serves real requests, from the VM.

Run after the fixture preparation, from the checkout on the VM. Compose must
report every fixture service running, and healthy where it declares a health
check. Each fixture must then complete an application exchange with the public
test identities of scripts/fixture_connections.py: an authenticated listing or
bucket request, over TLS verified against the fixture CA where the fixture
uses TLS. A banner, an error status or an unverified certificate is a failure.
The report names services and checks only, never addresses, credentials or
server messages.

MinIO TLS has no published port, and the Python standard library has no SMB
client: the MinIO TLS probe runs this script inside the minio-init container,
and the SMB probe runs smbclient inside the SMB fixture.
"""
import argparse
import base64
from datetime import datetime, timezone
import email.utils
import ftplib
import hashlib
import hmac
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ['docker', 'compose', '-f', 'docker-compose.yml', '-f', 'compose.extended.yml']
SERVICES = ('minio', 'minio-tls', 'sftp', 'ftp', 'azure', 'gcs', 'ftps', 'webdav', 'smb')
TIMEOUT = 15
S3_KEY, S3_SECRET = 'plenora-dev', 'plenora-dev-secret'
AZURE_ACCOUNT = 'devstoreaccount1'
AZURE_KEY = 'Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=='
FIXTURE_USER, FIXTURE_SECRET = 'plenora', 'plenora-fixture-secret'


class ProbeFailure(Exception):
    """A fixture did not complete its exchange."""


def require(condition, message):
    if not condition:
        raise ProbeFailure(message)


def http(request, context=None):
    """Status of an HTTP exchange; an HTTP error status is returned, not raised."""
    try:
        with urlopen(request, timeout=TIMEOUT, context=context) as response:
            response.read(65536)
            return response.status
    except HTTPError as error:
        return error.code


def s3_head_bucket(endpoint, context=None):
    """SigV4-signed HEAD of the fixture bucket: 200 only with valid credentials."""
    now = datetime.now(timezone.utc)
    timestamp, date = now.strftime('%Y%m%dT%H%M%SZ'), now.strftime('%Y%m%d')
    empty = hashlib.sha256(b'').hexdigest()
    host = urlsplit(endpoint).netloc
    signed = 'host;x-amz-content-sha256;x-amz-date'
    canonical = (f'HEAD\n/plenora-test\n\nhost:{host}\nx-amz-content-sha256:{empty}\nx-amz-date:{timestamp}\n'
                 f'\n{signed}\n{empty}')
    scope = f'{date}/us-east-1/s3/aws4_request'
    to_sign = f'AWS4-HMAC-SHA256\n{timestamp}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}'
    key = ('AWS4' + S3_SECRET).encode()
    for part in (date, 'us-east-1', 's3', 'aws4_request'):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    request = Request(endpoint + '/plenora-test', method='HEAD', headers={
        'x-amz-date': timestamp, 'x-amz-content-sha256': empty,
        'Authorization': f'AWS4-HMAC-SHA256 Credential={S3_KEY}/{scope}, SignedHeaders={signed}, Signature={signature}'})
    require(http(request, context) == 200, 'bucket request not authorised')


def azure_list(endpoint):
    """SharedKey-signed listing of the fixture container: 200 only with the key."""
    date = email.utils.formatdate(usegmt=True)
    path = urlsplit(endpoint).path.rstrip('/') + '/plenora-test'
    headers = {'x-ms-date': date, 'x-ms-version': '2023-11-03'}
    canonical = ('GET\n' + '\n' * 11 + ''.join(f'{name}:{headers[name]}\n' for name in sorted(headers))
                 + f'/{AZURE_ACCOUNT}{path}\ncomp:list\nrestype:container')
    signature = base64.b64encode(hmac.new(base64.b64decode(AZURE_KEY), canonical.encode(), hashlib.sha256).digest())
    request = Request(endpoint.rstrip('/') + '/plenora-test?restype=container&comp=list', headers={
        **headers, 'Authorization': f'SharedKey {AZURE_ACCOUNT}:{signature.decode()}'})
    require(http(request) == 200, 'container listing not authorised')


def gcs_bucket(endpoint):
    request = Request(endpoint.rstrip('/') + '/storage/v1/b/plenora-test',
                      headers={'Authorization': 'Bearer fixture-token'})
    require(http(request) == 200, 'bucket metadata not served')


def webdav_propfind(endpoint):
    token = base64.b64encode(f'{FIXTURE_USER}:{FIXTURE_SECRET}'.encode()).decode()
    request = Request(endpoint, method='PROPFIND', data=b'', headers={'Depth': '0', 'Authorization': 'Basic ' + token})
    require(http(request) == 207, 'authenticated PROPFIND did not return 207')


def ftp_list(host, port, user, secret, *, tls_ca=None):
    """Login and listing; with `tls_ca`, explicit TLS verified against that CA."""
    if tls_ca is None:
        client = ftplib.FTP(timeout=TIMEOUT)
    else:
        client = ftplib.FTP_TLS(context=ssl.create_default_context(cafile=str(tls_ca)), timeout=TIMEOUT)
    try:
        client.connect(host, port)
        if tls_ca is not None:
            client.auth()
        client.login(user, secret)
        if tls_ca is not None:
            client.prot_p()
        client.nlst()
    except (*ftplib.all_errors, ssl.SSLError) as error:
        raise ProbeFailure('login or listing failed') from error
    finally:
        client.close()


def sftp_list(host, port, key, fingerprint):
    """Key login and listing, with the host key verified against the recorded fingerprint."""
    with tempfile.TemporaryDirectory() as folder:
        known = Path(folder) / 'known_hosts'
        scan = subprocess.run(['ssh-keyscan', '-T', str(TIMEOUT), '-t', 'ed25519', '-p', str(port), host],
                              capture_output=True, text=True, check=False)
        require(scan.returncode == 0 and scan.stdout.strip(), 'host key not served')
        known.write_text(scan.stdout)
        listed = subprocess.run(['ssh-keygen', '-lf', str(known)], capture_output=True, text=True, check=False)
        require(listed.returncode == 0 and fingerprint in listed.stdout.split(), 'host key differs from the fixture pin')
        session = subprocess.run(
            ['sftp', '-b', '-', '-i', str(key), '-P', str(port), '-o', 'BatchMode=yes',
             '-o', f'UserKnownHostsFile={known}', '-o', 'StrictHostKeyChecking=yes', f'plenora@{host}'],
            input='ls upload\n', capture_output=True, text=True, timeout=TIMEOUT * 4, check=False)
        require(session.returncode == 0, 'key login or listing failed')


def smbclient_command(host, port, share):
    """Encrypted SMB3 session, tree connect and listing of `share`."""
    return ['smbclient', f'//{host}/{share}', '-p', str(port), '-U', f'{FIXTURE_USER}%{FIXTURE_SECRET}',
            '-m', 'SMB3', '--option=client smb encrypt=required', '-c', 'ls']


def run_probe(command, message):
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=TIMEOUT * 8, check=False)
    require(result.returncode == 0, message)


def containers():
    output = subprocess.check_output([*COMPOSE, 'ps', '--all', '--format', 'json'], cwd=ROOT, text=True)
    rows = []
    for line in output.splitlines():
        line = line.strip()
        if line:
            value = json.loads(line)
            rows.extend(value if isinstance(value, list) else [value])
    return {row['Service']: row for row in rows}


def probes(host):
    fixtures = ROOT / '.fixtures'
    return {
        'minio': lambda: s3_head_bucket('http://127.0.0.1:9000'),
        'minio-tls': lambda: run_probe(
            [*COMPOSE, 'run', '--rm', '--no-deps', '-v', f'{ROOT / "scripts"}:/probe:ro', '--entrypoint', 'python',
             'minio-init', '/probe/check_fixtures.py', '--inside', 'minio-tls'], 'MinIO TLS request failed'),
        'sftp': lambda: sftp_list('127.0.0.1', 2222, fixtures / 'sftp-client',
                                  (fixtures / 'sftp-fingerprint').read_text().strip()),
        'ftp': lambda: ftp_list('127.0.0.1', 2121, 'plenora', 'plenora-ftp-secret'),
        'ftps': lambda: ftp_list(host, 2122, FIXTURE_USER, FIXTURE_SECRET, tls_ca=fixtures / 'extended/server.crt'),
        'azure': lambda: azure_list(f'http://127.0.0.1:10000/{AZURE_ACCOUNT}'),
        'gcs': lambda: gcs_bucket('http://127.0.0.1:4443'),
        'webdav': lambda: webdav_propfind('http://127.0.0.1:8088/'),
        'smb': lambda: run_probe([*COMPOSE, 'exec', '-T', 'smb', *smbclient_command('127.0.0.1', 445, 'storage')],
                                 'SMB session, tree connect or listing failed'),
    }


def inspect(host):
    states = containers()
    results = []
    for service in SERVICES:
        row = states.get(service, {})
        running = row.get('State') == 'running' and row.get('Health', '') in ('', 'healthy')
        results.append({'service': service, 'check': 'container', 'status': 'PASS' if running else 'FAIL'})
    for service, check in probes(host).items():
        try:
            check()
            status = 'PASS'
        except (ProbeFailure, OSError, URLError, ssl.SSLError, subprocess.SubprocessError, ValueError):
            status = 'FAIL'
        results.append({'service': service, 'check': 'application', 'status': status})
    return {'schema_version': 2, 'status': 'PASS' if all(r['status'] == 'PASS' for r in results) else 'FAIL',
            'results': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--inside', choices=('minio-tls',),
                        help='run one probe from inside the fixture network')
    args = parser.parse_args()
    if args.inside:
        try:
            s3_head_bucket('https://minio-tls:9000', ssl.create_default_context(cafile='/certs/ca.crt'))
        except (ProbeFailure, OSError, URLError, ssl.SSLError):
            sys.exit(1)
        return
    if args.output is None:
        parser.error('--output is required')
    report = inspect(os.environ.get('PLENORA_FIXTURE_HOST', '127.0.0.1'))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    if report['status'] != 'PASS':
        failed = sorted({row['service'] for row in report['results'] if row['status'] != 'PASS'})
        sys.exit('fixtures not serving requests: ' + ', '.join(failed))
    print('PASS every fixture serves requests')


if __name__ == '__main__':
    main()
