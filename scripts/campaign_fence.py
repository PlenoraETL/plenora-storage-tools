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

from campaign_model import (LOCK_HELD, FENCED, LABEL, LEASE_SECONDS, LEASE_MARGIN,
    RENEW_SECONDS, KILL_SECONDS, POLL_SECONDS, STALL_SECONDS, ADMISSION_SECONDS,
    EXIT_SECONDS, LEASE_WAIT_LIMIT, COUNTER_LIMIT, EPOCH, ROOT_PATH,
    CampaignBusy, CampaignFenced, CampaignLost, Session, epoch_bytes, transition, PROCESS_POLL_SECONDS)
from campaign_shell import (OWNERSHIP_LINES, owned_command, tree_unshared_command,
    admission_command, private_root_command, fence_lines)

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
# Bounded waits on the admission channel: the first line may come after the
# wait for a previous lease; the exit status of a refused admission at once.
# The longest wait an admission accepts for a recorded lease: a larger value
# is not a lease this protocol wrote.
ERROR_NO_MORE_FILES = 18


# Shell functions shared by the VM-side checks. They inspect a path itself,
# never what a link points to: `fail` exits 1 with a message, `trusted`
# accepts a directory that belongs to root or to this user, `owned` a
# directory or regular file that belongs to this user; neither may be a link,
# be writable by group or others or carry an access control list. Every
# command they run is checked, and every output must match a whole-line
# pattern: a failure or an unexpected answer is a refusal, never a pass.
# Access control lists come from `getfacl`, never from a listing: the path
# must have exactly the three base entries. Without `getfacl` (package
# `acl`) nothing is accepted.

# The reason of a refusal travels as a fixed code on standard error, never
# as a path or a value; the controller turns it into one of these messages.
REFUSALS = {
    'acl-missing': 'getfacl is missing on the VM: install the acl package',
    'acl-unreadable': 'the access control list of a path could not be read',
    'acl': 'a path has an access control list beyond its mode',
    'cannot-inspect': 'the owner or the mode of a path could not be read',
    'writable-by-others': 'a path is writable by group or others',
    'not-a-directory': 'a path is a link or not a directory',
    'not-a-file': 'a path is a link or not a file or directory',
    'foreign-owner': 'a path belongs to another user',
    'characters': 'the VM root path contains a character outside letters, digits, dot, underscore, dash and slash',
    'not-canonical': 'the VM root path is not canonical',
    'outside-top': 'the VM root is not below the top of the check',
    'appeared': 'a directory of the VM root path appeared while it was being created',
    'not-walked': 'the VM root path was not walked to its end',
    'root-mode': 'the VM root must belong to the campaign user with mode 700',
    'outside-tree': 'a link leaves the campaign tree',
}


class RemoteFailure(RuntimeError):
    """A VM command that exited with an error. Its standard error is kept
    apart (`stderr`), never in the message; read it only with `refusal`."""

    def __init__(self, message, stderr=''):
        super().__init__(message)
        self.stderr = stderr


def refusal(stderr):
    """The fixed message of the last refusal code in `stderr`, or None."""
    codes = re.findall(r'^campaign-refusal: ([a-z-]+)$', stderr or '', re.MULTILINE)
    if not codes:
        return None
    return REFUSALS.get(codes[-1], 'an unrecognized refusal of the VM check')


def run_check(remote, command, expected, what):
    """Run a VM check: return quietly on `expected`, raise ValueError with the
    reason the VM gave, as a fixed message, otherwise."""
    try:
        answer = remote.run(command)
    except RemoteFailure as failure:
        reason = refusal(failure.stderr) or 'the check failed without a recognized reason'
        raise ValueError(f'{what}: {reason}') from None
    if answer != expected:
        raise ValueError(f'{what}: the check gave an unexpected answer')


# Characters allowed in the VM root path, controller and VM alike: no
# control character, no space, nothing a shell or a line reader could split.


def check_tree_unshared(remote, root, prune=None):
    """Refuse a VM tree with anything writable by group or others; see `tree_unshared_command`."""
    run_check(remote, tree_unshared_command(root, prune), 'unshared', 'campaign files on the VM were refused')


def check_owned(remote, paths):
    """Refuse campaign state on the VM that is not exclusively this user's; see `owned_command`."""
    run_check(remote, owned_command(paths), 'owned', 'campaign state on the VM was refused')


def check_private_root(remote, vm_root):
    """Refuse a VM root that another user could write; see `private_root_command`."""
    run_check(remote, private_root_command(vm_root), 'private',
              'the VM root was refused, nothing was created below the refused directory')


@contextmanager
def admission(remote, vm_root, *, clock=time.monotonic):
    """Hold the VM admission for the duration of the block; yields the Session.

    Raises CampaignBusy when the VM is held, RuntimeError for any other
    failure of the admission command.
    """
    granted = clock()
    channel, reader, first = remote.hold(admission_command(vm_root), ADMISSION_SECONDS)
    session = None
    try:
        fields = first.split(' ', 2)
        if len(fields) != 3 or fields[0] != 'locked' or not re.fullmatch(EPOCH, fields[1]):
            limit = time.monotonic() + EXIT_SECONDS
            while not channel.exit_status_ready():
                if time.monotonic() >= limit:
                    raise RuntimeError('the VM admission neither started nor ended; nothing was touched')
                time.sleep(PROCESS_POLL_SECONDS)
            code = channel.recv_exit_status()
            if code == LOCK_HELD:
                raise CampaignBusy('another controller, or an activity it left on the VM, holds this VM root; '
                                   'nothing was touched')
            raise RuntimeError(f'the VM admission failed (exit code {code}); nothing was touched')
        channel.settimeout(RENEW_SECONDS)
        session = Session(channel, reader, fields[1], fields[2], granted, clock=clock)
        yield session
    finally:
        channel.close()
        if session is not None:
            session.status = transition('session', session.status, 'close')


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
            time.sleep(PROCESS_POLL_SECONDS)

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
                raise RuntimeError('the campaign epoch cannot be read (' + type(error).__name__ + '); nothing '
                                   'more is recorded') from None
            if current != epoch_bytes(epoch):
                raise CampaignFenced('another controller was admitted on this VM; this run is void and its '
                                     'result is not recorded')

        try:
            fence()
            yield fence
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
