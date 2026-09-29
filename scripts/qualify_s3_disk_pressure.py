"""Reproduce MinIO disk pressure inside a bounded disposable tmpfs, never the host disk."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

from fixture_s3 import error_code, request

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'plenora-storage-minio-fixture:2025-09-07'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def qualify(binary, output):
    report = {'schema_version': 1, 'status': 'RUNNING',
              'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
              'source_revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip()),
              'scope': 'disposable MinIO fixture with a 256 MiB tmpfs', 'results': []}
    report['tool_sha256'] = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                            for name in ('qualify_s3_disk_pressure.py', 'fixture_s3.py')}
    output.parent.mkdir(parents=True, exist_ok=True)
    name = 'storage-pressure-' + uuid.uuid4().hex
    created = False

    def docker(*args):
        result = subprocess.run(['docker', *args], capture_output=True, text=True)
        require(result.returncode == 0, 'isolated fixture command failed')
        return result.stdout.strip()

    try:
        docker('run', '-d', '--name', name, '--tmpfs', '/data:rw,size=268435456',
               '-p', '127.0.0.1::9000', '-e', 'MINIO_ROOT_USER=plenora-dev',
               '-e', 'MINIO_ROOT_PASSWORD=plenora-dev-secret', IMAGE, 'server', '/data')
        created = True
        endpoint = 'http://' + docker('port', name, '9000/tcp')
        for attempt in range(30):
            try:
                if request(endpoint, 'PUT', '/plenora-test')[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(1)
        else:
            raise ValueError('isolated MinIO did not become ready')
        payload, sentinel = b'x' * (8 * 1024**2), b'preserve-destination'
        require(request(endpoint, 'PUT', '/plenora-test/source', payload)[0] == 200, 'fixture source creation failed')
        require(request(endpoint, 'PUT', '/plenora-test/destination', sentinel)[0] == 200, 'fixture destination creation failed')
        pressure = '''import json, os
v = os.statvfs('/data')
assert 0 < v.f_blocks * v.f_frsize <= 268435456
before = v.f_bavail * v.f_frsize
remaining = before - 2 * 1024**2
assert remaining > 0
with open('/data/.qualification-pressure', 'xb') as f:
    block = bytes(1024**2)
    while remaining:
        n = min(remaining, len(block))
        f.write(block[:n])
        remaining -= n
    f.flush()
    os.fsync(f.fileno())
v = os.statvfs('/data')
print(json.dumps({'total_bytes': v.f_blocks * v.f_frsize, 'free_before': before, 'free_under_pressure': v.f_bavail * v.f_frsize}))
'''
        report['disk'] = json.loads(docker('exec', name, 'python', '-c', pressure))
        time.sleep(3)  # Allow the server's cached filesystem statistics to expire.
        # Capture the precise backend code independently of public redaction.
        status, body = request(endpoint, 'PUT', '/plenora-test/probe', headers={'x-amz-copy-source': '/plenora-test/source'})
        report['backend_error'] = {'http_status': status, 'code': error_code(body)}
        require(error_code(body) == 'XMinioStorageFull', 'backend did not reject copy for disk pressure')
        with tempfile.TemporaryDirectory(prefix='storage-pressure-client-') as temporary:
            work = Path(temporary)
            connection = work / 'connection.json'
            connection.write_text(json.dumps({'provider': 's3', 'config_contract': 'plenora-storage-s3-connection-v1',
                'config': {'endpoint': endpoint, 'bucket': 'plenora-test', 'region': 'us-east-1', 'virtual_hosted_style': False},
                'credential_ref': 'env:PLENORA_TEST_CREDENTIALS'}))
            env = dict(os.environ, PLENORA_TEST_CREDENTIALS=json.dumps({'access_key_id': 'plenora-dev', 'secret_access_key': 'plenora-dev-secret'}))

            def copy():
                result = subprocess.run([str(binary), '--format', 'json', '--allow-private-network', '--allow-insecure-http',
                    'copy', '--connection', str(connection), '--source-key', 'source', '--destination-key', 'destination',
                    '--overwrite', 'true', '--publication-policy', 'atomic-required'], capture_output=True, env=env, timeout=60)
                require(not result.stderr and len(result.stdout.splitlines()) == 1, 'invalid public CLI response')
                require(not any(value in result.stdout for value in [endpoint.encode(), b'plenora-dev-secret', sentinel, payload[:1024]]),
                        'public CLI response leaked fixture data')
                return result.returncode, json.loads(result.stdout)

            code, response = copy()
            axes = {key: response.get('error', {}).get(key) for key in ('code', 'category', 'phase', 'remote_effect', 'retry')}
            require(code != 0 and axes == {'code': 'PROVIDER_MUTATION_FAILED', 'category': 'execution', 'phase': 'commit',
                    'remote_effect': 'unknown', 'retry': {'kind': 'requires_recovery'}}, 'disk pressure changed public error axes')
            require(request(endpoint, 'GET', '/plenora-test/destination')[1] == sentinel, 'failed copy changed destination')
            report['results'].append({'case': 'disk_pressure', 'status': 'PASS', 'public_error': axes, 'destination_preserved': True})
            docker('exec', name, 'python', '-c', "import os; os.unlink('/data/.qualification-pressure')")
            time.sleep(3)
            code, response = copy()
            require(code == 0 and response['status'] == 'ok', 'copy did not recover after pressure removal')
            require(request(endpoint, 'GET', '/plenora-test/destination')[1] == payload, 'recovered copy differs from source')
            report['results'].append({'case': 'recovery', 'status': 'PASS', 'checksum_verified': True})
        report['status'] = 'PASS'
    except BaseException:
        report['status'] = 'FAIL'
        raise
    finally:
        if created:
            try:
                docker('rm', '-f', name)
            except BaseException:
                report['status'] = 'FAIL'
                raise
            finally:
                output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        else:
            output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print('PASS isolated MinIO disk pressure and recovery')


def validate(report, revision, binary):
    require(report['status'] == 'PASS' and report['source_revision'] == revision and report['dirty'] is False,
            'disk pressure evidence must identify clean qualified source')
    require(report['binary_sha256'] == binary, 'disk pressure evidence uses a different binary')
    require(report['tool_sha256'] == {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                    for name in ('qualify_s3_disk_pressure.py', 'fixture_s3.py')},
            'disk pressure tool identity differs')
    require(report['backend_error'] == {'http_status': 507, 'code': 'XMinioStorageFull'},
            'disk pressure did not reproduce the real backend rejection')
    disk = report['disk']
    require(0 < disk['free_under_pressure'] <= 2 * 1024**2 < disk['free_before'] <= disk['total_bytes'] == 256 * 1024**2,
            'disk pressure was not confined to the bounded fixture')
    require(report['results'] == [
        {'case': 'disk_pressure', 'status': 'PASS', 'public_error': {
            'code': 'PROVIDER_MUTATION_FAILED', 'category': 'execution', 'phase': 'commit',
            'remote_effect': 'unknown', 'retry': {'kind': 'requires_recovery'}}, 'destination_preserved': True},
        {'case': 'recovery', 'status': 'PASS', 'checksum_verified': True}],
        'disk pressure or recovery evidence is incomplete')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    qualify(args.binary.resolve(), args.output.resolve())
