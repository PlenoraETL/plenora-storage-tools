"""Black-box qualification of the six additional providers; requires live fixtures."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('PLENORA_CLI_BIN', ROOT / 'target/debug' / ('plenora-storage.exe' if os.name == 'nt' else 'plenora-storage'))).resolve()
HOST = os.environ.get('PLENORA_FIXTURE_HOST')
AZURE_KEY = 'Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=='


def main():
    results = []
    with tempfile.TemporaryDirectory(prefix='plenora-extended-') as temporary:
        directory = Path(temporary)
        payload = bytes(range(251)) * 33423 + b'boundary'
        source = directory / 'source.bin'
        source.write_bytes(payload)
        destination = directory / 'download.bin'
        empty = directory / 'empty.bin'
        empty.write_bytes(b'')
        local_root = directory / 'local'
        local_root.mkdir()
        cert_path = Path(os.environ.get('PLENORA_FTPS_CA', ROOT / '.fixtures/extended/server.crt'))
        configs = {
            'local': {'root': str(local_root)},
            'azure': {'endpoint': f'http://{HOST or "azure"}:10000/devstoreaccount1', 'account': 'devstoreaccount1', 'container': 'plenora-test'},
            'gcs': {'endpoint': f'http://{HOST or "gcs"}:4443', 'bucket': 'plenora-test'},
            'smb': {'host': HOST or 'smb', 'port': 1445 if HOST else 445, 'share': 'storage'},
            'webdav': {'endpoint': f'http://{HOST or "webdav"}:{8088 if HOST else 8080}/'},
            'ftps': {'host': HOST or 'ftps', 'port': 2122 if HOST else 21, 'tls_ca_pem': cert_path.read_text()},
        }
        env = dict(os.environ, PLENORA_EXTENDED_CREDENTIALS=json.dumps({
            'username': 'plenora', 'password': 'plenora-fixture-secret',
            'account_key': AZURE_KEY, 'bearer_token': 'fixture-token',
        }))
        for provider, config in configs.items():
            if os.environ.get('PLENORA_QUALIFY_PROVIDERS') and provider not in os.environ['PLENORA_QUALIFY_PROVIDERS'].split(','):
                continue
            # Azure must exercise SharedKey authentication, not fixture bearer auth.
            credentials = ({'account_key': AZURE_KEY} if provider == 'azure' else
                           {'bearer_token': 'fixture-token'} if provider == 'gcs' else
                           {'username': 'plenora', 'password': 'plenora-fixture-secret'})
            provider_env = dict(env, PLENORA_EXTENDED_CREDENTIALS=json.dumps(credentials))
            connection = directory / f'{provider}.json'
            connection.write_text(json.dumps({'provider': provider, 'config_contract': f'plenora-storage-{provider}-connection-v1', 'config': config,
                                              'credential_ref': 'local:process' if provider == 'local' else 'env:PLENORA_EXTENDED_CREDENTIALS'}))
            prefix = f'qualification/{uuid.uuid4().hex}'
            key = prefix + '/payload'
            copy_key = prefix + '/copy'
            zero_key = prefix + '/zero'
            policy = 'atomic-required' if provider in {'local', 'azure', 'gcs'} else 'best-effort'

            def invoke(operation, *arguments, success=True, flags=()):
                command = [str(BINARY), '--format', 'json', '--allow-private-network', '--allow-insecure-http', *flags,
                           operation, '--connection', str(connection), *map(str, arguments)]
                result = subprocess.run(command, env=provider_env, capture_output=True, text=True, timeout=90)
                assert not result.stderr, (provider, operation, result.stderr)
                assert len(result.stdout.splitlines()) == 1, result.stdout
                assert not any(secret in result.stdout for secret in ['plenora-fixture-secret', AZURE_KEY, 'fixture-token']), 'credential leaked'
                document = json.loads(result.stdout)
                if success:
                    assert result.returncode == 0 and document['status'] == 'ok', (provider, operation, document)
                    return document['result']
                assert result.returncode != 0 and document['status'] == 'error', (provider, operation, document)
                return document

            try:
                invoke('test')
                if provider == 'ftps':
                    invalid_tls = dict(config)
                    invalid_tls.pop('tls_ca_pem')
                    document = json.loads(connection.read_text())
                    document['config'] = invalid_tls
                    connection.write_text(json.dumps(document))
                    invoke('test', success=False)
                    document['config'] = config
                    connection.write_text(json.dumps(document))
                if provider == 'smb':
                    provider_env['PLENORA_EXTENDED_CREDENTIALS'] = json.dumps({'username': 'plenora', 'password': 'wrong-password'})
                    invoke('test', success=False)
                    provider_env['PLENORA_EXTENDED_CREDENTIALS'] = json.dumps(credentials)
                invoke('put', '--key', key, '--input', source, '--overwrite', 'true', '--publication-policy', policy)
                assert invoke('stat', '--key', key)['size'] == len(payload)
                got = invoke('get', '--key', key, '--output', destination, '--overwrite', 'true')
                assert destination.read_bytes() == payload
                assert got['checksum']['value'] == hashlib.sha256(payload).hexdigest()
                invoke('copy', '--source-key', key, '--destination-key', copy_key, '--overwrite', 'true', '--publication-policy', policy)
                invoke('get', '--key', copy_key, '--output', destination, '--overwrite', 'true')
                assert destination.read_bytes() == payload
                invoke('put', '--key', zero_key, '--input', empty, '--overwrite', 'true', '--publication-policy', policy)
                assert invoke('stat', '--key', zero_key)['size'] == 0
                listing = invoke('list', '--prefix', prefix, '--max-items', '1', '--all')
                assert {item['key'] for item in listing['objects']} == {key, copy_key, zero_key}
                # Buffered providers must reject before mutation; FTPS uses streaming
                # and the declared source file length permits the same early check.
                invoke('put', '--key', key, '--input', source, '--overwrite', 'true', '--publication-policy', policy,
                       success=False, flags=('--max-transfer-bytes', '4'))
                invoke('get', '--key', key, '--output', destination, '--overwrite', 'true')
                assert destination.read_bytes() == payload
                invoke('put', '--key', key, '--input', empty, '--overwrite', 'false', '--publication-policy', policy, success=False)
                invoke('get', '--key', key, '--output', destination, '--overwrite', 'true')
                assert destination.read_bytes() == payload
                invoke('put', '--key', key, '--input', empty, '--overwrite', 'true', '--publication-policy', policy)
                assert invoke('stat', '--key', key)['size'] == 0
                if provider != 'ftps':
                    race_key = prefix + '/race'
                    def race(input_path):
                        command = [str(BINARY), '--format', 'json', '--allow-private-network', '--allow-insecure-http',
                                   'put', '--connection', str(connection), '--key', race_key, '--input', str(input_path),
                                   '--overwrite', 'false', '--publication-policy', policy]
                        process = subprocess.run(command, env=provider_env, capture_output=True, text=True, timeout=90)
                        assert not process.stderr, process.stderr
                        return process.returncode, json.loads(process.stdout)
                    with concurrent.futures.ThreadPoolExecutor(2) as pool:
                        outcomes = list(pool.map(race, [source, empty]))
                    assert sorted(code for code, _ in outcomes) == [0, 5], (provider, 'race', outcomes)
                    loser = next(document for code, document in outcomes if code != 0)
                    assert loser['error']['category'] == 'conflict', loser
                    winner = next(index for index, (code, _) in enumerate(outcomes) if code == 0)
                    invoke('get', '--key', race_key, '--output', destination, '--overwrite', 'true')
                    assert destination.read_bytes() == [payload, b''][winner]
                    invoke('delete', '--key', race_key, '--ignore-missing', 'false')
                for item in (key, copy_key, zero_key):
                    invoke('delete', '--key', item, '--ignore-missing', 'false')
                assert not invoke('delete', '--key', key, '--ignore-missing', 'true')['deleted']
                results.append({'provider': provider, 'status': 'PASS', 'operations': 7, 'bytes': len(payload)})
            except Exception as error:
                traceback.print_exc()
                results.append({'provider': provider, 'status': 'FAIL', 'error': str(error)})
            print(json.dumps(results[-1]), flush=True)
        report = {'binary_sha256': hashlib.sha256(BINARY.read_bytes()).hexdigest(), 'results': results,
                  'scope': 'local filesystem and isolated protocol fixtures/emulators; no live cloud qualification'}
        output = ROOT / 'target/release-readiness/extended-qualification.json'
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + '\n')
        assert results and all(result['status'] == 'PASS' for result in results), 'extended provider qualification failed'


if __name__ == '__main__':
    main()
