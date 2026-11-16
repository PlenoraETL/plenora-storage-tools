"""Campaign policy and state: the only owner of transitions, modes and budgets.

Adapters own OS handles and transport; this module owns their decisions.
No release identity or evidence schema is changed by these transitions.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import time
import strict_json

UNSHARED_WRITE_BITS = 0o022
BASE_ACL = r'^user::[-r][-w][-x]\ngroup::[-r][-w][-x]\nother::[-r][-w][-x]$'

# Declarative file classes. ACL policy is rejection, never silent removal.
@dataclass(frozen=True)
class Permission:
    mode: int
    umask: int

PERMISSIONS = {
    'private_file': Permission(0o600, 0o077),
    'private_directory': Permission(0o700, 0o077),
    'fixture_file': Permission(0o644, 0o022),
    'fixture_directory': Permission(0o755, 0o022),
    'fixture_key': Permission(0o600, 0o022),
    'runner_directory': Permission(0o711, 0o022),
    'runner_binary': Permission(0o755, 0o022),
}

@contextmanager
def creation_profile(name):
    previous = os.umask(PERMISSIONS[name].umask)
    try:
        yield
    finally:
        os.umask(previous)

# All lifetime defaults for controller, lease and measurement adapters.
LEASE_SECONDS = 60
LEASE_MARGIN = 30
RENEW_SECONDS = 10
KILL_SECONDS = 10
POLL_SECONDS = 1
STALL_SECONDS = 9
LEASE_WAIT_LIMIT = 3600
ADMISSION_SECONDS = LEASE_WAIT_LIMIT + 120
EXIT_SECONDS = 30
CONNECT_SECONDS = 15
REMOTE_SECONDS = 60
EXPORT_SECONDS = 600
PREPARATION_POLL = 10
PREPARATION_SECONDS = 1800
TRANSFER_ROUND_TIMEOUT = 10800
TRANSFER_PHASE_TIMEOUT = 14400
COMMAND_TIMEOUT = 14400
CHILD_REAP_SECONDS = 5
COMMAND_GRACE_SECONDS = 60
TLS_SECONDS = 30
KEEPALIVE_SECONDS = 30
WORKFLOW_SECONDS = 4 * 3600
VM_SECONDS = 6 * 3600
REMOTE_POLL_SECONDS = 30
WATCHDOG_POLL = .05
WATCHDOG_JOIN = 1
PROCESS_POLL_SECONDS = .05
WORKER_SHUTDOWN_SECONDS = 5

LOCK_HELD = 75
FENCED = 77
LABEL = 'plenora.campaign'
RUNNER_ACTIVE = 76
FIXTURE_STATE = '.fixtures/campaign/fixture-state.json'
COUNTER_LIMIT = 999999999999999999
EPOCH = r'(0|[1-9][0-9]{0,17})-[0-9a-f]{32}'
ROOT_PATH = r'[A-Za-z0-9._/-]+'

# Host-side preparation code, separated from the probes of live services.
# Deployment ships the complete committed tree; tests replace the probe role.
# This is an explicit runtime boundary, not discovery from comments or imports.
PREPARATION_SOURCES = {
    'runtime': (
        'scripts/prepare-fixtures.sh', 'scripts/prepare-extended-fixtures.sh',
        'scripts/prepare-sftp-keys.sh', 'scripts/fixture_tls.py',
        'scripts/campaign_model.py', 'scripts/campaign_shell.py', 'scripts/strict_json.py',
    ),
    'probes': ('scripts/check_memory.py', 'scripts/check_fixtures.py'),
}


def preparation_sources(role=None):
    if role is not None:
        return PREPARATION_SOURCES[role]
    return tuple(path for paths in PREPARATION_SOURCES.values() for path in paths)


def require_preparation_sources(paths):
    if not set(preparation_sources()) <= set(paths):
        raise ValueError('campaign source revision lacks required preparation files')

# Declared phase identities for retries and inventory. Actual dispatch is
# independently observed by test_frozen_campaign_plan, never inferred by AST.
CAMPAIGN_PHASES = {
    'run_vm_campaign.py': ('qualify-linux', 'install-sdk',
                          'transfers-large', 'transfers-workers4', 'transfers-workers16',
                          'spooled-large', 'spooled-workers4', 'spooled-workers16', 'calibrate-resources', 'soak'),
    'release_campaign.py': ('workflows', 'download', 'assemble', 'prepare-vm',
                           'qualify-windows', 'qualify-vm', 'seal'),
}

# (machine, source, event) -> destination. Failed/running retries append a
# new attempt; no transition can rewrite a completed attempt or its evidence.
TRANSITIONS = {
    ('admission', 'NEW', 'lock'): 'EXCLUSIVE',
    ('admission', 'EXCLUSIVE', 'wait'): 'DRAINED',
    ('admission', 'DRAINED', 'inspect'): 'CLEAR',
    ('admission', 'CLEAR', 'advance'): 'EPOCH',
    ('admission', 'EPOCH', 'record'): 'LEASED',
    ('admission', 'LEASED', 'share'): 'SHARED',
    ('admission', 'SHARED', 'confirm'): 'ACTIVE',
    ('admission', 'ACTIVE', 'renew'): 'ACTIVE',
    ('admission', 'ACTIVE', 'close'): 'CLOSED',
    ('phase', 'NEW', 'start'): 'RUNNING',
    ('phase', 'RUNNING', 'success'): 'PASS',
    ('phase', 'RUNNING', 'failure'): 'FAIL',
    ('phase', 'RUNNING', 'timeout'): 'TIMEOUT',
    ('phase', 'RUNNING', 'retry'): 'RUNNING',
    ('phase', 'FAIL', 'retry'): 'RUNNING',
    ('phase', 'PASS', 'reuse'): 'PASS',
    ('session', 'NEW', 'admit'): 'ACTIVE',
    ('session', 'ACTIVE', 'renew'): 'ACTIVE',
    ('session', 'EXPIRED', 'renew'): 'ACTIVE',
    ('session', 'ACTIVE', 'expire'): 'EXPIRED',
    ('session', 'EXPIRED', 'expire'): 'EXPIRED',
    ('session', 'ACTIVE', 'lose'): 'LOST',
    ('session', 'EXPIRED', 'lose'): 'LOST',
    ('session', 'ACTIVE', 'fence'): 'FENCED',
    ('session', 'EXPIRED', 'fence'): 'FENCED',
    ('session', 'ACTIVE', 'close'): 'CLOSED',
    ('session', 'EXPIRED', 'close'): 'CLOSED',
    ('session', 'LOST', 'lose'): 'LOST',
    ('session', 'LOST', 'renew'): 'ACTIVE',
    ('session', 'FENCED', 'renew'): 'ACTIVE',
    ('session', 'LOST', 'expire'): 'EXPIRED',
    ('session', 'FENCED', 'expire'): 'EXPIRED',
    ('session', 'LOST', 'close'): 'CLOSED',
    ('session', 'FENCED', 'close'): 'CLOSED',
    ('fixture', 'NEW', 'start'): 'in-progress',
    ('fixture', 'in-progress', 'prepare'): 'prepare',
    ('fixture', 'in-progress', 'reset'): 'reset',
}

def transition(machine, state, event):
    try:
        return TRANSITIONS[machine, state, event]
    except KeyError:
        raise ValueError('invalid campaign transition') from None


def epoch_bytes(epoch):
    return (epoch + '\n').encode('ascii')


def atomic_write(path, writer, *, temporary=None, binary=False):
    """Publish one complete generation, after flush/fsync, under caller's lock.

    Retain the old destination on failure. Caller owns partial cleanup and
    redaction. This is process-crash durability, not a directory fsync claim.
    This self-contained function is also embedded in remote shell commands.
    """
    temporary = path.with_suffix('.pending') if temporary is None else temporary
    options = {} if binary else {'encoding': 'utf-8', 'newline': '\n'}
    with temporary.open('wb' if binary else 'w', **options) as stream:
        writer(stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def write_json(path, value):
    def encode(stream):
        json.dump(value, stream, indent=2)
        stream.write('\n')
    atomic_write(path, encode)

class CampaignBusy(Exception):
    """Another admission, or an activity it left behind, holds the VM; nothing was touched."""


class CampaignFenced(RuntimeError):
    """The epoch changed: this activity no longer holds the VM, its result is void."""


class CampaignLost(RuntimeError):
    """The controller lost its admission or its lease while a local activity was running."""


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024**2), b''):
            value.update(block)
    return value.hexdigest()


class Campaign:
    """Use under exclusive(); changed identities or successful evidence fail closed."""

    def __init__(self, folder, identity):
        self.folder = Path(folder).resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / 'campaign.json'
        if self.path.exists():
            self.state = strict_json.loads(self.path.read_text(encoding='utf-8'))
            if self.state['identity'] != identity:
                raise ValueError('campaign source, subjects or policy changed; use a new campaign')
        else:
            self.state = {'schema_version': 1, 'identity': identity, 'phases': {}}
            self.save()

    def save(self):
        write_json(self.path, self.state)

    def validate_retries(self, names, known):
        """A retry names a known phase whose last attempt did not pass.

        Passed evidence is immutable: rerunning a passed phase would let a
        campaign keep measuring until a result suits it. A retry of a passed or
        unknown phase is refused before anything runs, never ignored.
        """
        for name in names:
            if name not in known:
                raise ValueError(f'unknown phase in retry: {name}')
            attempts = self.state['phases'].get(name, [])
            if attempts and attempts[-1]['status'] == 'PASS':
                raise ValueError(f'retry of a passed phase is refused: {name}; passed evidence is never '
                                 'remeasured, start a new campaign to measure again')

    def phase(self, name, action, *, retry=False, reason=None):
        """Actions write evidence inside their attempt; failed evidence is never reused."""
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', name):
            raise ValueError('invalid phase name')
        attempts = self.state['phases'].setdefault(name, [])
        if attempts and attempts[-1]['status'] == 'PASS' and retry:
            raise ValueError(f'retry of a passed phase is refused: {name}')
        if attempts and attempts[-1]['status'] == 'PASS':
            transition('phase', attempts[-1]['status'], 'reuse')
            previous = attempts[-1]
            folder = self.folder / name / str(len(attempts))
            actual = self.inventory(folder)
            if actual != previous['files']:
                raise ValueError('successful phase evidence changed or disappeared')
            return folder
        if attempts and (not retry or not reason):
            raise ValueError('unfinished or failed phase requires explicit retry and reason')
        folder = self.folder / name / str(len(attempts) + 1)
        folder.mkdir(parents=True, exist_ok=False)
        attempt = {'status': transition('phase', attempts[-1]['status'] if attempts else 'NEW',
                                         'retry' if attempts else 'start'), 'started_unix': time.time(), 'retry_reason': reason if attempts else None}
        attempts.append(attempt)
        self.save()
        try:
            action(folder)
            attempt['files'] = self.inventory(folder)
            if not attempt['files']:
                raise ValueError('phase produced no evidence')
            attempt['status'] = transition('phase', attempt['status'], 'success')
        except BaseException as error:
            attempt.update(status=transition('phase', attempt['status'], 'failure'), failure_type=type(error).__name__)
            raise
        finally:
            attempt['finished_unix'] = time.time()
            self.save()
        return folder

    @staticmethod
    def inventory(folder):
        files = {}
        for path in sorted(folder.rglob('*')):
            if path.is_symlink():
                if not path.resolve().is_relative_to(folder.resolve()) or not path.exists():
                    raise ValueError('campaign evidence link escapes its attempt or is broken')
                files[path.relative_to(folder).as_posix()] = 'symlink:' + hashlib.sha256(os.readlink(path).encode()).hexdigest()
                continue
            if path.is_file():
                files[path.relative_to(folder).as_posix()] = digest(path)
        return files


class Session:
    """The controller's admission: its channel, epoch, VM directory and lease."""

    def __init__(self, channel, reader, epoch, directory, granted, *, lease=LEASE_SECONDS, clock=time.monotonic):
        self.channel, self.reader, self.epoch, self.directory = channel, reader, epoch, directory
        self.lease, self.clock = lease, clock
        self.status = transition('session', 'NEW', 'admit')
        self.lease_until = granted + lease

    def alive(self):
        """Whether local work may go on: the admission channel is open and the
        lease, by the controller's clock, has not ended."""
        return (not self.channel.closed and not self.channel.exit_status_ready()
                and self.clock() < self.lease_until)

    def renew(self, *, strict=False):
        """Extend the lease through the admission process, or raise CampaignLost.

        `strict`, while local work runs: once the validity of the last
        confirmed renewal has elapsed nothing is sent and the lease is lost,
        whatever the admission would answer now.
        """
        sent = self.clock()
        if strict and sent >= self.lease_until:
            self.status = transition('session', self.status, 'expire')
            raise CampaignLost('the lease ended before it was renewed; local work stops')
        try:
            if self.channel.closed or self.channel.exit_status_ready():
                raise EOFError
            self.channel.sendall(b'renew\n')
            reply = self.reader.readline()
        except Exception:  # every failure of the channel is a lost admission
            reply = ''
        if reply.strip() != 'renewed':
            self.status = transition('session', self.status, 'lose')
            raise CampaignLost('the campaign admission on the VM did not renew its lease; nothing more is done')
        self.status = transition('session', self.status, 'renew')
        self.lease_until = sent + self.lease

    def check(self, remote):
        """Fail unless this admission still holds the VM, checked on the VM.

        No local work runs here, so a lease that lapsed between two checks is
        renewed. The epoch is compared byte for byte: the file is framed by a
        marker on both sides, so no whitespace is lost on the way.
        """
        self.renew()
        path = shlex.quote(self.directory + '/epoch')
        current = remote.run(f'printf x && cat {path} && printf x')
        if current != 'x' + self.epoch + '\nx':
            self.status = transition('session', self.status, 'fence')
            raise CampaignFenced('another controller was admitted on this VM; this campaign stops and its '
                                 'pending result is not recorded')



def positive_seconds(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError('watchdog seconds must be finite and positive')
    return value


class Deadline:
    """An absolute budget: polling and subordinate work cannot restart it."""
    def __init__(self, seconds):
        self.until = time.monotonic() + positive_seconds(seconds)

    def expired(self):
        return time.monotonic() >= self.until


class PhaseBudget:
    def __init__(self, seconds):
        self.phase_deadline = time.monotonic() + positive_seconds(seconds)
        self.round_deadline = None
        self.round_number = None

    def round(self, number, seconds):
        self.round_number = number
        self.round_deadline = time.monotonic() + positive_seconds(seconds)
