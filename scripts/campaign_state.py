"""Exclusive, artifact-bound phase ledger with immutable attempts and explicit retries."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024**2), b''):
            value.update(block)
    return value.hexdigest()


def write_json(path, value):
    temporary = path.with_suffix('.pending')
    with temporary.open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


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


class Campaign:
    """Use under exclusive(); changed identities or successful evidence fail closed."""

    def __init__(self, folder, identity):
        self.folder = Path(folder).resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / 'campaign.json'
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding='utf-8'))
            if self.state['identity'] != identity:
                raise ValueError('campaign source, subjects or policy changed; use a new campaign')
        else:
            self.state = {'schema_version': 1, 'identity': identity, 'phases': {}}
            self.save()

    def save(self):
        write_json(self.path, self.state)

    def phase(self, name, action, *, retry=False, reason=None):
        """Actions write evidence inside their attempt; failed evidence is never reused."""
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', name):
            raise ValueError('invalid phase name')
        attempts = self.state['phases'].setdefault(name, [])
        if attempts and attempts[-1]['status'] == 'PASS':
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
        attempt = {'status': 'RUNNING', 'started_unix': time.time(), 'retry_reason': reason if attempts else None}
        attempts.append(attempt)
        self.save()
        try:
            action(folder)
            attempt['files'] = self.inventory(folder)
            if not attempt['files']:
                raise ValueError('phase produced no evidence')
            attempt['status'] = 'PASS'
        except BaseException as error:
            attempt.update(status='FAIL', failure_type=type(error).__name__)
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


def logged(command, folder, *, cwd=None, env=None, name='command.log'):
    """Keep diagnostics local; the public failure contains no subprocess arguments."""
    with (folder / name).open('wb') as stream:
        result = subprocess.run(command, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError('campaign command failed; inspect the protected attempt log')
