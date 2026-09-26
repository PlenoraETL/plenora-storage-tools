"""Public test identities for the isolated storage fixtures, never production."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = ('local', 's3', 'sftp', 'ftp', 'ftps', 'azure', 'gcs', 'smb', 'webdav')
BUFFERED = frozenset({'local', 'azure', 'gcs', 'smb', 'webdav'})
ATOMIC = frozenset({'local', 's3', 'sftp', 'azure', 'gcs'})


def fixture(provider, local_root):
    host = os.environ.get('PLENORA_FIXTURE_HOST')
    configurations = {
        'local': {'root': str(local_root)},
        's3': {'endpoint': f'http://{host or "minio"}:9000', 'bucket': 'plenora-test',
               'region': 'us-east-1', 'virtual_hosted_style': False},
        'sftp': {'host': host or 'sftp', 'port': 2222 if host else 22, 'root': 'upload', 'atomic_rename': True},
        'ftp': {'host': host or 'ftp', 'port': 2121 if host else 21, 'root': '.', 'mode': 'passive'},
        'ftps': {'host': host or 'ftps', 'port': 2122 if host else 21},
        'azure': {'endpoint': f'http://{host or "azure"}:10000/devstoreaccount1',
                  'account': 'devstoreaccount1', 'container': 'plenora-test'},
        'gcs': {'endpoint': f'http://{host or "gcs"}:4443', 'bucket': 'plenora-test'},
        'smb': {'host': host or 'smb', 'port': 1445 if host else 445, 'share': 'storage'},
        'webdav': {'endpoint': f'http://{host or "webdav"}:{8088 if host else 8080}/'},
    }
    config = configurations[provider]
    if provider == 'sftp':
        config['host_key_sha256'] = os.environ.get('PLENORA_SFTP_HOST_KEY_SHA256') or (ROOT / '.fixtures/sftp-fingerprint').read_text().strip()
    if provider == 'ftps':
        config['tls_ca_pem'] = Path(os.environ.get('PLENORA_FTPS_CA', ROOT / '.fixtures/extended/server.crt')).read_text()
    credentials = {
        'local': {},
        's3': {'access_key_id': 'plenora-dev', 'secret_access_key': 'plenora-dev-secret'},
        'sftp': {'username': 'plenora', 'password': 'plenora-sftp-secret'},
        'ftp': {'username': 'plenora', 'password': 'plenora-ftp-secret'},
        'azure': {'account_key': 'Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=='},
        'gcs': {'bearer_token': 'fixture-token'},
    }.get(provider, {'username': 'plenora', 'password': 'plenora-fixture-secret'})
    connection = {'provider': provider, 'config_contract': f'plenora-storage-{provider}-connection-v1',
                  'config': config, 'credential_ref': 'local:process' if provider == 'local' else 'env:PLENORA_TEST_CREDENTIALS'}
    return connection, credentials
