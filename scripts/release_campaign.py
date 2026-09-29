"""Collect GitHub artifacts, qualify them on a dedicated VM and Windows, and seal a release.

Configuration contains locations and workflow IDs, never passwords. Authentication
uses verified SSH host keys with a private key or an interactive password prompt.
Run again with the same configuration to resume verified successful phases.
"""
import argparse
import getpass
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import time

from campaign_state import Campaign, digest, exclusive, logged, write_json
from versioning import parse_version, workspace_version

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {'linux': 'x86_64-unknown-linux-gnu', 'windows': 'x86_64-pc-windows-msvc'}


def one(folder, name):
    matches = list(folder.rglob(name))
    if len(matches) != 1:
        raise ValueError('missing or duplicated qualification input: ' + name)
    return matches[0]


class Remote:
    def __init__(self, config, connect_host=None):
        import paramiko
        self.client = paramiko.SSHClient()
        self.client.load_system_host_keys()
        self.client.load_host_keys(str(Path(config.get('known_hosts', '~/.ssh/known_hosts')).expanduser()))
        key = config.get('ssh_key')
        password = None if key else getpass.getpass('Dedicated VM password: ')
        transport = socket.create_connection((connect_host, config.get('port', 22)), timeout=15) if connect_host else None
        try:
            # The logical host remains the known_hosts lookup identity when a
            # reboot changes only its network address. Never trust a new key.
            self.client.connect(config['host'], port=config.get('port', 22), username=config['user'],
                                key_filename=str(Path(key).expanduser()) if key else None, password=password,
                                sock=transport, allow_agent=False, look_for_keys=False, timeout=15)
        except BaseException:
            if transport:
                transport.close()
            raise
        self.client.get_transport().set_keepalive(30)

    def run(self, command):
        _, out, err = self.client.exec_command(command, timeout=60)
        text = out.read().decode()
        err.read()
        if out.channel.recv_exit_status():
            raise RuntimeError('dedicated VM command failed')
        return text.strip()

    def upload(self, path, remote):
        with self.client.open_sftp() as sftp:
            sftp.put(str(path), remote)

    def write(self, remote, text):
        with self.client.open_sftp() as sftp:
            with sftp.open(remote, 'w') as stream:
                stream.write(text)

    def download(self, remote, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.client.open_sftp() as sftp:
            sftp.get(remote, str(path))


def configuration(path):
    config = json.loads(path.read_text())
    allowed = {'repository', 'host', 'port', 'user', 'known_hosts', 'ssh_key', 'vm_root',
               'runner_image', 'registry_volume', 'candidate_run', 'ci_run', 'baseline_binary'}
    if set(config) - allowed:
        raise ValueError('unknown campaign option; passwords must not be stored in configuration')
    for key in ('candidate_run', 'ci_run'):
        if not re.fullmatch(r'[0-9]+', str(config[key])):
            raise ValueError('invalid workflow identifier')
    root = PurePosixPath(config['vm_root'])
    if not root.is_absolute() or '..' in root.parts or len(root.parts) < 4:
        raise ValueError('VM root must be an explicit dedicated absolute directory')
    return config


def validate_transport_resume(state, connect_host, reason):
    """Address changes can resume a verified VM; fresh fixture preparation needs a new configuration."""
    if not connect_host:
        return
    if not reason:
        raise ValueError('transport address change requires a recorded reason')
    phases = state.get('phases', {})
    for name in ('workflows', 'download', 'assemble', 'prepare-vm', 'qualify-windows'):
        attempts = phases.get(name, [])
        if not attempts or attempts[-1]['status'] != 'PASS':
            raise ValueError('transport address change requires completed pre-VM qualification')


def run(config_path, output, retries, reason, vm_retries, connect_host=None):
    if sys.platform != 'win32':
        raise ValueError('orchestrator qualifies the Windows distribution locally; use a Windows host')
    config = configuration(config_path)
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip():
        raise ValueError('release campaign requires clean committed source')
    version = workspace_version().native
    baseline = Path(config['baseline_binary']).resolve()
    output = output.resolve()
    identity = {'revision': revision, 'version': version, 'configuration_sha256': digest(config_path),
                'baseline_sha256': digest(baseline)}
    remote_root = config['vm_root'].rstrip('/') + '/' + version + '-' + revision[:12]
    project = 'storage-q-' + revision[:12]
    q = shlex.quote
    remote = None
    with exclusive(output):
        campaign = Campaign(output, identity)
        validate_transport_resume(campaign.state, connect_host, reason)

        def phase(name, action):
            print('Phase:', name, flush=True)
            return campaign.phase(name, action, retry=name in retries, reason=reason)

        def gh(*arguments):
            return json.loads(subprocess.check_output(['gh', *arguments, '--repo', config['repository']], cwd=ROOT, text=True))

        def workflows(path):
            for key in ('candidate_run', 'ci_run'):
                deadline = time.monotonic() + 4 * 3600
                while time.monotonic() < deadline:
                    report = gh('run', 'view', str(config[key]), '--json', 'headSha,status,conclusion')
                    if report['headSha'] != revision:
                        raise ValueError('workflow describes a different source revision')
                    write_json(path / (key + '.json'), report)
                    if report['status'] == 'completed':
                        if report['conclusion'] != 'success':
                            raise ValueError('required workflow did not pass')
                        break
                    time.sleep(30)
                else:
                    raise TimeoutError('workflow deadline exceeded')

        phase('workflows', workflows)

        def download(path):
            logged(['gh', 'run', 'download', str(config['candidate_run']), '--repo', config['repository'],
                    '--dir', str(path / 'candidate')], path, cwd=ROOT, name='candidate.log')
            logged(['gh', 'run', 'download', str(config['ci_run']), '--repo', config['repository'],
                    '--name', 'production-coverage', '--dir', str(path / 'coverage')], path, cwd=ROOT, name='coverage.log')

        downloaded = phase('download', download)

        def assemble(path):
            artifacts = downloaded / 'candidate'
            evidence = path / 'evidence'
            evidence.mkdir()
            for system, target in TARGETS.items():
                source = artifacts / ('storage-candidate-' + system)
                folder = one(source, 'release-manifest.json').parent
                manifest = json.loads((folder / 'release-manifest.json').read_text())
                if manifest['source_revision'] != revision or manifest['version'] != version:
                    raise ValueError('artifact source or version differs')
                shutil.copytree(folder, path / 'dist' / version / target)
                shutil.copyfile(one(source, target + '-tests.log'), evidence / (target + '-tests.log'))
                api = artifacts / ('api-compatibility-Linux' if system == 'linux' else 'api-compatibility-Windows')
                shutil.copytree(one(api, 'report.json').parent, evidence / 'gates/api' / target)
                for python in ('3.10', '3.11', '3.12', '3.13', '3.14'):
                    label = 'ubuntu-24.04' if system == 'linux' else 'windows-latest'
                    sdk = artifacts / f'release-python-{label}-{python}'
                    shutil.copytree(one(sdk, 'sdk-tests.json').parent, evidence / 'gates/sdk' / target / python)
            for name in ('audit.json', 'api-audit.json', 'fuzz-audit.json', 'deny.log', 'smb-upstream-audit.json'):
                shutil.copyfile(one(artifacts / 'release-dependency-evidence', name), evidence / name)
            for source, destination in [('parser-fuzz-evidence', 'fuzz'), ('release-native-components', 'native-components'),
                                        ('release-disk-pressure', 'disk-pressure')]:
                shutil.copytree(artifacts / source, evidence / 'gates' / destination)
            (evidence / 'gates/coverage').mkdir()
            for name in ('coverage-summary.json', 'rust-coverage.json'):
                shutil.copyfile(one(downloaded / 'coverage', name), evidence / 'gates/coverage' / name)
            logged([sys.executable, str(ROOT / 'scripts/verify_release.py'), str(path / 'dist' / version)], path, cwd=ROOT)

        assembled = phase('assemble', assemble)
        remote = Remote(config, connect_host=connect_host)
        try:
            compose = (f'cd {q(remote_root)} && docker compose -p {q(project)} -f docker-compose.yml '
                       '-f compose.extended.yml -f .fixtures/compose.campaign.json')

            def prepare(path):
                bundle = path / 'source.bundle'
                logged(['git', 'bundle', 'create', str(bundle), 'HEAD'], path, cwd=ROOT)
                remote.run('mkdir -p ' + q(remote_root))
                remote.upload(bundle, remote_root + '/source.bundle')
                # A dedicated directory may only contain this campaign's checkout.
                remote.run(f'cd {q(remote_root)} && if test ! -d .git; then git init -q && git fetch -q source.bundle HEAD && git checkout -q --detach FETCH_HEAD; fi')
                remote.run(f'cd {q(remote_root)} && test "$(git rev-parse HEAD)" = {q(revision)} && test -z "$(git status --porcelain --untracked-files=no)" && mkdir -p .fixtures')
                # Move the transport bundle into ignored campaign storage after checkout.
                remote.run(f'cd {q(remote_root)} && mv source.bundle .fixtures/source.bundle')
                remote.run(f'cd {q(remote_root)} && test -z "$(git status --porcelain)"')
                override = {'services': {'storage-rust': {'image': config['runner_image'],
                    'volumes': [remote_root + ':/workspace', remote_root + '/.fixtures/runner-target:/workspace/target',
                                'campaign-registry:/usr/local/cargo/registry',
                                remote_root + '/.fixtures/baseline:/baseline:ro',
                                'minio-data:/fixture-disks/minio:ro', 'sftp-data:/fixture-disks/sftp:ro', 'ftp-data:/fixture-disks/ftp:ro']}},
                    'volumes': {'campaign-registry': {'external': True, 'name': config['registry_volume']}}}
                # Override the target mounts explicitly; the Compose service defines
                # the same destinations, which Compose replaces by target path.
                remote.write(remote_root + '/.fixtures/compose.campaign.json', json.dumps(override))
                remote.run('mkdir -p ' + q(remote_root + '/.fixtures/baseline'))
                remote.upload(baseline, remote_root + '/.fixtures/baseline/plenora-storage')
                remote.run('chmod +x ' + q(remote_root + '/.fixtures/baseline/plenora-storage'))
                attempt = '.fixtures/prepare-' + path.name
                script = ('#!/usr/bin/env bash\nset -euo pipefail\ncd ' + q(remote_root) + '\n'
                          + f"exec >{attempt}.log 2>&1\ntrap 'printf \"%s\\n\" \"$?\" >{attempt}.exit' EXIT\n"
                          + 'export COMPOSE_PROJECT_NAME=' + q(project) + '\n'
                          + 'export PLENORA_FIXTURE_HOST=' + q(config['host']) + '\n'
                          + 'bash scripts/prepare-fixtures.sh\nbash scripts/prepare-extended-fixtures.sh\n')
                remote.write(remote_root + '/.fixtures/prepare.sh', script)
                remote.run(f'cd {q(remote_root)} && (nohup bash .fixtures/prepare.sh >/dev/null 2>&1 </dev/null & echo started)')
                deadline = time.monotonic() + 1800
                while time.monotonic() < deadline:
                    result = remote.run(f'cd {q(remote_root)} && if test -f {attempt}.exit; then cat {attempt}.exit; else echo running; fi')
                    if result != 'running':
                        remote.download(remote_root + '/' + attempt + '.log', path / 'prepare.log')
                        if result != '0':
                            raise ValueError('fixture preparation failed')
                        break
                    time.sleep(10)
                else:
                    raise TimeoutError('fixture preparation timed out')
                remote.download(remote_root + '/.fixtures/extended/server.crt', path / 'fixture-ca.crt')
                remote.download(remote_root + '/.fixtures/sftp-fingerprint', path / 'host-pin')

            prepared = phase('prepare-vm', prepare)

            def windows(path):
                folder = path / 'dist' / version / TARGETS['windows']
                shutil.copytree(assembled / 'dist' / version / TARGETS['windows'], folder)
                logged([sys.executable, str(ROOT / 'scripts/qualify_target.py'), str(folder), '--fixture-host', config['host'],
                        '--ftps-ca', str(prepared / 'fixture-ca.crt'), '--host-pin', str(prepared / 'host-pin')],
                       path, cwd=ROOT, env=dict(os.environ, PLENORA_WEBDAV_PORT='8088'))

            windows_result = phase('qualify-windows', windows)

            def vm(path):
                if connect_host:
                    import hashlib
                    host_key = remote.client.get_transport().get_remote_server_key()
                    write_json(path / 'transport.json', {'logical_host': config['host'], 'connect_host': connect_host,
                               'host_key_sha256': hashlib.sha256(host_key.asbytes()).hexdigest(), 'reason': reason})
                archive = path / 'linux-input.tar.gz'
                with tarfile.open(archive, 'w:gz') as stream:
                    stream.add(assembled / 'dist' / version / TARGETS['linux'], arcname='dist/' + version + '/' + TARGETS['linux'])
                remote.upload(archive, remote_root + '/.fixtures/linux-input.tar.gz')
                remote.run(f'cd {q(remote_root)} && tar -xzf .fixtures/linux-input.tar.gz && chmod +x dist/{q(version)}/{TARGETS["linux"]}/plenora-storage')
                container = project + '-campaign-' + path.name
                command = ['python3', 'scripts/run_vm_campaign.py', f'dist/{version}/{TARGETS["linux"]}',
                           '--baseline-binary', '/baseline/plenora-storage', '--output', '.fixtures/campaign',
                           '--backend-data', '/fixture-disks/minio', '--backend-data', '/fixture-disks/sftp', '--backend-data', '/fixture-disks/ftp']
                for name in vm_retries:
                    command.extend(['--retry-phase', name])
                if reason:
                    command.extend(['--retry-reason', reason])
                setup = ('git config --global --add safe.directory /workspace; '
                         'cp .fixtures/ca.crt /usr/local/share/ca-certificates/plenora-storage-fixture.crt; '
                         'update-ca-certificates; ' + shlex.join(command))
                remote.run(compose + ' run -d --no-deps --name ' + q(container) + ' storage-rust bash -e -c ' + q(setup))
                write_json(path / 'container.json', {'name': container})
                deadline = time.monotonic() + 6 * 3600
                while time.monotonic() < deadline:
                    status = json.loads(remote.run('docker inspect --format ' + q('{{json .State}}') + ' ' + q(container)))
                    if not status['Running']:
                        break
                    time.sleep(30)
                else:
                    raise TimeoutError('VM campaign deadline exceeded')
                remote.run('docker logs ' + q(container) + ' >' + q(remote_root + '/.fixtures/runner-' + path.name + '.log') + ' 2>&1')
                remote.download(remote_root + '/.fixtures/runner-' + path.name + '.log', path / 'runner.log')
                ledger_exists = remote.run('if test -f ' + q(remote_root + '/.fixtures/campaign/campaign.json') + '; then echo yes; else echo no; fi')
                if ledger_exists != 'yes':
                    raise ValueError('VM runner stopped before creating its ledger; inspect runner.log')
                remote.download(remote_root + '/.fixtures/campaign/campaign.json', path / 'vm-campaign.json')
                if status['ExitCode']:
                    raise ValueError('VM campaign failed; retry only the recorded failed phases')
                remote.run(f'cd {q(remote_root)} && tar -czf .fixtures/selected.tar.gz -C .fixtures/campaign selected')
                remote.download(remote_root + '/.fixtures/selected.tar.gz', path / 'selected.tar.gz')
                with tarfile.open(path / 'selected.tar.gz') as stream:
                    stream.extractall(path, filter='data')

            vm_result = phase('qualify-vm', vm)

            def seal(path):
                release = path / 'dist' / version
                shutil.copytree(assembled / 'dist' / version, release)
                evidence = path / 'evidence'
                shutil.copytree(assembled / 'evidence', evidence)
                gates = ['performance', 'transfers', 'soak']
                if parse_version(version).requires((2, 1, 0)):
                    gates.append('transfers-spooled')
                for name in gates:
                    shutil.copytree(vm_result / 'selected/gates' / name, evidence / 'gates' / name)
                for source, target in [(windows_result / 'dist' / version / TARGETS['windows'], TARGETS['windows']),
                                       (vm_result / 'selected/linux-qualification', TARGETS['linux'])]:
                    for report in source.glob('*.json'):
                        shutil.copyfile(report, release / target / report.name)
                logged([sys.executable, str(ROOT / 'scripts/release_publication.py'), 'bundle', '--directory', str(release),
                        '--evidence', str(evidence), '--archive', str(path / 'qualification-input.tar.gz')], path, cwd=ROOT)

            sealed = phase('seal', seal)
            print('Qualified bundle:', sealed / 'qualification-input.tar.gz', flush=True)
        finally:
            remote.client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--retry-phase', action='append', default=[])
    parser.add_argument('--vm-retry-phase', action='append', default=[])
    parser.add_argument('--retry-reason')
    parser.add_argument('--connect-host', help='New TCP address of the same known SSH host, only after Windows qualification passed')
    args = parser.parse_args()
    run(args.config, args.output, args.retry_phase, args.retry_reason, args.vm_retry_phase, args.connect_host)
