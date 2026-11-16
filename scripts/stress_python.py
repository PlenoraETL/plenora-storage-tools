"""Soak the installed SDK with persistent engines against isolated Linux fixtures."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import uuid
import zipfile

import plenora_storage
from plenora_storage import Connection, Engine, EngineConfig, StorageError, CancellationToken
from fixture_connections import ATOMIC, PROVIDERS, fixture
from soak_policy import POLICY, analyze, validate_cycles


def resources():
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())
    return {'rss_bytes': int(status['VmRSS'].split()[0]) * 1024,
            'threads': int(status['Threads']), 'file_descriptors': len(list(Path('/proc/self/fd').iterdir()))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cycles', type=int, default=POLICY['cycles'])
    parser.add_argument('--warmup-cycles', type=int, default=POLICY['warmup_cycles'])
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--both-upload-modes', action='store_true',
                        help='Exercise persistent default and private-file engines for every declared cycle')
    args = parser.parse_args()
    if sys.platform != 'linux' or args.cycles != POLICY['cycles'] or args.warmup_cycles != POLICY['warmup_cycles'] or args.workers != 4:
        parser.error('requires Linux and the declared cycle policy')
    installed = Path(plenora_storage.__file__).parent
    with zipfile.ZipFile(args.wheel) as archive:
        for name in archive.namelist():
            if name.startswith('plenora_storage/') and not name.endswith('/'):
                assert (installed / name.removeprefix('plenora_storage/')).read_bytes() == archive.read(name), 'installed wheel differs'
    report = {'schema_version': 2, 'status': 'RUNNING', 'wheel': args.wheel.name,
              'wheel_sha256': hashlib.sha256(args.wheel.read_bytes()).hexdigest(),
              'version': plenora_storage.version(), 'policy': POLICY, 'samples': [], 'cycle_matrix': [], 'lifecycle': [], 'warmup_completed': 0,
              'started_utc': datetime.now(timezone.utc).isoformat(),
              'workers': args.workers, 'payload_bytes': 65536, 'completed_cycles': 0,
              'providers': list(PROVIDERS), 'scope': 'persistent synchronous SDK, fixture servers, Linux'}
    modes = ['buffered', 'private_file'] if args.both_upload_modes else ['buffered']
    report.update(upload_modes=modes, completed_cycles_by_mode={mode: 0 for mode in modes},
                  after_mode_provider={mode: {} for mode in modes})
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
            os.environ['TMPDIR'] = str(root)
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
            def sample():
                value = resources()
                value['temporary_files'] = sum(p.is_file() for p in root.rglob('*') if p != source)
                return value

            report['before_open'] = sample()
            policy = EngineConfig(allow_insecure_http=True, allow_insecure_ftp=True, allow_private_network=True)
            with ExitStack() as engines_stack, \
                    ThreadPoolExecutor(max_workers=args.workers) as pool:
                engines = {mode: engines_stack.enter_context(Engine(
                    policy, credential_resolver=lambda reference: credentials[reference],
                    **({'spool_uploads': True} if mode == 'private_file' else {}))) for mode in modes}

                def cycle(provider, worker, mode):
                    engine = engines[mode]
                    connection = connections[provider]
                    key = f'{prefix}/{mode}/{provider}/{worker}/source'
                    copied = f'{prefix}/{mode}/{provider}/{worker}/copy'
                    output = root / f'download-{mode}-{provider}-{worker}'
                    publication = 'atomic_required' if provider in ATOMIC else 'best_effort'
                    try:
                        engine.put(connection, key, source, overwrite=True, publication_policy=publication, timeout_ms=60000)
                        engine.copy(connection, key, copied, overwrite=True, publication_policy=publication, timeout_ms=60000)
                        transfer = engine.get(connection, copied, output, overwrite=True, timeout_ms=60000)
                        assert transfer['checksum']['value'] == checksum and output.read_bytes() == payload
                    finally:
                        output.unlink(missing_ok=True)
                        errors = []
                        for owned_key in [key, copied]:
                            try:
                                engine.delete(connection, owned_key, ignore_missing=True, timeout_ms=60000)
                            except StorageError as error:
                                errors.append(error)
                        if errors and sys.exc_info()[0] is None:
                            raise errors[0]

                for iteration in range(args.warmup_cycles + args.cycles):
                    cells = []
                    for mode in modes:
                        for provider in PROVIDERS:
                            tasks = [pool.submit(cycle, provider, worker, mode) for worker in range(args.workers)]
                            for task in tasks:
                                task.result()
                            current = sample()
                            report.setdefault('after_provider', {})[provider] = current
                            report['after_mode_provider'][mode][provider] = current
                            cells.append(dict(mode=mode, provider=provider, workers=args.workers, resources=current))
                        if iteration >= args.warmup_cycles:
                            report['completed_cycles_by_mode'][mode] += 1
                    lifecycle = []
                    for mode in modes:
                        close_before = sample()
                        entered, release = threading.Event(), threading.Event()
                        def resolver(reference):
                            entered.set()
                            if not release.wait(10):
                                raise RuntimeError('cancellation fixture did not release resolver')
                            return credentials[reference]
                        cancelled_key = f'{prefix}/{mode}/cancelled'
                        with Engine(policy, credential_resolver=resolver,
                                    spool_uploads=(mode == 'private_file')) as transient:
                            cancel_before = sample()
                            token = CancellationToken()
                            task = pool.submit(transient.put, connections['webdav'], cancelled_key, source,
                                               overwrite=True, publication_policy='best_effort',
                                               cancellation=token, timeout_ms=60000)
                            try:
                                if not entered.wait(10):
                                    raise ValueError('cancellation did not reach the resolver boundary')
                                token.cancel()
                            finally:
                                release.set()
                            try:
                                task.result(timeout=10)
                            except StorageError as error:
                                assert error.category == 'cancelled'
                            else:
                                raise ValueError('cancelled transfer unexpectedly completed')
                            finally:
                                engines[mode].delete(connections['webdav'], cancelled_key,
                                                     ignore_missing=True, timeout_ms=60000)
                            cancel_after = sample()
                        lifecycle.append(dict(mode=mode, cancel_phase='resolver',
                                              close_before=close_before, close_after=sample(),
                                              cancel_before=cancel_before, cancel_after=cancel_after))
                    current = sample()
                    if iteration < args.warmup_cycles:
                        report['warmup_completed'] += 1
                        if report['warmup_completed'] == args.warmup_cycles:
                            report['after_warmup'] = current
                            report['warmup_lifecycle'] = lifecycle
                    else:
                        report['samples'].append(current)
                        report['cycle_matrix'].append(cells)
                        report['lifecycle'].append(lifecycle)
                        report['completed_cycles'] += 1
                    save()
                report['analysis'] = analyze(report['samples'])
            report['after_close'] = sample()
            # A pre-cancelled operation is a deterministic lifecycle boundary;
            # in-flight cancellation remains covered by the installed SDK suite.
            with Engine(policy) as cancelled_engine:
                token = CancellationToken()
                token.cancel()
                try:
                    cancelled_engine.put(connections['local'], 'cancelled', source, overwrite=True,
                                              publication_policy='atomic_required', cancellation=token)
                except StorageError as error:
                    assert error.category == 'cancelled'
                else:
                    raise ValueError('cancelled transfer unexpectedly completed')
            report['after_cancel'] = sample()
            validate_cycles(report)
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
