"""Qualify the installed Python wheel against all nine dedicated storage fixtures."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid
import zipfile
import plenora_storage

from plenora_storage import AsyncEngine, Connection, Engine, EngineConfig, StorageError, __version__

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    # Bind the evidence to the actual installed module and Python wrappers.
    installed = Path(plenora_storage.__file__).parent
    with zipfile.ZipFile(args.wheel) as archive:
        for member in archive.namelist():
            if member.startswith('plenora_storage/') and not member.endswith('/'):
                relative = member.removeprefix('plenora_storage/')
                assert (installed / relative).read_bytes() == archive.read(member), 'installed SDK differs from wheel'
    host = os.environ.get('PLENORA_FIXTURE_HOST')
    ca = Path(os.environ.get('PLENORA_FTPS_CA', ROOT / '.fixtures/extended/server.crt')).read_text()
    pin = os.environ.get('PLENORA_SFTP_HOST_KEY_SHA256') or (ROOT / '.fixtures/sftp-fingerprint').read_text().strip()
    policy = EngineConfig(allow_private_network=True, allow_insecure_http=True, allow_insecure_ftp=True)
    results = []
    with tempfile.TemporaryDirectory(prefix='storage-python-live-') as temporary:
        work = Path(temporary)
        local = work / 'root'
        local.mkdir()
        configs = {
            'local': {'root': str(local)},
            's3': {'endpoint': f'http://{host or "minio"}:9000', 'bucket': 'plenora-test', 'region': 'us-east-1', 'virtual_hosted_style': False},
            'sftp': {'host': host or 'sftp', 'port': 2222 if host else 22, 'root': 'upload', 'host_key_sha256': pin, 'atomic_rename': True},
            'ftp': {'host': host or 'ftp', 'port': 2121 if host else 21, 'root': '.', 'mode': 'passive'},
            'ftps': {'host': host or 'ftps', 'port': 2122 if host else 21, 'tls_ca_pem': ca},
            'azure': {'endpoint': f'http://{host or "azure"}:10000/devstoreaccount1', 'account': 'devstoreaccount1', 'container': 'plenora-test'},
            'gcs': {'endpoint': f'http://{host or "gcs"}:4443', 'bucket': 'plenora-test'},
            'smb': {'host': host or 'smb', 'port': 1445 if host else 445, 'share': 'storage'},
            'webdav': {'endpoint': f'http://{host or "webdav"}:{8088 if host else 8080}/'},
        }
        credentials = {
            's3': {'access_key_id': 'plenora-dev', 'secret_access_key': 'plenora-dev-secret'},
            'sftp': {'username': 'plenora', 'password': 'plenora-sftp-secret'},
            'ftp': {'username': 'plenora', 'password': 'plenora-ftp-secret'},
            'azure': {'account_key': 'Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=='},
            'gcs': {'bearer_token': 'fixture-token'},
        }
        for name in ['ftps', 'smb', 'webdav']:
            credentials[name] = {'username': 'plenora', 'password': 'plenora-fixture-secret'}
        def resolver(reference):
            return credentials[reference.removeprefix('fixture:')]
        payload = bytes(range(251)) * 33423 + b'python-sdk'
        source = work / 'input'
        source.write_bytes(payload)
        with Engine(policy, credential_resolver=resolver) as engine:
            for provider, config in configs.items():
                connection = Connection(provider, f'plenora-storage-{provider}-connection-v1', config,
                                        'local:process' if provider == 'local' else f'fixture:{provider}')
                prefix = f'qualification/python-{uuid.uuid4().hex}'
                key, copied = prefix + '/source', prefix + '/copy'
                publication = 'atomic_required' if provider in {'local', 's3', 'sftp', 'azure', 'gcs'} else 'best_effort'
                engine.test(connection, timeout_ms=60_000)
                try:
                    engine.put(connection, key, source, overwrite=True, publication_policy=publication, timeout_ms=60_000)
                    assert engine.stat(connection, key)['size'] == len(payload)
                    engine.copy(connection, key, copied, overwrite=True, publication_policy=publication, timeout_ms=60_000)
                    output = work / 'download'
                    transfer = engine.get(connection, copied, output, overwrite=True, timeout_ms=60_000)
                    assert output.read_bytes() == payload
                    assert transfer['checksum']['value'] == hashlib.sha256(payload).hexdigest()
                    page = engine.list(connection, prefix=prefix, max_items=1)
                    objects = list(page['objects'])
                    while page['truncated']:
                        page = engine.list(connection, prefix=prefix, max_items=1, cursor=page['next_cursor'])
                        objects.extend(page['objects'])
                    assert {item['key'] for item in objects} == {key, copied}
                    async def async_stat():
                        async with AsyncEngine(policy, credential_resolver=resolver) as asynchronous:
                            assert (await asynchronous.stat(connection, key))['size'] == len(payload)
                    asyncio.run(async_stat())
                finally:
                    for name in [key, copied]:
                        engine.delete(connection, name, ignore_missing=True, timeout_ms=60_000)
                try:
                    engine.stat(connection, copied)
                except StorageError as error:
                    assert error.category == 'not_found'
                else:
                    raise AssertionError('deleted object remains')
                results.append({'provider': provider, 'operations': 7, 'status': 'PASS', 'async_stat': 'PASS'})
                print(f'PASS installed Python SDK: {provider}', flush=True)
    report = {'version': __version__, 'wheel': args.wheel.name,
              'wheel_sha256': hashlib.sha256(args.wheel.read_bytes()).hexdigest(), 'results': results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
