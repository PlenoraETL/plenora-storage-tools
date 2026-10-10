"""One VM, one campaign at a time: admission lock and fencing epoch.

Protocol, for a dedicated VM root (`vm_root`):

- One lock file, `<realpath(vm_root)>/.campaign/lock`, independent of the
  revision under qualification. A controller is admitted only with the
  EXCLUSIVE lock, which flock grants only when no process holds it in any
  mode. Every activity that touches inputs, fixtures or measurements holds the
  lock SHARED for its whole life: the controller's admission process (as long
  as its SSH channel is open), every fixture preparation (and its children,
  which inherit the descriptor) and the VM runner container, which opens the
  same file through a read-only mount of the directory. A controller is
  therefore never admitted while any activity of another admission lives.
- An epoch counter, `<realpath(vm_root)>/.campaign/epoch`, incremented at
  every admission under the exclusive lock. Every activity receives the epoch
  of its admission, takes the shared lock, then checks the epoch: an activity
  that starts after another admission (a runner or a preparation launched
  late by a controller that lost the VM) finds a newer epoch and stops before
  touching anything. Activities check it again before every write and at the
  end of every measurement, and the controller checks it on the VM after the
  Windows qualification and before every upload. A changed epoch is an
  explicit error and the result is not recorded.
- Admission also refuses while a runner container of this VM root exists but
  has not taken the lock yet (Docker label `plenora.campaign`).

What is prevented: two admissions at once, and any admission while an
activity of a previous one holds the lock. What is only detected: work of a
controller that lost its admission without knowing it (SSH partition, a
runner starting in the instant its controller died); it fails on the epoch
check instead of producing a result.
"""
from contextlib import contextmanager
import errno
from pathlib import Path
import shlex
import subprocess
import time

LOCK_HELD = 75
FENCED = 77
LABEL = 'plenora.campaign'


class CampaignBusy(Exception):
    """Another admission, or an activity it left behind, holds the VM; nothing was touched."""


class CampaignFenced(RuntimeError):
    """The epoch changed: this activity no longer holds the VM, its result is void."""


class CampaignLost(RuntimeError):
    """The controller lost its admission while a local activity was running."""


def admission_command(vm_root):
    """The VM side of an admission.

    Takes the exclusive lock, refuses while a labelled runner container
    exists, increments the epoch, converts to a shared lock and prints
    `locked <epoch> <directory>`; then waits on standard input, so the lock
    lives exactly as long as the controller's channel. A refusal exits with
    LOCK_HELD without changing anything. The conversion to shared is not
    atomic in flock: if another admission takes the exclusive lock in that
    instant, this one exits with LOCK_HELD and the newer epoch wins.
    """
    script = '\n'.join([
        'set -eu',
        'directory=$(realpath -m "$1")/.campaign',
        'mkdir -p "$directory"',
        ': >>"$directory/lock"',
        'exec 9<"$directory/lock"',
        f'flock -n -x 9 || exit {LOCK_HELD}',
        f'if test -n "$(docker ps -aq --filter "label={LABEL}=$directory" --filter status=created '
        '--filter status=running --filter status=restarting)"; then',
        f'  exit {LOCK_HELD}',
        'fi',
        'epoch=$(cat "$directory/epoch" 2>/dev/null || echo 0)',
        'case "$epoch" in ""|*[!0-9]*) echo "corrupt campaign epoch" >&2; exit 1;; esac',
        'epoch=$((epoch + 1))',
        'printf "%s\\n" "$epoch" >"$directory/epoch.pending"',
        'mv "$directory/epoch.pending" "$directory/epoch"',
        f'flock -n -s 9 || exit {LOCK_HELD}',
        'echo "locked $epoch $directory"',
        'exec cat >/dev/null',
    ])
    return f'exec bash -c {shlex.quote(script)} admission {shlex.quote(vm_root)}'


class Session:
    """The controller's admission: its channel, epoch and VM directory."""

    def __init__(self, channel, epoch, directory):
        self.channel, self.epoch, self.directory = channel, epoch, directory

    def alive(self):
        """Whether the admission process on the VM still holds the lock, as far
        as the controller can see: a closed or finished channel means not."""
        return not self.channel.closed and not self.channel.exit_status_ready()

    def check(self, remote):
        """Fail unless this admission still holds the VM, checked on the VM."""
        if not self.alive():
            raise CampaignLost('the campaign admission on the VM has ended; nothing more is done')
        current = remote.run(f'cat {shlex.quote(self.directory + "/epoch")}')
        if current != str(self.epoch):
            raise CampaignFenced('another controller was admitted on this VM; this campaign stops and its '
                                 'pending result is not recorded')


@contextmanager
def admission(remote, vm_root):
    """Hold the VM admission for the duration of the block; yields the Session.

    Raises CampaignBusy when the VM is held, RuntimeError for any other
    failure of the admission command.
    """
    channel, first = remote.hold(admission_command(vm_root))
    try:
        fields = first.split()
        if len(fields) != 3 or fields[0] != 'locked' or not fields[1].isdigit():
            code = channel.recv_exit_status()
            if code == LOCK_HELD:
                raise CampaignBusy('another controller, or an activity it left on the VM, holds this VM root; '
                                   'nothing was touched')
            raise RuntimeError(f'the VM admission failed (exit code {code}); nothing was touched')
        yield Session(channel, int(fields[1]), fields[2])
    finally:
        channel.close()


def supervised(command, log, session, *, cwd=None, env=None, poll=1.0, grace=10.0):
    """Run a local command for as long as the admission lives.

    If the admission ends, the command is terminated (then killed after
    `grace` seconds) and CampaignLost is raised; a failing command raises
    RuntimeError as `logged` does.
    """
    with open(log, 'wb') as stream:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT)
        try:
            while process.poll() is None:
                if not session.alive():
                    process.terminate()
                    try:
                        process.wait(grace)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    raise CampaignLost('the campaign admission on the VM ended during a local qualification; '
                                       'it was stopped and its result is not recorded')
                time.sleep(poll)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    if process.returncode:
        raise RuntimeError('campaign command failed; inspect the protected attempt log')


@contextmanager
def held(directory, epoch):
    """Activity side, for the VM runner: hold the lock shared and check the
    epoch; yields a `fence()` that raises CampaignFenced once it changes.

    Contention on the lock is CampaignBusy; any other lock error is raised as
    it is.
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
                current = (directory / 'epoch').read_text(encoding='utf-8').strip()
            except OSError:
                current = None
            if current != str(epoch):
                raise CampaignFenced('another controller was admitted on this VM; this run is void and its '
                                     'result is not recorded')

        try:
            fence()
            yield fence
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def fence_lines(directory_variable='CAMPAIGN_DIR', epoch_variable='CAMPAIGN_EPOCH'):
    """Shell function `fence` for preparation scripts: exits with FENCED once the epoch changed."""
    return [f'fence() {{ test "$(cat "${directory_variable}/epoch" 2>/dev/null)" = "${epoch_variable}" '
            f'|| {{ echo "campaign epoch changed" >&2; exit {FENCED}; }}; }}']

