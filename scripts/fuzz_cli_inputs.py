"""Seeded mutation campaign for CLI connection JSON, object keys and cursors.

Operations are read-only against a private local fixture. This is a black-box
campaign, not coverage-guided fuzzing of the FTP/XML protocol parsers.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SECRET = 'fuzz-credential-value-never-in-diagnostics'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=int, default=2000)
    parser.add_argument('--seed', type=int, default=7319)
    parser.add_argument('--output', type=Path, default=ROOT / 'target/release-readiness/fuzz-cli.json')
    args = parser.parse_args()
    if args.cases < 1:
        parser.error('cases must be positive')
    rng = random.Random(args.seed)
    binary = Path(os.environ.get('PLENORA_CLI_BIN', ROOT / 'target/release/plenora-storage')).resolve()
    report = {'schema_version': 1, 'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
              'seed': args.seed, 'requested_cases': args.cases, 'completed_cases': 0, 'status': 'RUNNING',
              'scope': ['connection_json', 'object_key', 'list_cursor'], 'read_only': True}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix='storage-fuzz-') as temporary:
            root = Path(temporary)
            storage = root / 'storage'
            storage.mkdir()
            marker = storage / 'known'
            marker.write_bytes(b'fixture-must-remain-unchanged')
            connection = root / 'connection.json'
            original = {'provider': 'local', 'config_contract': 'plenora-storage-local-connection-v1',
                        'config': {'root': str(storage)}, 'credential_ref': 'local:process'}
            corpus = ['known', '../escape', '/absolute', 'a/../b', '.', '..', 'a//b', 'a\\b',
                      'a\r\nb', '%2e%2e', '\u202e', '日本語', '', 'x' * 4096]
            for index in range(args.cases):
                document = json.loads(json.dumps(original))
                mode = index % 3
                text = rng.choice(corpus)
                if rng.randrange(2):
                    text += ''.join(chr(rng.randrange(1, 128)) for _ in range(rng.randrange(32)))
                if mode == 0:
                    variants = [None, [], {'unexpected': SECRET}, dict(document, password=SECRET),
                                dict(document, config={'root': str(storage), 'password': SECRET}),
                                dict(document, provider='unavailable-provider'),
                                dict(document, credential_ref={'unexpected': SECRET}),
                                dict(document, config_contract=text)]
                    raw = json.dumps(rng.choice(variants)).encode()
                    if rng.randrange(3) == 0:
                        raw = rng.choice([raw[:rng.randrange(len(raw) + 1)], rng.randbytes(rng.randrange(256)), b' ' * 262145])
                    connection.write_bytes(raw)
                    arguments = ['stat', '--key', 'known']
                else:
                    connection.write_text(json.dumps(document))
                    arguments = ['stat', '--key', text] if mode == 1 else ['list', '--cursor', text]
                result = subprocess.run([str(binary), '--format', 'json', *arguments,
                                         '--connection', str(connection)], capture_output=True, timeout=5)
                assert not result.stderr, 'public CLI emitted unexpected stderr'
                assert SECRET.encode() not in result.stdout, 'credential exposed in diagnostic'
                assert len(result.stdout.splitlines()) == 1, 'expected one protocol envelope'
                envelope = json.loads(result.stdout)
                assert envelope['status'] in {'ok', 'error'}
                if envelope['status'] == 'error':
                    assert result.returncode > 0, 'error did not use a failure exit code'
                    error = envelope['error']
                    assert all(name in error for name in ['code', 'category', 'phase', 'remote_effect', 'retry', 'message'])
                    assert error['remote_effect'] == 'none', 'read-only rejection reports a mutation'
                else:
                    assert result.returncode == 0
                assert marker.read_bytes() == b'fixture-must-remain-unchanged'
                assert [path.name for path in storage.iterdir()] == ['known']
                report['completed_cases'] = index + 1
        report['status'] = 'PASS'
    except BaseException:
        report['status'] = 'FAIL'
        report['failing_case'] = report['completed_cases']
        raise
    finally:
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
