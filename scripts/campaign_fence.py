"""One VM, one campaign at a time: admission lock, fencing epoch and lease.

Protocol, for a dedicated VM root (`vm_root`), with its state in
`<realpath(vm_root)>/.campaign/`, the same for every checkout of the root:

- `lock`, a flock. A controller is admitted only with the EXCLUSIVE lock,
  which flock grants only when no process holds it in any mode. Every
  activity that touches inputs, fixtures or measurements holds it SHARED for
  its whole life: the controller's admission process (as long as its SSH
  channel is open), every fixture preparation (and its children, which
  inherit the descriptor) and the VM runner container, which opens the same
  file through a read-only mount of the directory.
- `epoch`, a token `<counter>-<random>` that every admission replaces under
  the exclusive lock: the counter grows by one, with an overflow check, and
  the random part makes a token never repeat, even if the directory were
  recreated. Every activity receives the token of its admission, takes the
  shared lock and checks it before starting, before every write and at the
  end of every measured phase; the controller checks it on the VM before
  every upload and after every local qualification. A changed token is an
  explicit error and the result is not recorded. The directory is created,
  with its first token, only when it does not exist, by an atomic rename: in
  an existing directory a missing, unreadable or malformed token is an error,
  never a fresh start.
- `lease`, the end of the controller's lease on the VM's boot clock. Local
  work on the controller (the Windows qualification) runs only while the
  controller renews its lease on the admission channel; when a renewal fails
  or the lease ends, the local process tree is killed. An admission after a
  lost channel waits until the previous lease has ended. This is the only
  part that relies on clocks: see `LEASE_SECONDS`.

Admission also refuses while a runner container of this VM root exists but
has not taken the lock yet (Docker label `plenora.campaign`).
"""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time

LOCK_HELD = 75
FENCED = 77
LABEL = 'plenora.campaign'
# The controller considers its lease valid for LEASE_SECONDS after it SENT a
# renewal that the admission acknowledged; the VM records the lease as
# lasting LEASE_SECONDS + LEASE_MARGIN after it RECEIVED it, on its boot clock
# (/proc/uptime), and the next admission waits for that end. Sending precedes
# receiving, so the controller stops first as long as, over that interval,
# its monotonic clock and the VM's boot clock differ by less than the margin
# minus the time to kill the local tree (KILL_SECONDS) and one poll; and as
# long as the controller host is not suspended during a campaign, since its
# processes would resume before its next check. After a VM reboot the age of
# the last lease is unknown and the admission waits the whole duration.
#
# The supervisor checks the lease every POLL_SECONDS and never renews once
# its validity has elapsed (a stall past the end of the lease cannot be
# hidden by a later successful renewal). What it cannot prevent is a stall of
# its own while the local tree keeps running: the protocol assumes that the
# supervisor is never delayed by more than STALL_SECONDS, so that a poll,
# the stall and the kill fit in the margin together with the clock drift
# (POLL_SECONDS + STALL_SECONDS + KILL_SECONDS < LEASE_MARGIN).
LEASE_SECONDS = 60
LEASE_MARGIN = 30
RENEW_SECONDS = 10
KILL_SECONDS = 10
POLL_SECONDS = 1
STALL_SECONDS = 9
# Bounded waits on the admission channel: the first line may come after the
# wait for a previous lease; the exit status of a refused admission at once.
ADMISSION_SECONDS = 3600 + 120
EXIT_SECONDS = 30
# The longest wait an admission accepts for a recorded lease: a larger value
# is not a lease this protocol wrote.
LEASE_WAIT_LIMIT = 3600
COUNTER_LIMIT = 999999999999999999
ERROR_NO_MORE_FILES = 18
EPOCH = r'(0|[1-9][0-9]{0,17})-[0-9a-f]{32}'


class CampaignBusy(Exception):
    """Another admission, or an activity it left behind, holds the VM; nothing was touched."""


class CampaignFenced(RuntimeError):
    """The epoch changed: this activity no longer holds the VM, its result is void."""


class CampaignLost(RuntimeError):
    """The controller lost its admission or its lease while a local activity was running."""


# Shell functions shared by the VM-side checks. They inspect a path itself,
# never what a link points to: `fail` exits 1 with a message, `trusted`
# accepts a directory that belongs to root or to this user, `owned` a
# directory or regular file that belongs to this user; neither may be a link,
# be writable by group or others or carry an access control list.
OWNERSHIP_LINES = [
    'fail() { echo "$1" >&2; exit 1; }',
    'unshared() { local mode; mode=$(stat -c %a -- "$1"); '
    '(( (8#$mode & 8#022) == 0 )) || fail "$1 is writable by other users"; '
    'case "$(ls -ld -- "$1")" in ??????????+*) fail "$1 has an access control list";; esac; }',
    'trusted() { [ ! -L "$1" ] && [ -d "$1" ] || fail "$1 is a link or not a directory"; '
    'case "$(stat -c %u -- "$1")" in 0|"$(id -u)") ;; *) fail "$1 belongs to another user";; esac; unshared "$1"; }',
    'owned() { [ ! -L "$1" ] && { [ -d "$1" ] || [ -f "$1" ]; } || fail "$1 is a link or not a file or directory"; '
    '[ "$(stat -c %u -- "$1")" = "$(id -u)" ] || fail "$1 belongs to another user"; unshared "$1"; }',
]


def owned_command(paths):
    """VM command that prints `owned` only if every path is a directory or
    regular file of this user, not a link, not writable by others."""
    script = '\n'.join(['set -euo pipefail', *OWNERSHIP_LINES, 'for path in "$@"; do owned "$path"; done',
                        'echo owned'])
    return 'bash -c ' + shlex.quote(script) + ' campaign-owned ' + ' '.join(shlex.quote(path) for path in paths)


def check_owned(remote, paths):
    """Refuse campaign state on the VM that is not exclusively this user's; see `owned_command`."""
    try:
        answer = remote.run(owned_command(paths))
    except RuntimeError:
        answer = None
    if answer != 'owned':
        raise ValueError('campaign state on the VM is a link, belongs to another user or is writable by others')


def admission_command(vm_root, lease=LEASE_SECONDS + LEASE_MARGIN):
    """The VM side of an admission.

    Creates the campaign directory with its first epoch if it does not exist,
    takes the exclusive lock (LOCK_HELD on contention, flock's own code for
    any other lock error), waits for the end of the previous lease, refuses
    while a labelled runner container exists (LOCK_HELD; a failing `docker ps`
    is an error), replaces the epoch and records this admission's lease of
    `lease` seconds. Then it converts to the shared lock and checks the epoch
    again: flock converts by releasing and taking again, and if another
    admission came in between, this one exits with LOCK_HELD instead of
    announcing an epoch that is no longer its own. Last it prints
    `locked <epoch> <directory>` and renews the lease on every `renew` line,
    answering `renewed`, for as long as the channel is open.
    """
    script = '\n'.join([
        'set -euo pipefail',
        'shopt -s inherit_errexit',
        'umask 077',
        *OWNERSHIP_LINES,
        'root=$(realpath -m -- "$1")',
        'directory="$root/.campaign"',
        f'lease={int(lease)}',
        'boot() { local id; id=$(cat /proc/sys/kernel/random/boot_id); '
        '[[ "$id" =~ ^[0-9a-f-]{36}$ ]] || { echo "unreadable boot id" >&2; exit 1; }; printf %s "$id"; }',
        'boot_clock() { local value rest; read -r value rest </proc/uptime; value=${value%%.*}; '
        '[[ "$value" =~ ^[0-9]{1,12}$ ]] || { echo "unreadable boot clock" >&2; exit 1; }; printf %s "$value"; }',
        'token() { local hex; hex=$(od -An -N16 -tx1 /dev/urandom | tr -d " \\n"); '
        '[[ "$hex" =~ ^[0-9a-f]{32}$ ]] || { echo "random source failed" >&2; exit 1; }; printf %s "$1-$hex"; }',
        'write() { printf "%s\\n" "$2" >"$directory/$1.pending"; mv "$directory/$1.pending" "$directory/$1"; }',
        'strict() { local value; value=$(cat "$directory/$1"); '
        '[[ "$value" =~ $2 ]] && [ "$(wc -c <"$directory/$1")" -eq $(( ${#value} + 1 )) ] '
        '|| { echo "malformed campaign $1" >&2; exit 1; }; printf %s "$value"; }',
        f"epoch_pattern='^{EPOCH}$'",
        "lease_pattern='^(none|[0-9a-f-]{36} [0-9]{1,12})$'",
        'record_lease() { write lease "$(boot) $(( $(boot_clock) + 1 + lease ))"; }',
        # The root exists and was checked private (private_root_command).
        'owned "$root"',
        # First start only: the directory appears with its lock, first epoch
        # and an empty lease at once, or not at all.
        'if ! test -e "$directory" && ! test -L "$directory"; then',
        '  staging=$(mktemp -d "$root/.campaign.init.XXXXXXXX")',
        '  : >"$staging/lock"',
        '  printf "%s\\n" "$(token 0)" >"$staging/epoch"',
        '  printf "none\\n" >"$staging/lease"',
        '  mv -T -- "$staging" "$directory" || { rm -rf -- "$staging"; test -d "$directory"; }',
        'fi',
        # Every piece of state, checked as itself: a link anywhere stops here.
        'for path in "$directory" "$directory/lock" "$directory/epoch" "$directory/lease"; do owned "$path"; done',
        'exec 9<"$directory/lock"',
        f'flock -n -E {LOCK_HELD} -x 9',
        'previous=$(strict lease "$lease_pattern")',
        'if [ "$previous" != none ]; then',
        '  if [ "${previous%% *}" = "$(boot)" ]; then wait=$(( ${previous##* } - $(boot_clock) )); '
        'else wait=$lease; fi',
        f'  [ "$wait" -le {LEASE_WAIT_LIMIT} ] || {{ echo "recorded lease end out of range" >&2; exit 1; }}',
        '  if [ "$wait" -gt 0 ]; then echo "waiting for the end of the previous lease" >&2; sleep "$wait"; fi',
        'fi',
        f'if ! containers=$(docker ps -aq --filter "label={LABEL}=$directory" --filter status=created '
        '--filter status=running --filter status=restarting); then',
        '  echo "cannot list the runner containers" >&2; exit 1',
        'fi',
        'if [ -n "$containers" ]; then',
        f'  echo "a runner container of this VM root exists" >&2; exit {LOCK_HELD}',
        'fi',
        'current=$(strict epoch "$epoch_pattern")',
        'counter=${current%%-*}',
        f'[ "$counter" -lt {COUNTER_LIMIT} ] || {{ echo "campaign epoch counter exhausted" >&2; exit 1; }}',
        'epoch=$(token $(( counter + 1 )))',
        'write epoch "$epoch"',
        'record_lease',
        f'flock -n -E {LOCK_HELD} -s 9',
        '[ "$(strict epoch "$epoch_pattern")" = "$epoch" ] '
        f'|| {{ echo "another controller was admitted during the lock conversion" >&2; exit {LOCK_HELD}; }}',
        'echo "locked $epoch $directory"',
        'while IFS= read -r line; do',
        '  [ "$line" = renew ] || { echo "unexpected admission request" >&2; exit 1; }',
        '  record_lease',
        '  echo renewed',
        'done',
    ])
    return f'exec bash -c {shlex.quote(script)} admission {shlex.quote(vm_root)}'


def private_root_command(vm_root, top='/'):
    """The VM side of the privacy check of `vm_root`, run before the admission.

    The configured path must be canonical (absolute, no `.`, `..`, empty or
    trailing components). The check walks down from `top`, `/` for every
    campaign (tests stop it at a directory whose permissions they control):
    every directory that exists is checked, by lstat, before anything is
    looked at or created below it, and must belong to root or to this user,
    not be a link, not be writable by group or others and have no access
    control list. A level that does not exist is created with mode 700 under
    umask 077, so it is never open to others, not even for an instant; if it
    appears meanwhile, the check stops. Nothing that exists is ever changed.
    The root itself must belong to this user with mode 700. Prints `private`
    when it holds; otherwise exits 1, leaving only the private levels it
    created.
    """
    script = '\n'.join([
        'set -euo pipefail',
        'umask 077',
        'root=$1',
        'top=$2',
        *OWNERSHIP_LINES,
        '[[ "$root" == /* && "$root" != */ && "$root/" != *//* && "$root/" != */./* && "$root/" != */../* ]] '
        '|| fail "the VM root path is not canonical"',
        'if [ "$top" = / ]; then current=; rest=${root#/}; else',
        '  [[ "$root" == "$top"/* ]] || fail "the VM root is not below the top of the check"',
        '  current=$top; rest=${root#"$top"/}',
        'fi',
        'trusted "${current:-/}"',
        'IFS=/ read -r -a parts <<<"$rest"',
        'for part in "${parts[@]}"; do',
        '  current="$current/$part"',
        '  if test -e "$current" || test -L "$current"; then',
        '    trusted "$current"',
        '  else',
        '    mkdir -m 700 -- "$current" || fail "$current appeared while it was being created"',
        '  fi',
        'done',
        '[ "$(stat -c "%u %a" -- "$root")" = "$(id -u) 700" ] || fail "the VM root must belong to this user with mode 700"',
        'echo private',
    ])
    return f'bash -c {shlex.quote(script)} private {shlex.quote(vm_root)} {shlex.quote(top)}'


def check_private_root(remote, vm_root):
    """Refuse a VM root that another user could write; see `private_root_command`."""
    try:
        answer = remote.run(private_root_command(vm_root))
    except RuntimeError:
        answer = None
    if answer != 'private':
        raise ValueError('the VM root, or a directory above it, can be written by another user, belongs to '
                         'another user or is a link; nothing was created below it')


class Session:
    """The controller's admission: its channel, epoch, VM directory and lease."""

    def __init__(self, channel, reader, epoch, directory, granted, *, lease=LEASE_SECONDS, clock=time.monotonic):
        self.channel, self.reader, self.epoch, self.directory = channel, reader, epoch, directory
        self.lease, self.clock = lease, clock
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
            raise CampaignLost('the lease ended before it was renewed; local work stops')
        try:
            if self.channel.closed or self.channel.exit_status_ready():
                raise EOFError
            self.channel.sendall(b'renew\n')
            reply = self.reader.readline()
        except Exception:  # every failure of the channel is a lost admission
            reply = ''
        if reply.strip() != 'renewed':
            raise CampaignLost('the campaign admission on the VM did not renew its lease; nothing more is done')
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
            raise CampaignFenced('another controller was admitted on this VM; this campaign stops and its '
                                 'pending result is not recorded')


@contextmanager
def admission(remote, vm_root, *, clock=time.monotonic):
    """Hold the VM admission for the duration of the block; yields the Session.

    Raises CampaignBusy when the VM is held, RuntimeError for any other
    failure of the admission command.
    """
    granted = clock()
    channel, reader, first = remote.hold(admission_command(vm_root), ADMISSION_SECONDS)
    try:
        fields = first.split(' ', 2)
        if len(fields) != 3 or fields[0] != 'locked' or not re.fullmatch(EPOCH, fields[1]):
            limit = time.monotonic() + EXIT_SECONDS
            while not channel.exit_status_ready():
                if time.monotonic() >= limit:
                    raise RuntimeError('the VM admission neither started nor ended; nothing was touched')
                time.sleep(0.05)
            code = channel.recv_exit_status()
            if code == LOCK_HELD:
                raise CampaignBusy('another controller, or an activity it left on the VM, holds this VM root; '
                                   'nothing was touched')
            raise RuntimeError(f'the VM admission failed (exit code {code}); nothing was touched')
        channel.settimeout(RENEW_SECONDS)
        yield Session(channel, reader, fields[1], fields[2], granted, clock=clock)
    finally:
        channel.close()


class ProcessTree:
    """A local process and all its descendants, terminated together.

    On Windows the process starts suspended, is assigned to a job object that
    kills every process in it when its last handle closes (also when this
    controller dies), and only then runs: no descendant can start outside the
    job. On POSIX it leads a new process group; a descendant that leaves the
    group (setsid) escapes it. `terminate` kills the whole tree and verifies
    that no process of it is left.
    """

    def __init__(self, command, *, cwd=None, env=None, stdout=None):
        self.job = None
        if sys.platform == 'win32':
            self.job = _Job()
            try:
                self.process = subprocess.Popen(command, cwd=cwd, env=env, stdout=stdout, stderr=subprocess.STDOUT,
                                                creationflags=_Job.CREATE_SUSPENDED)
            except BaseException:
                self.job.close()
                raise
            try:
                self.job.adopt(self.process.pid)
            except BaseException:
                self.process.kill()
                self.process.wait(KILL_SECONDS)
                self.job.close()
                raise
        else:
            self.process = subprocess.Popen(command, cwd=cwd, env=env, stdout=stdout, stderr=subprocess.STDOUT,
                                            start_new_session=True)

    def terminate(self, deadline=KILL_SECONDS):
        """Kill every process of the tree and wait until none is left, or
        raise; every wait shares the one `deadline`."""
        limit = time.monotonic() + deadline
        if self.job is not None:
            self.job.terminate()
            remaining = self.job.active
        else:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

            def remaining():
                return _group_alive(self.process.pid)
        try:
            self.process.wait(max(0.0, limit - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise RuntimeError('the local process did not terminate; stop it before any new admission') from None
        while remaining():
            if time.monotonic() >= limit:
                raise RuntimeError('the local process tree did not terminate; stop it before any new admission')
            time.sleep(0.05)

    def close(self):
        if self.job is not None:
            self.job.close()


def _group_alive(group):
    """Whether a process of `group` still runs (zombies do not)."""
    proc = Path('/proc')
    if not proc.is_dir():
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return False
        return True
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / 'stat').read_text()
        except OSError:
            continue
        fields = stat.rsplit(')', 1)[1].split()
        if fields[0] not in ('Z', 'X') and int(fields[2]) == group:
            return True
    return False


class _Job:
    """Windows job object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, through ctypes."""

    CREATE_SUSPENDED = 0x00000004

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.wintypes = ctypes, wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateJobObjectW': (wintypes.HANDLE, [ctypes.c_void_p, wintypes.LPCWSTR]),
            'SetInformationJobObject': (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                        wintypes.DWORD]),
            'QueryInformationJobObject': (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                          wintypes.DWORD, ctypes.c_void_p]),
            'AssignProcessToJobObject': (wintypes.BOOL, [wintypes.HANDLE, wintypes.HANDLE]),
            'TerminateJobObject': (wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]),
            'OpenProcess': (wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
            'OpenThread': (wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
            'ResumeThread': (wintypes.DWORD, [wintypes.HANDLE]),
            'CloseHandle': (wintypes.BOOL, [wintypes.HANDLE]),
            'CreateToolhelp32Snapshot': (wintypes.HANDLE, [wintypes.DWORD, wintypes.DWORD]),
            'Thread32First': (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p]),
            'Thread32Next': (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p]),
        }
        for name, (result, arguments) in signatures.items():
            function = getattr(kernel, name)
            function.restype, function.argtypes = result, arguments
        self.kernel = kernel
        self.handle = self.call(kernel.CreateJobObjectW(None, None), 'CreateJobObjectW')

        class Basic(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                        ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                        ('SchedulingClass', wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', Basic), ('IoInfo', ctypes.c_uint64 * 6),
                        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

        try:
            limits = Extended()
            limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            self.call(kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)),
                      'SetInformationJobObject')
        except BaseException:
            self.close()
            raise

    def call(self, result, name):
        if not result:
            raise OSError(self.ctypes.get_last_error(), name + ' failed')
        return result

    def close_handle(self, handle, name):
        self.call(self.kernel.CloseHandle(handle), 'CloseHandle of ' + name)

    def threads(self, snapshot, entry):
        """(owner process, thread) of every thread in a snapshot; the end of
        the list is ERROR_NO_MORE_FILES, any other failure an error."""
        ctypes = self.ctypes
        more = self.kernel.Thread32First(snapshot, ctypes.byref(entry))
        while True:
            if not more:
                error = ctypes.get_last_error()
                if error != ERROR_NO_MORE_FILES:
                    raise OSError(error, 'thread enumeration failed')
                return
            yield entry.th32OwnerProcessID, entry.th32ThreadID
            more = self.kernel.Thread32Next(snapshot, ctypes.byref(entry))

    def adopt(self, pid):
        """Assign the suspended process `pid` to the job, then resume its only thread."""
        ctypes, wintypes, kernel = self.ctypes, self.wintypes, self.kernel
        process = self.call(kernel.OpenProcess(0x0101, False, pid), 'OpenProcess')  # SET_QUOTA | TERMINATE
        try:
            self.call(kernel.AssignProcessToJobObject(self.handle, process), 'AssignProcessToJobObject')
        finally:
            self.close_handle(process, 'process')

        class Entry(ctypes.Structure):
            _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ThreadID', wintypes.DWORD),
                        ('th32OwnerProcessID', wintypes.DWORD), ('tpBasePri', wintypes.LONG),
                        ('tpDeltaPri', wintypes.LONG), ('dwFlags', wintypes.DWORD)]

        snapshot = kernel.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if snapshot in (None, ctypes.c_void_p(-1).value):
            raise OSError(ctypes.get_last_error(), 'CreateToolhelp32Snapshot failed')
        resumed = 0
        try:
            entry = Entry()
            entry.dwSize = ctypes.sizeof(Entry)
            for owner, identifier in self.threads(snapshot, entry):
                if owner == pid:
                    thread = self.call(kernel.OpenThread(0x0002, False, identifier), 'OpenThread')
                    try:
                        if kernel.ResumeThread(thread) == 0xFFFFFFFF:
                            raise OSError(ctypes.get_last_error(), 'ResumeThread failed')
                    finally:
                        self.close_handle(thread, 'thread')
                    resumed += 1
        finally:
            self.close_handle(snapshot, 'snapshot')
        if resumed != 1:
            raise OSError(errno.ESRCH, 'the suspended process does not have exactly one thread')

    def terminate(self):
        self.call(self.kernel.TerminateJobObject(self.handle, 1), 'TerminateJobObject')

    def active(self):
        ctypes, wintypes = self.ctypes, self.wintypes

        class Accounting(ctypes.Structure):
            _fields_ = [('TotalUserTime', ctypes.c_int64), ('TotalKernelTime', ctypes.c_int64),
                        ('ThisPeriodTotalUserTime', ctypes.c_int64), ('ThisPeriodTotalKernelTime', ctypes.c_int64),
                        ('TotalPageFaultCount', wintypes.DWORD), ('TotalProcesses', wintypes.DWORD),
                        ('ActiveProcesses', wintypes.DWORD), ('TotalTerminatedProcesses', wintypes.DWORD)]

        accounting = Accounting()
        self.call(self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(accounting),
                                                        ctypes.sizeof(accounting), None),
                  'QueryInformationJobObject')
        return accounting.ActiveProcesses

    def close(self):
        """Close the job, which kills what is left in it; a failed close keeps
        the handle and raises, so it is never taken for closed."""
        if self.handle:
            self.close_handle(self.handle, 'job')
            self.handle = None


def supervised(command, log, session, *, cwd=None, env=None, poll=POLL_SECONDS, renew_every=RENEW_SECONDS):
    """Run a local command, and all its descendants, for as long as the admission lives.

    The lease is renewed before the start and every `renew_every` seconds,
    and checked before every renewal: once its validity has elapsed, for
    example after a stall of this supervisor, nothing is renewed. If a
    renewal fails, or the admission or its lease ends, the whole process tree
    is killed (verified) and CampaignLost is raised. When the command
    ends its leftover descendants are killed as well; a failing command
    raises RuntimeError as `logged` does.
    """
    # Nothing local runs yet, so a lease that lapsed since the last check of
    # the admission is renewed; from the start on, renewals are strict.
    session.renew()
    with open(log, 'wb') as stream:
        tree = ProcessTree(command, cwd=cwd, env=env, stdout=stream)
        # Whatever ends the loop, the whole tree is killed and verified gone
        # before anything else happens.
        try:
            renewed = time.monotonic()
            while tree.process.poll() is None:
                if not session.alive():
                    raise CampaignLost('the campaign admission or its lease ended during a local qualification; '
                                       'its process tree was killed and its result is not recorded')
                if time.monotonic() - renewed >= renew_every:
                    session.renew(strict=True)
                    renewed = time.monotonic()
                time.sleep(poll)
        finally:
            try:
                tree.terminate()
            finally:
                tree.close()
    if tree.process.returncode:
        raise RuntimeError('campaign command failed; inspect the protected attempt log')


@contextmanager
def held(directory, epoch):
    """Activity side, for the VM runner: hold the lock shared and check the
    epoch; yields a `fence()` that raises CampaignFenced once it changes.

    Contention on the lock is CampaignBusy; any other lock error is raised as
    it is, and an epoch that cannot be read is an error, not a change.
    """
    import fcntl
    directory = Path(directory)
    with open(directory / 'lock', 'rb') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise CampaignBusy('a controller is being admitted on this VM; nothing was measured') from None
            raise

        def fence():
            try:
                current = (directory / 'epoch').read_bytes()
            except OSError as error:
                raise RuntimeError('the campaign epoch cannot be read; nothing more is recorded') from error
            if current != (epoch + '\n').encode('ascii'):
                raise CampaignFenced('another controller was admitted on this VM; this run is void and its '
                                     'result is not recorded')

        try:
            fence()
            yield fence
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def fence_lines(directory_variable='CAMPAIGN_DIR', epoch_variable='CAMPAIGN_EPOCH'):
    """Shell function `fence` for preparation scripts: exits with FENCED once
    the epoch file is not exactly the epoch and one newline, and with 1 if it
    cannot be read. `cmp` compares bytes; a shell variable would lose NUL
    bytes and trailing newlines."""
    return [f'fence() {{ local status=0; printf "%s\\n" "${epoch_variable}" '
            f'| cmp -s - "${directory_variable}/epoch" || status=$?; case $status in 0) ;; '
            f'1) echo "campaign epoch changed" >&2; exit {FENCED};; '
            f'*) echo "campaign epoch unreadable" >&2; exit 1;; esac; }}']
