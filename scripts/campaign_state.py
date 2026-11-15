"""Exclusive, artifact-bound phase ledger with immutable attempts and explicit retries."""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import time
from campaign_model import Campaign, digest, write_json, CHILD_REAP_SECONDS, transition
from qualification_watchdog import kill_descendants, positive_seconds


@contextmanager
def exclusive(folder):
    """OS-owned locks disappear after process termination, including a crash."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / 'campaign.lock').open('a+b') as lock:
        lock.seek(0)
        if lock.read(1) == b'':
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def logged(command, folder, *, cwd=None, env=None, name='command.log', timeout=None):
    """Keep diagnostics local; the public failure contains no subprocess arguments."""
    if timeout is not None:
        positive_seconds(timeout)
    started = time.monotonic()
    state = {'status': transition('phase', 'NEW', 'start'), 'timeout_seconds': timeout}
    status_path = folder / (name + '.status.json')
    write_json(status_path, state)
    with (folder / name).open('wb') as stream:
        process = None
        try:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdout=stream,
                                       stderr=subprocess.STDOUT, start_new_session=os.name != 'nt')
            state['pid'] = process.pid
            write_json(status_path, state)
            state['returncode'] = process.wait(timeout=timeout)
            state['status'] = transition('phase', state['status'],
                                         'success' if process.returncode == 0 else 'failure')
        except BaseException as error:
            state.update(status=transition('phase', state['status'],
                                          'timeout' if isinstance(error, subprocess.TimeoutExpired) else 'failure'),
                         failure_type=type(error).__name__)
            stream.write(b'\nFAIL campaign child watchdog or wait failure; remote effect unknown; inspect before retry\n')
            stream.flush()
            if process is not None:
                kill_descendants(process.pid)
                process.kill()
                try:
                    state['returncode'] = process.wait(timeout=CHILD_REAP_SECONDS)
                except subprocess.TimeoutExpired:
                    state['reap_timeout'] = True
            raise RuntimeError('campaign child watchdog or wait failed; inspect the protected attempt log') from None
        finally:
            state['elapsed_seconds'] = time.monotonic() - started
            write_json(status_path, state)
    if state['returncode']:
        raise RuntimeError('campaign command failed; inspect the protected attempt log')
