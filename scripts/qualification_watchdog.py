"""Fail-closed lifetime bounds for qualification (not performance thresholds)."""
from concurrent.futures import Future
import json
import math
import os
from pathlib import Path
import queue
import signal
import sys
import threading
import time
import traceback


def positive_seconds(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError('watchdog seconds must be finite and positive')
    return value


def diagnostic(where, error=None):
    # Never serialize exception messages, locals, source lines or callback data.
    message = f'FAIL qualification {where}'
    if error is not None:
        message += f': {type(error).__name__}'
        if type(error).__name__ == 'OperationFailure':
            message += ' ' + json.dumps({key: error.axes.get(key) for key in
                                        ('code', 'category', 'phase', 'remote_effect', 'retry')})
        for frame in traceback.extract_tb(error.__traceback__):
            message += f'\n  {Path(frame.filename).name}:{frame.lineno} in {frame.name}'
    os.write(2, (message + '\n').encode('utf-8', errors='replace'))


def kill_descendants(pid):
    """Linux runner: include measurement wrappers with their own sessions."""
    if not Path('/proc').is_dir():
        return
    children = {}
    for entry in Path('/proc').iterdir():
        if entry.name.isdigit():
            try:
                fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
                children.setdefault(int(fields[1]), []).append(int(entry.name))
            except (OSError, ValueError, IndexError):
                pass
    def stop(parent):
        for child in children.get(parent, []):
            stop(child)
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
    stop(pid)


class Watchdog:
    def __init__(self, seconds):
        self.phase_deadline = time.monotonic() + positive_seconds(seconds)
        self.round_deadline = None
        self.round_number = None
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.watch, daemon=True, name='qualification-watchdog')

    def __enter__(self):
        self.thread.start()
        return self

    def round(self, number, seconds):
        self.round_number = number
        self.round_deadline = time.monotonic() + positive_seconds(seconds)

    def watch(self):
        while not self.stopped.wait(.05):
            now = time.monotonic()
            reason = None
            if not threading.main_thread().is_alive():
                reason = 'main thread ended unexpectedly'
            elif now >= self.phase_deadline:
                reason = 'phase watchdog expired'
            elif self.round_deadline is not None and now >= self.round_deadline:
                reason = f'round watchdog expired (round {self.round_number})'
            if reason:
                try:
                    diagnostic(reason + '; remote effect unknown; inspect before retry')
                    kill_descendants(os.getpid())
                finally:
                    os._exit(124)

    def __exit__(self, *exc):
        self.stopped.set()
        self.thread.join(timeout=1)


class WorkerPool:
    """Reusable daemon workers, no interpreter atexit join of stuck workers.

    Failure is reported where it originates, before any main-thread cleanup.
    Waiting for results is covered by the independent round/phase watchdog.
    """
    def __init__(self, max_workers):
        self.queue = queue.Queue()
        self.threads = [threading.Thread(target=self.work, daemon=True) for _ in range(max_workers)]

    def __enter__(self):
        for thread in self.threads:
            thread.start()
        return self

    def work(self):
        while True:
            task = self.queue.get()
            if task is None:
                return
            future, function, args = task
            try:
                future.set_result(function(*args))
            except BaseException as error:
                diagnostic('worker exception', error)
                # SystemExit(0) in a worker is still a failed qualification,
                # never the successful --help exit of the script entrypoint.
                future.set_exception(RuntimeError('worker exited') if isinstance(error, SystemExit) else error)

    def submit(self, function, *args):
        future = Future()
        self.queue.put((future, function, args))
        return future

    def __exit__(self, kind, error, tb):
        if error is not None:
            diagnostic('main leaving worker pool', error)
        for _ in self.threads:
            self.queue.put(None)
        if kind is None:
            deadline = time.monotonic() + 5
            for thread in self.threads:
                thread.join(timeout=max(0, deadline - time.monotonic()))
            if any(thread.is_alive() for thread in self.threads):
                raise RuntimeError('qualification worker shutdown timed out')


def entrypoint(main):
    try:
        main()
    except BaseException as error:
        if (isinstance(error, SystemExit) and error.code in (None, 0)
                and any(arg in ('-h', '--help') for arg in sys.argv[1:])):
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(0)
        try:
            diagnostic('main exception', error)
            kill_descendants(os.getpid())
        finally:
            os._exit(1)
    # No interpreter finalization may join a native or Python worker forever.
    if any(thread is not threading.main_thread() and thread.is_alive()
           for thread in threading.enumerate()):
        try:
            diagnostic('main ended with live workers; remote effect unknown; inspect before retry')
            kill_descendants(os.getpid())
        finally:
            os._exit(1)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
