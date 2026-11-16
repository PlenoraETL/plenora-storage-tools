"""Calibrate the cycle oracle locally; synthetic lifecycle, not provider qualification."""
import argparse
import ctypes
import json
import hashlib
import os
from pathlib import Path
import platform
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from soak_policy import POLICY, analyze


def resources():
    if sys.platform == 'linux':
        status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())
        return dict(rss_bytes=int(status['VmRSS'].split()[0]) * 1024,
                    file_descriptors=len(list(Path('/proc/self/fd').iterdir())), threads=int(status['Threads']))
    if sys.platform != 'win32':
        raise ValueError('measurement requires Linux or Windows')
    from ctypes import wintypes
    class Memory(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [(n, ctypes.c_size_t) for n in
            ('peak', 'working', 'qpeak', 'quota', 'ppeak', 'page', 'usage', 'pusage')]
    ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    ctypes.windll.kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    ctypes.windll.kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    ctypes.windll.kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    ctypes.windll.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    memory = Memory(); memory.cb = ctypes.sizeof(memory)
    if not ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.cb):
        raise OSError('memory measurement unavailable')
    count = wintypes.DWORD()
    if not ctypes.windll.kernel32.GetProcessHandleCount(handle, ctypes.byref(count)):
        raise OSError('handle measurement unavailable')
    # Toolhelp enumerates native threads, including SDK/native runtimes.
    class Thread(ctypes.Structure):
        _fields_ = [(n, wintypes.DWORD) for n in ('size', 'usage', 'id', 'pid', 'base', 'delta', 'flags')]
    kernel = ctypes.windll.kernel32
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel.CreateToolhelp32Snapshot(4, 0)
    entry = Thread(); entry.size = ctypes.sizeof(entry); threads = 0
    try:
        ok = kernel.Thread32First(snapshot, ctypes.byref(entry))
        while ok:
            threads += entry.pid == os.getpid()
            ok = kernel.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    if not threads:
        raise OSError('thread measurement unavailable')
    return dict(rss_bytes=memory.working, file_descriptors=count.value, threads=threads)


def probe(leak=False):
    retained = []
    samples = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='cycle-calibration-') as name:
        root = Path(name)
        def operation(worker):
            path = root / str(worker)
            data = bytes(range(256)) * 256
            path.write_bytes(data)
            assert path.read_bytes() == data
            path.unlink()
        for cycle in range(POLICY['warmup_cycles'] + POLICY['cycles']):
            with ThreadPoolExecutor(4) as pool:
                list(pool.map(operation, range(4)))
            if cycle >= POLICY['warmup_cycles']:
                if leak:
                    retained.append(bytearray(b'x' * 1024**2))
                row = resources()
                row['temporary_files'] = sum(p.is_file() for p in root.iterdir())
                samples.append(row)
    try:
        result = analyze(samples)
        status = 'PASS'
    except ValueError:
        result = None
        status = 'FAIL'
    return dict(status=status, samples=samples, analysis=result, elapsed_seconds=time.monotonic()-started,
                injected_bytes_per_cycle=1024**2 if leak else 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = dict(harness_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  policy_sha256=hashlib.sha256(Path(__file__).with_name('soak_policy.py').read_bytes()).hexdigest(), policy=POLICY, platform=platform.platform(), python=sys.version,
                  scope='synthetic local allocator and lifecycle noise; no SDK/provider qualification',
                  normal=[probe() for _ in range(3)], leak_control=probe(True))
    report['status'] = 'PASS' if all(r['status']=='PASS' for r in report['normal']) and report['leak_control']['status']=='FAIL' else 'FAIL'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(report['status'])
    return 0 if report['status']=='PASS' else 1

if __name__ == '__main__':
    raise SystemExit(main())
