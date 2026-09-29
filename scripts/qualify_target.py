"""Qualify one built target against dedicated fixtures, including its installed wheel.

Run inside the fixture network, or specify the dedicated VM's reachable host.
This uses only the public fixture credentials, never production connections.
"""
import argparse
import json
import os
from pathlib import Path
from versioning import parse_version
import shutil
import subprocess
import sys
import tempfile
import venv

ROOT = Path(__file__).resolve().parents[1]


def qualify(folder, host=None, ca=None, pin=None):
    folder = folder.resolve()
    manifest = json.loads((folder / 'release-manifest.json').read_text(encoding='utf-8'))
    windows = sys.platform == 'win32'
    spooled = parse_version(manifest['version']).requires((2, 1, 0))
    target = 'x86_64-pc-windows-msvc' if windows else 'x86_64-unknown-linux-gnu'
    if manifest['target'] != target:
        raise ValueError('qualify the artifact on its declared target platform')
    binary = folder / ('plenora-storage.exe' if windows else 'plenora-storage')
    env = dict(os.environ, PLENORA_CLI_BIN=str(binary))
    if host:
        env.update(PLENORA_FIXTURE_HOST=host, PLENORA_QUALIFY_ALLOW_HTTP='1',
                   PLENORA_MINIO_ENDPOINT=f'http://{host}:9000', PLENORA_SFTP_ENDPOINT=host,
                   PLENORA_SFTP_PORT='2222', PLENORA_FTP_ENDPOINT=host, PLENORA_FTP_PORT='2121')
    else:
        env.setdefault('PLENORA_MINIO_TLS_ENDPOINT', 'https://minio-tls:9000')
    env['PLENORA_FTPS_CA'] = str((ca or ROOT / '.fixtures/extended/server.crt').resolve())
    env['PLENORA_SFTP_HOST_KEY_SHA256'] = (pin or ROOT / '.fixtures/sftp-fingerprint').read_text().strip()
    for provider, credentials in {
        'MINIO': {'access_key_id': 'plenora-dev', 'secret_access_key': 'plenora-dev-secret'},
        'SFTP': {'username': 'plenora', 'password': 'plenora-sftp-secret'},
        'FTP': {'username': 'plenora', 'password': 'plenora-ftp-secret'},
    }.items():
        env[f'PLENORA_{provider}_CREDENTIALS'] = json.dumps(credentials)

    def run(name, output=None):
        command = [sys.executable, str(ROOT / 'scripts' / (name + '.py'))]
        if output:
            with (folder / output).open('wb') as stream:
                subprocess.run(command, cwd=ROOT, env=env, stdout=stream, check=True)
        else:
            subprocess.run(command, cwd=ROOT, env=env, check=True)

    subprocess.run([sys.executable, str(ROOT / 'scripts/check_webdav_fixture.py'),
                    '--output', str(folder / 'webdav-fixture.json')], cwd=ROOT, env=env, check=True)
    run('qualify_cli', 'qualification.json')
    for name, report, destination in [
        ('qualify_extended', 'extended-qualification.json', 'extended-qualification.json'),
        ('qualify_extended_faults', 'extended-regressions.json', 'extended-regressions.json'),
        ('audit_release_readiness', 'results.json', 'cli-regressions.json'),
    ]:
        run(name)
        shutil.copyfile(ROOT / 'target/release-readiness' / report, folder / destination)
    if spooled:
        for script, name in [('qualify_extended', 'spooled-qualification'), ('qualify_extended_faults', 'spooled-regressions')]:
            subprocess.run([sys.executable, str(ROOT / 'scripts' / (script + '.py')), '--spool-uploads',
                            '--output', str(folder / (name + '.json'))], cwd=ROOT, env=env, check=True)
    if not windows:
        run('qualify_commit_faults', 'commit-faults.json')
        run('qualify_local_faults')
        shutil.copyfile(ROOT / 'target/release-readiness/local-faults.json', folder / 'local-faults.json')
        if spooled:
            subprocess.run([sys.executable, str(ROOT / 'scripts/qualify_local_faults.py'), '--spool-uploads',
                            '--output', str(folder / 'spooled-local-faults.json')], cwd=ROOT, env=env, check=True)
    wheels = [folder / p['name'] for p in manifest['artifacts'] if p['name'].endswith('.whl')]
    if len(wheels) != 1:
        raise ValueError('expected one manifested wheel for the target')
    with tempfile.TemporaryDirectory(prefix='storage-installed-sdk-') as temporary:
        environment = Path(temporary)
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / ('Scripts/python.exe' if windows else 'bin/python')
        subprocess.run([str(python), '-m', 'pip', 'install', '--no-index', str(wheels[0])], check=True)
        subprocess.run([str(python), str(ROOT / 'scripts/qualify_python.py'),
                        '--wheel', str(wheels[0]), '--output', str(folder / 'python-qualification.json')],
                       cwd=environment, env=env, check=True)
        if spooled:
            subprocess.run([str(python), str(ROOT / 'scripts/qualify_python.py'), '--spool-uploads',
                            '--wheel', str(wheels[0]), '--output', str(folder / 'spooled-python-qualification.json')],
                           cwd=environment, env=env, check=True)
    subprocess.run([sys.executable, str(ROOT / 'scripts/verify_release.py'), str(folder.parent)], check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--fixture-host')
    parser.add_argument('--ftps-ca', type=Path)
    parser.add_argument('--host-pin', type=Path)
    args = parser.parse_args()
    qualify(args.directory, args.fixture_host, args.ftps_ca, args.host_pin)
