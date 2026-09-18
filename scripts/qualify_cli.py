"""Exercise all seven operations against the isolated Docker fixtures."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('PLENORA_CLI_BIN', str(ROOT / 'target/debug/plenora-storage'))).resolve()
PREFIX = 'qualification/' + uuid.uuid4().hex
SECRETS = ['plenora-dev-secret', 'plenora-sftp-secret', 'plenora-ftp-secret']


def invoke(connection, flags, command, *args, expected=0):
    result = subprocess.run([str(BINARY), '--format', 'json',
        '--allow-private-network', *flags, command, '--connection', str(connection), *args],
        capture_output=True, text=True, timeout=90)
    assert not result.stderr, result.stderr
    assert not any(secret in result.stdout for secret in SECRETS), 'secret in public output'
    assert len(result.stdout.splitlines()) == 1, result.stdout
    envelope = json.loads(result.stdout)
    if expected is None:
        return result.returncode, envelope
    assert result.returncode == expected, envelope
    return envelope.get('result') if expected == 0 else envelope['error']


def main():
    observations = []
    with tempfile.TemporaryDirectory(prefix='storage-qualification-') as directory:
        work = Path(directory)
        payload = bytes(range(256)) * 32768 + b'last-part'
        source = work / 'source.bin'
        source.write_bytes(payload)
        old = work / 'old.bin'
        old.write_bytes(b'old')
        empty = work / 'empty.bin'
        empty.write_bytes(b'')
        for provider in ['s3', 'sftp', 'ftp']:
            configuration = json.loads((ROOT / 'docker' / f'{"minio" if provider == "s3" else provider}-connection.json').read_text())
            flags = ['--allow-insecure-ftp'] if provider == 'ftp' else []
            if provider == 's3':
                if os.environ.get('PLENORA_QUALIFY_ALLOW_HTTP') == '1':
                    configuration['config']['endpoint'] = os.environ['PLENORA_MINIO_ENDPOINT']
                    flags.append('--allow-insecure-http')
                else:
                    configuration['config']['endpoint'] = os.environ['PLENORA_MINIO_TLS_ENDPOINT']
            if provider in ['sftp', 'ftp']:
                configuration['config']['host'] = os.environ.get(f'PLENORA_{provider.upper()}_ENDPOINT', configuration['config']['host'])
                configuration['config']['port'] = int(os.environ.get(f'PLENORA_{provider.upper()}_PORT', configuration['config']['port']))
            if provider == 'sftp':
                configuration['config']['host_key_sha256'] = os.environ['PLENORA_SFTP_HOST_KEY_SHA256']
            connection = work / f'{provider}.json'
            connection.write_text(json.dumps(configuration))
            policy = 'best-effort' if provider == 'ftp' else 'atomic-required'
            key = PREFIX + '/' + provider
            source_key, destination_key, empty_key = key + '/source', key + '/destination', key + '/empty'
            assert invoke(connection, flags, 'test')['reachable']
            put = invoke(connection, flags, 'put', '--key', source_key, '--input', str(source), '--overwrite', 'true', '--publication-policy', policy)
            assert put['bytes_transferred'] == len(payload)
            assert put['checksum']['value'] == hashlib.sha256(payload).hexdigest()
            invoke(connection, flags, 'put', '--key', destination_key, '--input', str(old), '--overwrite', 'true', '--publication-policy', policy)
            copied = invoke(connection, flags, 'copy', '--source-key', source_key, '--destination-key', destination_key, '--overwrite', 'true', '--publication-policy', policy)
            assert copied['size'] == len(payload)
            output = work / f'{provider}.bin'
            invoke(connection, flags, 'get', '--key', destination_key, '--output', str(output), '--overwrite', 'false')
            assert output.read_bytes() == payload
            assert invoke(connection, flags, 'stat', '--key', destination_key)['size'] == len(payload)
            listed = invoke(connection, flags, 'list', '--prefix', key, '--max-items', '1', '--all')
            assert [item['key'] for item in listed['objects']] == [destination_key, source_key]
            assert not listed['truncated'] and listed['next_cursor'] is None
            # An invalid size must not truncate a previously published object.
            invoke(connection, flags, 'put', '--key', destination_key, '--input', str(source), '--overwrite', 'true', '--publication-policy', 'best-effort', '--max-transfer-bytes', '1', expected=4)
            assert invoke(connection, flags, 'stat', '--key', destination_key)['size'] == len(payload)
            error = invoke(connection, flags, 'put', '--key', destination_key, '--input', str(old), '--overwrite', 'false', '--publication-policy', 'best-effort', expected=3 if provider == 'ftp' else 5)
            assert error['category'] == ('unsupported' if provider == 'ftp' else 'conflict'), error
            invoke(connection, flags, 'put', '--key', empty_key, '--input', str(empty), '--overwrite', 'true', '--publication-policy', policy)
            assert invoke(connection, flags, 'stat', '--key', empty_key)['size'] == 0
            if provider in ['sftp', 'ftp']:
                failure = invoke(connection, flags, 'copy', '--source-key', key + '/missing',
                    '--destination-key', key + '/new-parent/child', '--overwrite', 'true',
                    '--publication-policy', policy, expected=5)
                assert failure['remote_effect'] == 'unknown', failure
                assert failure['retry']['kind'] == 'requires_recovery', failure
                assert failure['details']['preparation'] == 'directories_may_remain', failure
            if provider != 'ftp':
                race_key = key + '/race'
                def create(path):
                    return invoke(connection, flags, 'put', '--key', race_key,
                        '--input', str(path), '--overwrite', 'false',
                        '--publication-policy', 'best-effort', expected=None)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(create, [old, empty]))
                assert sorted(code for code, _ in results) == [0, 5], results
                loser = next(value for code, value in results if code == 5)
                assert loser['error']['category'] == 'conflict', loser
                winner = next(i for i, (code, _) in enumerate(results) if code == 0)
                race_output = work / f'{provider}-race.bin'
                invoke(connection, flags, 'get', '--key', race_key,
                    '--output', str(race_output), '--overwrite', 'false')
                assert race_output.read_bytes() == [old, empty][winner].read_bytes()
                invoke(connection, flags, 'delete', '--key', race_key, '--ignore-missing', 'false')
            for object_key in [source_key, destination_key, empty_key]:
                assert invoke(connection, flags, 'delete', '--key', object_key, '--ignore-missing', 'false')['deleted']
                assert not invoke(connection, flags, 'delete', '--key', object_key, '--ignore-missing', 'true')['deleted']
            observations.append({'provider': provider, 'operations': 7, 'bytes': len(payload), 'status': 'PASS',
                                 'transport': ('http_opt_in' if '--allow-insecure-http' in flags else 'https') if provider == 's3'
                                 else ('ssh_pinned' if provider == 'sftp' else 'ftp_opt_in')})
    print(json.dumps({'binary_sha256': hashlib.sha256(BINARY.read_bytes()).hexdigest(), 'results': observations}))


if __name__ == '__main__':
    main()
