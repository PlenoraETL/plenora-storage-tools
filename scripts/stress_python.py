"""Soak the installed SDK with persistent engines against isolated Linux fixtures."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid
import zipfile

import plenora_storage
from plenora_storage import Connection, Engine, EngineConfig, StorageError
from fixture_connections import ATOMIC, PROVIDERS, fixture
from soak_policy import SOAK_DURATION_SECONDS


def resources():
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())
    return {'rss_bytes': int(status['VmRSS'].split()[0]) * 1024,
            'threads': int(status['Threads']), 'file_descriptors': len(list(Path('/proc/self/fd').iterdir()))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--duration-seconds', type=int, default=SOAK_DURATION_SECONDS,
                        help='Soak duration (default: 2 hours for every version; provisional policy)')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--interval-seconds', type=float, default=30)
    args = parser.parse_args()
    if sys.platform != 'linux' or args.duration_seconds <= 0 or args.workers <= 0 or args.interval_seconds < 0:
        parser.error('requires Linux and positive duration/workers, with a nonnegative interval')
    installed = Path(plenora_storage.__file__).parent
    with zipfile.ZipFile(args.wheel) as archive:
        for name in archive.namelist():
            if name.startswith('plenora_storage/') and not name.endswith('/'):
                assert (installed / name.removeprefix('plenora_storage/')).read_bytes() == archive.read(name), 'installed wheel differs'
    report = {'schema_version': 1, 'status': 'RUNNING', 'wheel': args.wheel.name,
              'wheel_sha256': hashlib.sha256(args.wheel.read_bytes()).hexdigest(),
              'version': plenora_storage.version(), 'duration_seconds': args.duration_seconds,
              'started_utc': datetime.now(timezone.utc).isoformat(),
              'workers': args.workers, 'payload_bytes': 65536, 'completed_cycles': 0,
              'providers': list(PROVIDERS), 'scope': 'persistent synchronous SDK, fixture servers, Linux'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    def save():
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        temporary = args.output.with_suffix('.tmp')
        temporary.write_text(json.dumps(report, indent=2) + '\n')
        temporary.replace(args.output)

    save()
    try:
        with tempfile.TemporaryDirectory(prefix='storage-soak-') as temporary:
            root = Path(temporary)
            local = root / 'storage'
            local.mkdir()
            source = root / 'input'
            payload = bytes(range(256)) * 256
            source.write_bytes(payload)
            checksum = hashlib.sha256(payload).hexdigest()
            connections, credentials = {}, {}
            for provider in PROVIDERS:
                document, material = fixture(provider, local)
                document['credential_ref'] = 'local:process' if provider == 'local' else f'fixture:{provider}'
                connections[provider] = Connection(**document)
                credentials[f'fixture:{provider}'] = material
            prefix = 'qualification/soak-' + uuid.uuid4().hex
            policy = EngineConfig(allow_insecure_http=True, allow_insecure_ftp=True, allow_private_network=True)
            with Engine(policy, credential_resolver=lambda reference: credentials[reference]) as engine, \
                    ThreadPoolExecutor(max_workers=args.workers) as pool:
                def cycle(provider, worker):
                    connection = connections[provider]
                    key = f'{prefix}/{provider}/{worker}/source'
                    copied = f'{prefix}/{provider}/{worker}/copy'
                    output = root / f'download-{provider}-{worker}'
                    publication = 'atomic_required' if provider in ATOMIC else 'best_effort'
                    try:
                        engine.put(connection, key, source, overwrite=True, publication_policy=publication, timeout_ms=60000)
                        engine.copy(connection, key, copied, overwrite=True, publication_policy=publication, timeout_ms=60000)
                        transfer = engine.get(connection, copied, output, overwrite=True, timeout_ms=60000)
                        assert transfer['checksum']['value'] == checksum and output.read_bytes() == payload
                    finally:
                        errors = []
                        for owned_key in [key, copied]:
                            try:
                                engine.delete(connection, owned_key, ignore_missing=True, timeout_ms=60000)
                            except StorageError as error:
                                errors.append(error)
                        if errors and sys.exc_info()[0] is None:
                            raise errors[0]

                while True:
                    for provider in PROVIDERS:
                        tasks = [pool.submit(cycle, provider, worker) for worker in range(args.workers)]
                        for task in tasks:
                            task.result()
                        report.setdefault('after_provider', {})[provider] = resources()
                    current = resources()
                    if 'baseline' not in report:
                        report['baseline'] = current
                        report['peak'] = dict(current)
                    for name, value in current.items():
                        report['peak'][name] = max(report['peak'][name], value)
                    report['latest'] = current
                    # Fixed budgets after warmup; no allowance grows with cycle count.
                    assert current['rss_bytes'] <= report['baseline']['rss_bytes'] + 128 * 1024**2, 'RSS grew beyond soak budget'
                    assert current['file_descriptors'] <= report['baseline']['file_descriptors'] + 16, 'descriptor count grew beyond soak budget'
                    assert current['threads'] <= report['baseline']['threads'] + 16, 'thread count grew beyond soak budget'
                    report['completed_cycles'] += 1
                    save()
                    remaining = args.duration_seconds - (time.monotonic() - started)
                    if remaining <= 0:
                        break
                    time.sleep(min(args.interval_seconds, remaining))
            report['status'] = 'PASS'
    except BaseException as error:
        report['status'] = 'FAIL'
        report['failure_type'] = type(error).__name__
        if isinstance(error, StorageError):
            report['error'] = {'code': error.code, 'category': error.category,
                               'phase': error.phase, 'remote_effect': error.remote_effect, 'retry': error.retry}
        raise
    finally:
        save()
    print(json.dumps(report))


if __name__ == '__main__':
    main()
