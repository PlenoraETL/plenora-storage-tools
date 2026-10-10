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
LEASE_SECONDS = 60
LEASE_MARGIN = 30
RENEW_SECONDS = 10
KILL_SECONDS = 10
# The longest wait an admission accepts for a recorded lease: a larger value
# is not a lease this protocol wrote.
LEASE_WAIT_LIMIT = 3600
COUNTER_LIMIT = 999999999999999999
EPOCH = r'(0|[1-9][0-9]{0,17})-[0-9a-f]{32}'


class CampaignBusy(Exception):
    """Another admission, or an activity it left behind, holds the VM; nothing was touched."""


class CampaignFenced(RuntimeError):
    """The epoch changed: this activity no longer holds the VM, its result is void."""


class CampaignLost(RuntimeError):
    """The controller lost its admission or its lease while a local activity was running."""


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
        'mkdir -p -- "$root"',
        # First start only: the directory appears with its lock, first epoch
        # and an empty lease at once, or not at all.
        'if ! test -e "$directory"; then',
        '  staging=$(mktemp -d "$root/.campaign.init.XXXXXXXX")',
        '  : >"$staging/lock"',
        '  printf "%s\\n" "$(token 0)" >"$staging/epoch"',
        '  printf "none\\n" >"$staging/lease"',
        '  mv -T -- "$staging" "$directory" || { rm -rf -- "$staging"; test -d "$directory"; }',
        'fi',
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

    def renew(self):
        """Extend the lease through the admission process, or raise CampaignLost."""
        sent = self.clock()
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
        """Fail unless this admission still holds the VM, checked on the VM."""
        self.renew()
        current = remote.run(f'cat {shlex.quote(self.directory + "/epoch")}')
        if current != self.epoch:
            raise CampaignFenced('another controller was admitted on this VM; this campaign stops and its '
                                 'pending result is not recorded')


@contextmanager
def admission(remote, vm_root, *, clock=time.monotonic):
    """Hold the VM admission for the duration of the block; yields the Session.

    Raises CampaignBusy when the VM is held, RuntimeError for any other
    failure of the admission command.
    """
    granted = clock()
    channel, reader, first = remote.hold(admission_command(vm_root))
    try:
        fields = first.split(' ', 2)
        if len(fields) != 3 or fields[0] != 'locked' or not re.fullmatch(EPOCH, fields[1]):
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
                self.process.wait()
                self.job.close()
                raise
        else:
            self.process = subprocess.Popen(command, cwd=cwd, env=env, stdout=stdout, stderr=subprocess.STDOUT,
                                            start_new_session=True)

    def terminate(self, deadline=KILL_SECONDS):
        """Kill every process of the tree and wait until none is left, or raise."""
        if self.job is not None:
            self.job.terminate()
            self.process.wait()
            remaining = self.job.active
        else:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait()

            def remaining():
                return _group_alive(self.process.pid)
        limit = time.monotonic() + deadline
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

    def adopt(self, pid):
        """Assign the suspended process `pid` to the job, then resume its only thread."""
        ctypes, wintypes, kernel = self.ctypes, self.wintypes, self.kernel
        process = self.call(kernel.OpenProcess(0x0101, False, pid), 'OpenProcess')  # SET_QUOTA | TERMINATE
        try:
            self.call(kernel.AssignProcessToJobObject(self.handle, process), 'AssignProcessToJobObject')
        finally:
            kernel.CloseHandle(process)

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
            more = kernel.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == pid:
                    thread = self.call(kernel.OpenThread(0x0002, False, entry.th32ThreadID), 'OpenThread')
                    try:
                        if kernel.ResumeThread(thread) == 0xFFFFFFFF:
                            raise OSError(ctypes.get_last_error(), 'ResumeThread failed')
                    finally:
                        kernel.CloseHandle(thread)
                    resumed += 1
                more = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            kernel.CloseHandle(snapshot)
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
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def supervised(command, log, session, *, cwd=None, env=None, poll=1.0, renew_every=RENEW_SECONDS):
    """Run a local command, and all its descendants, for as long as the admission lives.

    The lease is renewed before the start and every `renew_every` seconds. If
    a renewal fails, or the admission or its lease ends, the whole process
    tree is killed (verified) and CampaignLost is raised. When the command
    ends its leftover descendants are killed as well; a failing command
    raises RuntimeError as `logged` does.
    """
    session.renew()
    with open(log, 'wb') as stream:
        tree = ProcessTree(command, cwd=cwd, env=env, stdout=stream)
        # Whatever ends the loop, the whole tree is killed and verified gone
        # before anything else happens.
        try:
            renewed = time.monotonic()
            while tree.process.poll() is None:
                if time.monotonic() - renewed >= renew_every:
                    session.renew()
                    renewed = time.monotonic()
                if not session.alive():
                    raise CampaignLost('the campaign admission or its lease ended during a local qualification; '
                                       'its process tree was killed and its result is not recorded')
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
                current = (directory / 'epoch').read_text(encoding='utf-8')
            except OSError as error:
                raise RuntimeError('the campaign epoch cannot be read; nothing more is recorded') from error
            if current != epoch + '\n':
                raise CampaignFenced('another controller was admitted on this VM; this run is void and its '
                                     'result is not recorded')

        try:
            fence()
            yield fence
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def fence_lines(directory_variable='CAMPAIGN_DIR', epoch_variable='CAMPAIGN_EPOCH'):
    """Shell function `fence` for preparation scripts: exits with FENCED once
    the epoch changed, and with 1 if it cannot be read."""
    return [f'fence() {{ local current; current=$(cat "${directory_variable}/epoch") '
            f'|| {{ echo "campaign epoch unreadable" >&2; exit 1; }}; '
            f'test "$current" = "${epoch_variable}" '
            f'|| {{ echo "campaign epoch changed" >&2; exit {FENCED}; }}; }}']
