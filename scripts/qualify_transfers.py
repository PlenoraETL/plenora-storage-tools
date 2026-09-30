"""Measure real CLI transfers and verify bounded providers fail before mutation.

Linux fixture gate. Defaults to 1 GiB for streaming paths; buffered providers
retain their documented 64 MiB limit. Each report identifies the tested binary.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import platform
import signal
import subprocess
import sys
import tempfile
import uuid

from fixture_connections import ATOMIC, BUFFERED, PROVIDERS, ROOT, fixture


class OperationFailure(AssertionError):
    """Retain only public error axes when a qualification operation fails."""

    def __init__(self, operation, axes):
        self.axes = axes
        super().__init__(f'{operation}: unexpected result: {axes}')


def measurement_environment():
    """Identify the test platform without recording host names or endpoints."""
    fixture_files = subprocess.check_output(['git', 'ls-files', '--', 'docker'], cwd=ROOT, text=True).splitlines()
    sources = [ROOT / 'docker-compose.yml', ROOT / 'compose.extended.yml',
               *(ROOT / name for name in sorted(fixture_files))]
    fixture_hash = hashlib.sha256()
    for path in sources:
        fixture_hash.update(path.relative_to(ROOT).as_posix().encode() + b'\0' + path.read_bytes())
    model = next((line.split(':', 1)[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines()
                  if line.startswith('model name')), platform.machine())
    return {'machine': platform.machine(), 'kernel': platform.release(), 'cpu_count': os.cpu_count(),
            'cpu_model': model, 'fixture_sha256': fixture_hash.hexdigest()}


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def payload(path, size):
    block = random.Random(7319).randbytes(1024 * 1024)
    result = hashlib.sha256()
    with path.open('wb') as output:
        remaining = size
        while remaining:
            part = block[:min(remaining, len(block))]
            output.write(part)
            result.update(part)
            remaining -= len(part)
    return result.hexdigest()


def invoke(binary, connection, env, operation, arguments, max_bytes, timeout, rss_limit, expected='ok', spool_uploads=False):
    command = [str(binary), '--format', 'json', '--allow-private-network', '--allow-insecure-http',
               '--allow-insecure-ftp', '--max-transfer-bytes', str(max_bytes),
               '--deadline', (datetime.now(timezone.utc) + timedelta(seconds=timeout)).isoformat(),
               *(['--spool-uploads'] if spool_uploads else []),
               operation, '--connection', str(connection),
               *map(str, arguments)]
    with tempfile.TemporaryDirectory(prefix='storage-process-measure-') as temporary:
        measurement = Path(temporary) / 'usage.json'
        process = subprocess.Popen([sys.executable, str(ROOT / 'scripts/measure_process.py'), str(measurement), *command],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=timeout + 15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise RuntimeError(f'{operation}: process exceeded cooperative deadline; remote outcome requires inspection') from None
        measure = json.loads(measurement.read_text())
    if stderr or len(stdout.splitlines()) != 1:
        raise AssertionError(f'{operation}: invalid CLI envelope')
    response = json.loads(stdout)
    if response['status'] != expected or (expected == 'ok' and process.returncode != 0):
        error = response.get('error', {})
        axes = {name: error.get(name) for name in ('code', 'category', 'phase', 'remote_effect', 'retry')}
        raise OperationFailure(operation, axes)
    peak = measure['peak_rss_bytes']
    if not peak:
        raise AssertionError(f'{operation}: process memory was not measured')
    if peak > rss_limit:
        raise AssertionError(f'{operation}: peak RSS {peak} exceeds gate {rss_limit}')
    return response, dict(measure, operation=operation, status='PASS')


def roundtrip(binary, provider, source, source_hash, size, root, timeout, rss_limit, buffered_limit, shared_parent, spool_uploads=False):
    work = root / (provider + '-' + uuid.uuid4().hex)
    work.mkdir()
    local = root / 'storage'
    connection, credentials = fixture(provider, local)
    path = work / 'connection.json'
    path.write_text(json.dumps(connection))
    env = dict(os.environ, PLENORA_TEST_CREDENTIALS=json.dumps(credentials))
    prefix = shared_parent + '/' + uuid.uuid4().hex
    key, copied = prefix + '/source', prefix + '/copy'
    policy = 'atomic-required' if provider in ATOMIC else 'best-effort'
    measures = []

    def call(operation, *arguments, expected='ok'):
        response, measure = invoke(binary, path, env, operation, arguments, max(size, 1024), timeout, rss_limit, expected, spool_uploads)
        measures.append(measure)
        return response

    def upload(input_path, expected='ok', overwrite='true'):
        return call('put', '--key', key, '--input', input_path, '--overwrite', overwrite,
                    '--publication-policy', policy, expected=expected)

    try:
        call('test')
        if not spool_uploads and provider in BUFFERED and size > buffered_limit:
            sentinel = work / 'sentinel'
            sentinel.write_bytes(b'previous-object-must-survive')
            upload(sentinel)
            rejected = upload(source, expected='error')
            error = rejected['error']
            assert error['category'] == 'resource_limit' and error['remote_effect'] == 'none'
            downloaded = work / 'after-rejection'
            call('get', '--key', key, '--output', downloaded, '--overwrite', 'false')
            assert downloaded.read_bytes() == sentinel.read_bytes()
            mode = 'documented_limit_preserves_destination'
        else:
            transfer = upload(source)['result']
            assert transfer['bytes_transferred'] == size and transfer['checksum']['value'] == source_hash
            assert call('stat', '--key', key)['result']['size'] == size
            assert call('copy', '--source-key', key, '--destination-key', copied, '--overwrite', 'true',
                        '--publication-policy', policy)['result']['size'] == size
            downloaded = work / 'download'
            transfer = call('get', '--key', copied, '--output', downloaded, '--overwrite', 'false')['result']
            assert transfer['bytes_transferred'] == size and transfer['checksum']['value'] == source_hash
            assert digest(downloaded) == source_hash
            downloaded.unlink()
            if provider == 's3' and size > buffered_limit:
                error = upload(source, expected='error', overwrite='false')['error']
                assert error['category'] == 'resource_limit' and error['remote_effect'] == 'none'
                assert call('stat', '--key', key)['result']['size'] == size
            mode = 'streaming_roundtrip' if provider not in BUFFERED else 'buffered_roundtrip'
            if spool_uploads and provider in BUFFERED:
                mode = 'private_file_roundtrip'
    finally:
        # Only the unpredictable names owned by this test are eligible for cleanup.
        # A failed cleanup fails the gate; it must never erase a transfer failure.
        failures = []
        for name in [key, copied]:
            try:
                call('delete', '--key', name, '--ignore-missing', 'true')
            except Exception as error:
                failures.append({'key': name, 'failure_type': type(error).__name__,
                                 'axes': error.axes if isinstance(error, OperationFailure) else {}})
        if failures and sys.exc_info()[0] is None:
            raise RuntimeError('fixture cleanup failed for operation-owned objects: ' + json.dumps(failures))
    return {'provider': provider, 'mode': mode, 'payload_bytes': size, 'status': 'PASS', 'measurements': measures}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bytes', type=int, default=1024**3)
    parser.add_argument('--providers', default=','.join(PROVIDERS))
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--rounds', type=int, default=1)
    parser.add_argument('--spool-uploads', action='store_true')
    parser.add_argument('--timeout', type=float, default=600)
    parser.add_argument('--rss-limit-mib', type=int, default=256)
    parser.add_argument('--output', type=Path, default=ROOT / 'target/release-readiness/large-transfers.json')
    args = parser.parse_args()
    providers = args.providers.split(',')
    if sys.platform != 'linux' or not set(providers) <= set(PROVIDERS) or min(args.bytes, args.workers, args.rounds, args.timeout, args.rss_limit_mib) <= 0:
        parser.error('requires Linux, known fixture providers and positive resource limits')
    binary = Path(os.environ.get('PLENORA_CLI_BIN', ROOT / 'target/release/plenora-storage')).resolve()
    report = {'schema_version': 1, 'binary_sha256': digest(binary), 'platform': sys.platform,
              'campaign_id': str(uuid.uuid4()), 'started_utc': datetime.now(timezone.utc).isoformat(),
              'environment': measurement_environment(),
              'source_revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip()),
              'memory_measurement': 'Linux RUSAGE_CHILDREN per fresh wrapper; includes process startup',
              'payload_bytes': args.bytes, 'workers': args.workers, 'rounds': args.rounds,
              'spool_uploads': args.spool_uploads,
              'rss_limit_bytes': args.rss_limit_mib * 1024**2, 'status': 'RUNNING', 'results': []}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.output.write_text(json.dumps(report, indent=2) + '\n')

    save()
    try:
        with tempfile.TemporaryDirectory(prefix='storage-transfer-gate-') as temporary:
            root = Path(temporary)
            (root / 'storage').mkdir()
            source = root / 'payload'
            source_hash = payload(source, args.bytes)
            report['payload_sha256'] = source_hash
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                for iteration in range(args.rounds):
                    for provider in providers:
                        # A new shared directory makes the mkdir race repeatable
                        # across concurrent writers, even on reused fixtures.
                        shared_parent = 'qualification/large-' + uuid.uuid4().hex
                        tasks = [pool.submit(roundtrip, binary, provider, source, source_hash, args.bytes,
                                             root, args.timeout, report['rss_limit_bytes'], 64 * 1024**2, shared_parent, args.spool_uploads)
                                 for _ in range(args.workers)]
                        for task in tasks:
                            result = task.result()
                            result['round'] = iteration
                            report['results'].append(result)
                            save()
                        print(f'PASS {provider}: {args.workers} workers, round {iteration + 1}', flush=True)
        report['status'] = 'PASS'
    except BaseException:
        report['status'] = 'FAIL'
        raise
    finally:
        save()


if __name__ == '__main__':
    main()
