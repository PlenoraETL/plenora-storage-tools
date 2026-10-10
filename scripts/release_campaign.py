"""Collect GitHub artifacts, qualify them on a dedicated VM and Windows, and seal a release.

Configuration contains locations and workflow IDs, never passwords. Authentication
uses verified SSH host keys with a private key or an interactive password prompt.
Run again with the same configuration to resume verified successful phases.
"""
import argparse
from contextlib import contextmanager
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
import uuid

from campaign_fence import (FENCED, LABEL, LOCK_HELD, CampaignBusy, CampaignFenced, admission, check_owned,
                           check_private_root, fence_lines, supervised)
from campaign_state import Campaign, digest, exclusive, logged, write_json
from versioning import parse_version, workspace_version

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {'linux': 'x86_64-unknown-linux-gnu', 'windows': 'x86_64-pc-windows-msvc'}
LOCAL_PHASES = ('workflows', 'download', 'assemble', 'prepare-vm', 'qualify-windows', 'qualify-vm', 'seal')
# The runner's own directory in the container's filesystem, which no mount
# reaches: its source, its working files and its evidence.
RUNNER_WORK = '/tmp/plenora-runner'
RUNNER_OUTPUT = RUNNER_WORK + '/output'
# Fixture files generated on the VM that the runner reads, by path under
# `.fixtures/`: the controller passes their digests with the inputs.
RUNNER_FIXTURES = ('ca.crt', 'extended/server.crt', 'sftp-fingerprint')


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

    def hold(self, command, timeout):
        """Start `command` on its own channel and return the channel, its line
        reader and the first line it prints within `timeout` seconds. The
        command keeps running while the channel is open; closing the channel,
        or losing the connection, ends it."""
        channel = self.client.get_transport().open_session()
        channel.settimeout(timeout)
        channel.exec_command(command)
        reader = channel.makefile('r')
        return channel, reader, reader.readline().strip()

    def upload(self, path, remote):
        with self.client.open_sftp() as sftp:
            sftp.put(str(path), remote)

    def write(self, remote, text):
        with self.client.open_sftp() as sftp:
            with sftp.open(remote, 'w') as stream:
                stream.write(text)

    def stream(self, command, path, timeout=600):
        """Write the standard output of `command` to the local `path`, bytes as
        they are; nothing is written on the VM.

        `path` appears only when the command succeeded and its whole output
        arrived: a failed command, a lost connection or an output cut short
        (the channel then ends without an exit status, reported as -1) leaves
        no file and raises RuntimeError.
        """
        partial = path.with_name(path.name + '.partial')
        try:
            _, out, err = self.client.exec_command(command, timeout=timeout)
            with open(partial, 'wb') as stream:
                shutil.copyfileobj(out, stream)
            err.read()
            code = out.channel.recv_exit_status()
        except Exception as error:  # every failure of the transfer is the same failure
            partial.unlink(missing_ok=True)
            raise RuntimeError('dedicated VM transfer interrupted') from error
        if code:
            partial.unlink()
            raise RuntimeError('dedicated VM command failed')
        partial.replace(path)

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


RUNNER_ACTIVE = 76


FIXTURE_STATE = '.fixtures/campaign/fixture-state.json'
def fixture_scripts(remote_root, project, host, label, nonce, *, reset, directory, epoch):
    """The fixture preparation script of one execution, and the wrapper that runs it.

    Every execution has its own `nonce`, which names its signal files: a signal
    left by an earlier execution, of this or another campaign, is never read
    as this one's. The wrapper holds the VM campaign lock (campaign_fence)
    shared, inherited by the script and its children, so no other controller
    is admitted while the preparation lives; it also holds the campaign's
    ledger lock, so the preparation never overlaps this campaign's runner.
    When a lock is held it exits with LOCK_HELD before the script touches
    anything; any other lock error keeps its own code, and a LOCK_HELD from
    the script itself, which never refuses with it, becomes a failure. The
    script checks the admission `epoch` at its start, before changing
    fixtures and before its final record, and exits with FENCED once another
    controller was admitted. The wrapper always records the real exit code
    and exits with it. Every preparation recreates every fixture container,
    so no connection of an earlier activity survives it, and regenerates
    certificates and the SFTP fingerprint. The script ends by recording its
    nonce in FIXTURE_STATE, which the runner checks before it measures
    anything.

    A `reset` also refuses (RUNNER_ACTIVE) while a runner container of this
    campaign is running, archives the fixtures' state, logs and certificates
    first (a failed collection stops it before anything is recreated), checks
    the memory for the fixtures and then that every fixture serves requests.
    """
    q = shlex.quote

    def state_lines(state):
        return [f"printf '{{\"nonce\": \"%s\", \"kind\": \"%s\"}}\\n' {q(nonce)} {state} >{FIXTURE_STATE}.pending",
                f'mv {FIXTURE_STATE}.pending {FIXTURE_STATE}']

    signal = '.fixtures/signals/' + label + '-' + nonce
    diagnostics = '.fixtures/diagnostics/' + label + '-' + nonce
    compose_all = 'docker compose -f docker-compose.yml -f compose.extended.yml'
    kind = 'reset' if reset else 'prepare'
    lines = ['#!/usr/bin/env bash', 'set -euo pipefail', 'cd ' + q(remote_root),
             f'exec >{signal}.log 2>&1',
             'export COMPOSE_PROJECT_NAME=' + q(project),
             'export PLENORA_FIXTURE_HOST=' + q(host),
             *fence_lines(), 'fence']
    if reset:
        lines += [
            # A failed listing is an error, never an empty list.
            f"containers=$(docker ps --format '{{{{.Names}}}}') "
            "|| { echo 'cannot list the running containers'; exit 1; }",
            f'if grep -q {q("^" + project + "-campaign-")} <<<"$containers"; then',
            f"  echo 'a VM runner container of this campaign is still running'; exit {RUNNER_ACTIVE}",
            'fi',
            # From here on the fixtures may change: a preparation that stops
            # anywhere below leaves this state, which no runner accepts.
            'fence',
            *state_lines('in-progress'),
            f'mkdir -p {diagnostics}',
            f'{compose_all} ps --all --format json >{diagnostics}/containers.json',
            f'{compose_all} logs --no-color --timestamps >{diagnostics}/fixtures.log 2>&1',
            'for file in .fixtures/ca.crt .fixtures/minio/public.crt .fixtures/extended/server.crt '
            f'.fixtures/sftp-fingerprint; do cp "$file" {diagnostics}/; done',
            f'tar -czf {diagnostics}.tar.gz -C {diagnostics} .',
            f'python3 scripts/check_memory.py --output {signal}-memory.json',
            'fence']
    else:
        lines += ['fence', *state_lines('in-progress')]
    lines += ['export PLENORA_FIXTURE_RECREATE=1',
              'bash scripts/prepare-fixtures.sh', 'bash scripts/prepare-extended-fixtures.sh']
    if reset:
        lines += [f'python3 scripts/check_fixtures.py --output {signal}-check.json']
    lines += ['fence', *state_lines(kind)]
    wrapper = (f'cd {q(remote_root)} && mkdir -p .fixtures/campaign .fixtures/signals || exit 1\n'
               '(\n'
               f'  exec 9<{q(directory + "/lock")} || exit 1\n'
               f'  flock -n -E {LOCK_HELD} -s 9 || exit $?\n'
               '  exec 8>>.fixtures/campaign/campaign.lock || exit 1\n'
               f'  flock -n -E {LOCK_HELD} -x 8 || exit $?\n'
               f'  CAMPAIGN_DIR={q(directory)} CAMPAIGN_EPOCH={q(epoch)} bash .fixtures/{label}-{nonce}.sh\n'
               '  code=$?\n'
               f'  if [ "$code" -eq {LOCK_HELD} ]; then exit 1; fi\n'
               '  exit "$code"\n'
               ')\n'
               'code=$?\n'
               f'printf \'%s\\n\' "$code" >{signal}.exit.pending && mv {signal}.exit.pending {signal}.exit\n'
               'exit "$code"')
    return '\n'.join(lines) + '\n', wrapper + '\n'


def run_preparation(remote, remote_root, project, host, label, folder, *, reset, session, poll=10, deadline=1800):
    """Run one fixture preparation on the VM and wait for its own exit signal.

    Returns the execution's nonce, also recorded in `folder`. Raises
    CampaignBusy when a lock is held or a runner is active, CampaignFenced when
    another controller was admitted, and an error when the preparation fails
    or times out.
    """
    session.check(remote)
    nonce = uuid.uuid4().hex
    write_json(folder / (label + '-nonce.json'), {'nonce': nonce})
    signal = '.fixtures/signals/' + label + '-' + nonce
    script, wrapper = fixture_scripts(remote_root, project, host, label, nonce, reset=reset,
                                      directory=session.directory, epoch=session.epoch)
    quoted = shlex.quote(remote_root)
    remote.run(f'cd {quoted} && umask 077 && mkdir -p .fixtures/signals .fixtures/campaign')
    check_owned(remote, [remote_root, f'{remote_root}/.fixtures', f'{remote_root}/.fixtures/signals',
                         f'{remote_root}/.fixtures/campaign'])
    remote.write(f'{remote_root}/.fixtures/{label}-{nonce}.sh', script)
    remote.write(f'{remote_root}/.fixtures/{label}-{nonce}-run.sh', wrapper)
    check_owned(remote, [f'{remote_root}/.fixtures/{label}-{nonce}.sh', f'{remote_root}/.fixtures/{label}-{nonce}-run.sh'])
    remote.run(f'cd {quoted} && '
               f'(nohup bash .fixtures/{label}-{nonce}-run.sh >/dev/null 2>&1 </dev/null & echo started)')
    limit = time.monotonic() + deadline
    while True:
        result = remote.run(f'cd {quoted} && if test -f {signal}.exit; then cat {signal}.exit; else echo running; fi')
        if result != 'running':
            break
        if time.monotonic() >= limit:
            raise TimeoutError('fixture preparation timed out')
        time.sleep(poll)
    if result == str(LOCK_HELD):
        raise CampaignBusy('a VM runner or fixture preparation holds the campaign lock; nothing was prepared')
    if result == str(FENCED):
        raise CampaignFenced('another controller was admitted on this VM; the preparation stopped')
    remote.download(f'{remote_root}/{signal}.log', folder / (label + '.log'))
    if result == str(RUNNER_ACTIVE):
        raise CampaignBusy('a VM runner container of this campaign is still running; nothing was recreated')
    if reset:
        diagnostics = '.fixtures/diagnostics/' + label + '-' + nonce + '.tar.gz'
        if remote.run(f'cd {quoted} && if test -f {diagnostics}; then echo yes; else echo no; fi') == 'yes':
            remote.download(f'{remote_root}/{diagnostics}', folder / 'pre-reset.tar.gz')
    if reset and remote.run(f'cd {quoted} && if test -f {signal}-check.json; then echo yes; else echo no; fi') == 'yes':
        remote.download(f'{remote_root}/{signal}-check.json', folder / 'fixture-check.json')
    if result != '0':
        raise ValueError('fixture preparation failed')
    return nonce


def reset_fixtures(remote, remote_root, project, host, folder, session, **timing):
    """Recreate every fixture before qualifying against them and record it only when all serve requests."""
    started = time.time()
    nonce = run_preparation(remote, remote_root, project, host, 'fixture-reset', folder, reset=True,
                            session=session, **timing)
    check = json.loads((folder / 'fixture-check.json').read_text())
    if check.get('status') != 'PASS':
        raise ValueError('recreated fixtures do not all serve requests')
    write_json(folder / 'fixture-reset.json', {'recreated': True, 'nonce': nonce, 'epoch': session.epoch,
                                               'started_unix': started,
                                               'finished_unix': time.time(), 'check': 'fixture-check.json',
                                               'diagnostics': 'pre-reset.tar.gz',
                                               'reason': 'fresh fixtures before the VM runner'})
    return nonce


def expected_inputs(files):
    """Expected digest of every file of an attempt's input directory, by relative path."""
    return {name: digest(path) for name, path in sorted(files.items())}


def runner_bootstrap(work, inputs, bundle, revision, fixtures):
    """The first lines of the runner container: its own source, verified.

    Copies the source bundle from the input directory `inputs` into `work`,
    in the container's own filesystem, checks the copy against the digest
    `bundle` and checks out `revision` from it; then copies every fixture file
    of `fixtures` (path under `.fixtures/` to digest) into the checkout and
    checks it. Any difference stops the container before the runner starts:
    from here on nothing is read from the host source tree.
    """
    q = shlex.quote
    lines = ['set -euo pipefail',
             f'work={q(work)}',
             f'inputs={q(inputs)}',
             'mkdir -- "$work"',
             'cp -- "$inputs/source.bundle" "$work/source.bundle"',
             f'printf "%s  %s\\n" {q(bundle)} "$work/source.bundle" | sha256sum -c --quiet -',
             'git init -q "$work/source"',
             'git -C "$work/source" fetch -q "$work/source.bundle" HEAD',
             'git -C "$work/source" checkout -q --detach FETCH_HEAD',
             f'test "$(git -C "$work/source" rev-parse HEAD)" = {q(revision)}']
    for name, value in sorted(fixtures.items()):
        lines += [f'install -D -m 644 -- "$inputs/fixtures/"{q(name)} "$work/source/.fixtures/"{q(name)}',
                  f'printf "%s  %s\\n" {q(value)} "$work/source/.fixtures/"{q(name)} | sha256sum -c --quiet -']
    lines.append('test -z "$(git -C "$work/source" status --porcelain)"')
    return lines


def extract_runner_output(archive, folder):
    """Extract the runner's exported output; returns its directory, which must
    hold the runner ledger."""
    with tarfile.open(archive) as stream:
        stream.extractall(folder, filter='data')
    output = folder / 'output'
    if not (output / 'campaign.json').is_file():
        raise ValueError('the VM runner stopped before creating its ledger; inspect runner.log')
    return output


def check_distribution(folder, reference):
    """Every artifact of `folder` has the digest in the manifest of `reference`,
    the distribution verified when it was assembled."""
    manifest = json.loads((reference / 'release-manifest.json').read_text())
    if any(digest(folder / row['name']) != row['sha256'] for row in manifest['artifacts']):
        raise ValueError('a distribution differs from its verified manifest; nothing is sealed')


def check_selected(result, identity, linux, expected, ledger, epoch, nonce):
    """The runner's selected evidence is this attempt's, complete, and
    measured exactly this campaign's binaries.

    `linux` is the local Linux distribution, `expected` the input digests
    given to the runner, `ledger` the runner's downloaded ledger and `epoch`
    and `nonce` those of this attempt. Every file under `selected/` must be
    listed in the report with its digest, and nothing else; the report must
    be the one whose digest the ledger recorded, of this epoch and fixture
    reset, so an earlier valid report put in its place is refused; and the
    identity and the binary digests of the paired performance reports must
    match the campaign's.
    """
    selected = result / 'selected'
    report_path = selected / 'report.json'
    if any(path.is_symlink() for path in selected.rglob('*')):
        raise ValueError('the VM evidence contains a link; nothing is sealed')
    found = {path.relative_to(selected).as_posix(): digest(path) for path in sorted(selected.rglob('*'))
             if path.is_file() and path != report_path}
    recorded = json.loads(Path(ledger).read_text()).get('selected', {})
    report = json.loads(report_path.read_text())
    if (report.get('files') != found or recorded.get('report_sha256') != digest(report_path)
            or recorded.get('epoch') != epoch or report.get('epoch') != epoch
            or report.get('fixture_nonce') != nonce):
        raise ValueError('the VM evidence is not the inventoried evidence of this attempt; nothing is sealed')
    subject = report.get('identity', {})
    manifest = json.loads((linux / 'release-manifest.json').read_text())
    artifacts = {row['name']: digest(linux / row['name']) for row in manifest['artifacts']}
    performance = result / 'selected/gates/performance'
    baseline = json.loads((performance / 'baseline.json').read_text())
    candidate = json.loads((performance / 'candidate.json').read_text())
    if (report.get('status') != 'PASS' or subject.get('source_revision') != identity['revision']
            or subject.get('baseline_binary_sha256') != identity['baseline_sha256']
            or subject.get('artifacts') != artifacts or report.get('inputs') != expected
            or baseline.get('binary_sha256') != identity['baseline_sha256']
            or candidate.get('binary_sha256') != digest(linux / 'plenora-storage')):
        raise ValueError('the VM evidence does not describe this campaign\'s binaries; nothing is sealed')


def run(config_path, output, retries, reason, connect_host=None):
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
        campaign.validate_retries(retries, LOCAL_PHASES)

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
        held = None
        try:
            def prepare(path):
                bundle = path / 'source.bundle'
                logged(['git', 'bundle', 'create', str(bundle), 'HEAD'], path, cwd=ROOT)
                session.check(remote)
                remote.run('umask 077 && mkdir -p ' + q(remote_root))
                check_owned(remote, [remote_root])
                remote.upload(bundle, remote_root + '/source.bundle')
                # A dedicated directory may only contain this campaign's checkout.
                remote.run(f'cd {q(remote_root)} && if test ! -d .git; then git init -q && git fetch -q source.bundle HEAD && git checkout -q --detach FETCH_HEAD; fi')
                remote.run(f'cd {q(remote_root)} && test "$(git rev-parse HEAD)" = {q(revision)} && test -z "$(git status --porcelain --untracked-files=no)" && umask 077 && mkdir -p .fixtures')
                check_owned(remote, [remote_root, remote_root + '/.fixtures'])
                # Move the transport bundle into ignored campaign storage after checkout.
                remote.run(f'cd {q(remote_root)} && mv source.bundle .fixtures/source.bundle')
                remote.run(f'cd {q(remote_root)} && test -z "$(git status --porcelain)"')
                run_preparation(remote, remote_root, project, config['host'], 'prepare', path, reset=False,
                                session=session)
                remote.download(remote_root + '/.fixtures/extended/server.crt', path / 'fixture-ca.crt')
                remote.download(remote_root + '/.fixtures/sftp-fingerprint', path / 'host-pin')

            # One admission covers every phase that uses the VM fixtures: their
            # preparation, the Windows qualification against them and the VM
            # attempt up to the end of its runner (campaign_fence).
            # Nothing on the VM is used unless no other user can write it.
            check_private_root(remote, config['vm_root'].rstrip('/'))
            held = admission(remote, config['vm_root'])
            session = held.__enter__()
            phase('prepare-vm', prepare)

            def windows(path):
                folder = path / 'dist' / version / TARGETS['windows']
                shutil.copytree(assembled / 'dist' / version / TARGETS['windows'], folder)
                # Fixtures recreated by this admission: no connection of an
                # earlier activity survives, and certificates are new.
                reset_fixtures(remote, remote_root, project, config['host'], path, session)
                remote.download(remote_root + '/.fixtures/extended/server.crt', path / 'fixture-ca.crt')
                remote.download(remote_root + '/.fixtures/sftp-fingerprint', path / 'host-pin')
                # The whole local process tree lives only while the admission
                # and its lease do, and the epoch is checked on the VM at the
                # end: a qualification during which another controller could
                # reach the fixtures is not recorded.
                supervised([sys.executable, str(ROOT / 'scripts/qualify_target.py'), str(folder), '--fixture-host',
                            config['host'], '--ftps-ca', str(path / 'fixture-ca.crt'), '--host-pin',
                            str(path / 'host-pin')],
                           path / 'command.log', session, cwd=ROOT, env=dict(os.environ, PLENORA_WEBDAV_PORT='8088'))
                session.check(remote)

            windows_result = phase('qualify-windows', windows)

            def vm(path):
                if connect_host:
                    import hashlib
                    host_key = remote.client.get_transport().get_remote_server_key()
                    write_json(path / 'transport.json', {'logical_host': config['host'], 'connect_host': connect_host,
                               'host_key_sha256': hashlib.sha256(host_key.asbytes()).hexdigest(), 'reason': reason})
                # Every VM attempt starts on recreated fixtures, so the
                # performance phase never measures servers that earlier work
                # left with accumulated state. The reset is recorded with the
                # attempt, and only once every fixture answered.
                nonce = reset_fixtures(remote, remote_root, project, config['host'], path, session)
                for name in RUNNER_FIXTURES:
                    remote.download(f'{remote_root}/.fixtures/{name}', path / 'fixtures' / name)
                logged(['git', 'bundle', 'create', str(path / 'source.bundle'), 'HEAD'], path, cwd=ROOT,
                       name='bundle.log')
                # Every input of this attempt lives in a directory of this
                # epoch, which no other admission writes, and the runner gets
                # the digest of every file in it: an upload of another
                # controller, finished late, can neither land in it nor pass.
                inputs = '.fixtures/inputs/' + session.epoch
                linux = assembled / 'dist' / version / TARGETS['linux']
                distribution = f'dist/{version}/{TARGETS["linux"]}'
                archive = path / 'linux-input.tar.gz'
                with tarfile.open(archive, 'w:gz') as stream:
                    stream.add(linux, arcname=distribution)
                override = {'services': {'storage-rust': {'image': config['runner_image'],
                    'volumes': [remote_root + ':/workspace', remote_root + '/.fixtures/runner-target:/workspace/target',
                                'campaign-registry:/usr/local/cargo/registry',
                                remote_root + '/' + inputs + '/baseline:/baseline:ro',
                                'minio-data:/fixture-disks/minio:ro', 'sftp-data:/fixture-disks/sftp:ro', 'ftp-data:/fixture-disks/ftp:ro',
                                session.directory + ':/campaign:ro']}},
                    'volumes': {'campaign-registry': {'external': True, 'name': config['registry_volume']}}}
                # Override the target mounts explicitly; the Compose service defines
                # the same destinations, which Compose replaces by target path.
                (path / 'compose.campaign.json').write_text(json.dumps(override), encoding='utf-8')
                if any(item.is_dir() for item in linux.iterdir()):
                    raise ValueError('the Linux distribution must be a flat directory')
                fixtures = {f'fixtures/{name}': path / 'fixtures' / name for name in RUNNER_FIXTURES}
                expected = expected_inputs({'baseline/plenora-storage': baseline,
                                            'linux-input.tar.gz': archive,
                                            'source.bundle': path / 'source.bundle', **fixtures,
                                            'compose.campaign.json': path / 'compose.campaign.json',
                                            **{f'{distribution}/{item.name}': item for item in linux.iterdir()}})
                write_json(path / 'expected-inputs.json', expected)
                remote_inputs = remote_root + '/' + inputs
                session.check(remote)
                remote.run(f'umask 077 && mkdir -p {q(remote_root + "/.fixtures/inputs")} && mkdir {q(remote_inputs)} '
                           f'{q(remote_inputs + "/baseline")} {q(remote_inputs + "/fixtures")} '
                           f'{q(remote_inputs + "/fixtures/extended")}')
                check_owned(remote, [remote_root, remote_root + '/.fixtures', remote_root + '/.fixtures/inputs',
                                     remote_inputs])
                remote.upload(baseline, remote_inputs + '/baseline/plenora-storage')
                remote.upload(path / 'source.bundle', remote_inputs + '/source.bundle')
                for name, local in fixtures.items():
                    remote.upload(local, remote_inputs + '/' + name)
                remote.upload(archive, remote_inputs + '/linux-input.tar.gz')
                remote.upload(path / 'compose.campaign.json', remote_inputs + '/compose.campaign.json')
                remote.run(f'cd {q(remote_inputs)} && umask 077 && tar -xzf linux-input.tar.gz && '
                           f'chmod +x baseline/plenora-storage {q(distribution + "/plenora-storage")}')
                # Every input is a file of this user where it was put, not a link.
                check_owned(remote, [remote_inputs + '/' + name for name in sorted(expected)])
                compose = (f'cd {q(remote_root)} && docker compose -p {q(project)} -f docker-compose.yml '
                           f'-f compose.extended.yml -f {q(inputs + "/compose.campaign.json")}')
                container = project + '-campaign-' + path.name
                mounted = '/workspace/' + inputs
                # The runner runs from its own verified checkout in the
                # container's filesystem, writes every evidence there and
                # measures fresh: a VM attempt never resumes earlier phases.
                command = ['python3', 'scripts/run_vm_campaign.py', f'{mounted}/{distribution}',
                           '--baseline-binary', '/baseline/plenora-storage', '--output', RUNNER_OUTPUT,
                           '--fixture-state', '/workspace/.fixtures/campaign',
                           '--inputs', mounted, '--expected-inputs', json.dumps(expected, sort_keys=True),
                           '--fixture-nonce', nonce, '--campaign-dir', '/campaign',
                           '--epoch', session.epoch,
                           '--backend-data', '/fixture-disks/minio', '--backend-data', '/fixture-disks/sftp', '--backend-data', '/fixture-disks/ftp']
                setup = '\n'.join([
                    *runner_bootstrap(RUNNER_WORK, mounted, expected['source.bundle'], revision,
                                      {name: expected[f'fixtures/{name}'] for name in RUNNER_FIXTURES}),
                    f'cp -- {RUNNER_WORK}/source/.fixtures/ca.crt '
                    '/usr/local/share/ca-certificates/plenora-storage-fixture.crt',
                    'update-ca-certificates',
                    f'cd {RUNNER_WORK}/source',
                    'exec ' + shlex.join(command)])
                session.check(remote)
                # The label lets an admission see this container before the
                # runner inside it has taken the campaign lock.
                remote.run(compose + ' run -d --no-deps --label ' + q(f'{LABEL}={session.directory}') + ' --name '
                           + q(container) + ' storage-rust bash -e -c ' + q(setup))
                write_json(path / 'container.json', {'name': container})
                deadline = time.monotonic() + 6 * 3600
                while time.monotonic() < deadline:
                    status = json.loads(remote.run('docker inspect --format ' + q('{{json .State}}') + ' ' + q(container)))
                    if not status['Running']:
                        break
                    time.sleep(30)
                else:
                    raise TimeoutError('VM campaign deadline exceeded')
                # Logs and evidence come straight from the container, through
                # this connection: no file on the VM is their source.
                remote.stream('docker logs ' + q(container) + ' 2>&1', path / 'runner.log')
                try:
                    remote.stream('docker cp ' + q(container + ':' + RUNNER_OUTPUT) + ' -', path / 'runner-output.tar')
                except RuntimeError:
                    raise ValueError('the VM runner stopped before creating its evidence; inspect runner.log') from None
                runner = extract_runner_output(path / 'runner-output.tar', path)
                session.check(remote)
                if status['ExitCode']:
                    raise ValueError('VM campaign failed; inspect runner.log and the exported runner ledger, then '
                                     'run a new qualify-vm attempt')
                # Last checks before the attempt can pass: the evidence is of
                # this campaign's binaries, and the admission still held the
                # VM after the export.
                check_selected(runner, identity, linux, expected, runner / 'campaign.json', session.epoch, nonce)
                session.check(remote)

            vm_result = phase('qualify-vm', vm)
            held.__exit__(None, None, None)
            held = None

            def seal(path):
                release = path / 'dist' / version
                shutil.copytree(assembled / 'dist' / version, release)
                # The sealed distributions, and the Windows one that was
                # qualified, are byte for byte the verified ones.
                for target in TARGETS.values():
                    check_distribution(release / target, assembled / 'dist' / version / target)
                check_distribution(windows_result / 'dist' / version / TARGETS['windows'],
                                   assembled / 'dist' / version / TARGETS['windows'])
                evidence = path / 'evidence'
                shutil.copytree(assembled / 'evidence', evidence)
                gates = ['performance', 'transfers', 'soak']
                if parse_version(version).requires((2, 1, 0)):
                    gates.append('transfers-spooled')
                for name in gates:
                    shutil.copytree(vm_result / 'output/selected/gates' / name, evidence / 'gates' / name)
                for source, target in [(windows_result / 'dist' / version / TARGETS['windows'], TARGETS['windows']),
                                       (vm_result / 'output/selected/linux-qualification', TARGETS['linux'])]:
                    for report in source.glob('*.json'):
                        shutil.copyfile(report, release / target / report.name)
                logged([sys.executable, str(ROOT / 'scripts/release_publication.py'), 'bundle', '--directory', str(release),
                        '--evidence', str(evidence), '--archive', str(path / 'qualification-input.tar.gz')], path, cwd=ROOT)

            sealed = phase('seal', seal)
            print('Qualified bundle:', sealed / 'qualification-input.tar.gz', flush=True)
        finally:
            if held is not None:
                held.__exit__(None, None, None)
            remote.client.close()


def entrypoint(action):
    """Run the campaign; a busy VM exits with LOCK_HELD, distinct from a failure.

    Every refusal for contention, from the admission or from a preparation,
    arrives here as CampaignBusy; a changed epoch or a lost admission is a
    failure and propagates as such."""
    try:
        action()
    except CampaignBusy as busy:
        print('campaign not started:', busy, file=sys.stderr)
        return LOCK_HELD
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--retry-phase', action='append', default=[])
    parser.add_argument('--retry-reason')
    parser.add_argument('--connect-host', help='New TCP address of the same known SSH host, only after Windows qualification passed')
    args = parser.parse_args()
    sys.exit(entrypoint(lambda: run(args.config, args.output, args.retry_phase, args.retry_reason,
                                    args.connect_host)))
