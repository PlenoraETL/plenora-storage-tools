"""Exercise real Linux ENOSPC/EACCES on an isolated tmpfs through the built CLI."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_AXES = {
    'download_disk_full': ('resource_limit', 'write', 'rolled_back'),
    'upload_disk_full': ('resource_limit', 'write', 'rolled_back'),
    'input_permission_denied': ('authorization', 'read', 'none'),
    'output_permission_denied': ('authorization', 'prepare', 'none'),
}
EXPECTED_CASES = set(EXPECTED_AXES)


def validate_report(report, binary_sha256):
    if report['schema_version'] != 1 or report['status'] != 'PASS':
        raise ValueError('local filesystem fault qualification failed')
    if report['binary_sha256'] != binary_sha256:
        raise ValueError('local filesystem fault binary differs')
    results = report['results']
    if len(results) != len(EXPECTED_CASES) or {item['name'] for item in results} != EXPECTED_CASES:
        raise ValueError('incomplete local filesystem fault qualification')
    for item in results:
        axes = (item['category'], item['phase'], item['remote_effect'])
        if item['status'] != 'PASS' or axes != EXPECTED_AXES[item['name']] or item['retry'] != {'kind': 'never'}:
            raise ValueError('unexpected local filesystem fault result')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=Path(os.environ.get('PLENORA_CLI_BIN', ROOT / 'target/debug/plenora-storage')))
    parser.add_argument('--tmpfs', type=Path, default=Path('/storage-faults'))
    parser.add_argument('--output', type=Path, default=ROOT / 'target/release-readiness/local-faults.json')
    args = parser.parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0:
        parser.error('run in the dedicated Linux test container as root (child commands drop privileges)')
    mount = args.tmpfs.resolve()
    mounts = [line.split() for line in Path('/proc/mounts').read_text().splitlines()]
    if not any(fields[1] == str(mount) and fields[2] == 'tmpfs' for fields in mounts):
        parser.error('fault directory must be a dedicated tmpfs mount')
    capacity = os.statvfs(mount)
    if capacity.f_blocks * capacity.f_frsize > 4 * 1024**2:
        parser.error('tmpfs must be at most 4 MiB')
    binary = args.binary.resolve()
    report = {'schema_version': 1, 'status': 'RUNNING', 'scope': 'Linux local filesystem, CLI and shared Rust engine',
              'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(), 'results': []}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.output.write_text(json.dumps(report, indent=2) + '\n')

    save()
    try:
        with tempfile.TemporaryDirectory(prefix='storage-fault-') as ordinary, \
                tempfile.TemporaryDirectory(prefix='owned-', dir=mount) as limited:
            root, full = Path(ordinary), Path(limited)
            root.chmod(0o755)
            full.chmod(0o777)
            storage = root / 'storage'
            storage.mkdir(mode=0o777)
            storage.chmod(0o777)
            source = root / 'input'
            source.write_bytes(b'synthetic-storage-payload\n' * 350000)
            source.chmod(0o644)
            (storage / 'source').write_bytes(source.read_bytes())

            def connection(folder, name):
                path = root / name
                path.write_text(json.dumps({'provider': 'local', 'config_contract': 'plenora-storage-local-connection-v1',
                                            'config': {'root': str(folder)}, 'credential_ref': 'local:process'}))
                path.chmod(0o644)
                return path

            normal, constrained = connection(storage, 'normal.json'), connection(full, 'constrained.json')

            def check(name, connection_file, operation, arguments, category, phase, effect='none'):
                command = [str(binary), '--format', 'json', operation, '--connection', str(connection_file), *map(str, arguments)]
                process = subprocess.run(command, capture_output=True, text=True, timeout=30,
                                         user=65534, group=65534, extra_groups=[], cwd=root)
                if process.returncode == 0 or process.stderr or len(process.stdout.splitlines()) != 1:
                    raise ValueError(f'{name}: expected one redacted error envelope')
                document = json.loads(process.stdout)
                error = document['error']
                expected = (category, phase, effect, {'kind': 'never'})
                actual = (error['category'], error['phase'], error['remote_effect'], error['retry'])
                if document['status'] != 'error' or actual != expected:
                    raise ValueError(f'{name}: unexpected error axes {actual}')
                for private in (ordinary, limited, 'synthetic-storage-payload'):
                    if private in process.stdout:
                        raise ValueError(f'{name}: private input leaked')
                report['results'].append({'name': name, 'status': 'PASS', 'category': category, 'phase': phase,
                                          'remote_effect': error['remote_effect'], 'retry': error['retry']})

            sentinel = b'previous-content-must-survive'
            destination = full / 'existing'
            destination.write_bytes(sentinel)
            destination.chmod(0o666)
            check('download_disk_full', normal, 'get', ['--key', 'source', '--output', destination, '--overwrite', 'true'],
                  'resource_limit', 'write', 'rolled_back')
            if destination.read_bytes() != sentinel or set(full.iterdir()) != {destination}:
                raise ValueError('failed download altered final content or left staging files')
            check('upload_disk_full', constrained, 'put', ['--key', 'existing', '--input', source, '--overwrite', 'true',
                                                         '--publication-policy', 'atomic-required'], 'resource_limit', 'write', 'rolled_back')
            if destination.read_bytes() != sentinel or set(full.iterdir()) != {destination}:
                raise ValueError('failed upload altered final content or left staging files')
            source.chmod(0)
            try:
                check('input_permission_denied', normal, 'put', ['--key', 'new', '--input', source, '--overwrite', 'false',
                                                               '--publication-policy', 'atomic-required'], 'authorization', 'read')
            finally:
                source.chmod(0o644)
            protected = root / 'protected'
            protected.mkdir()
            existing = protected / 'existing'
            existing.write_bytes(sentinel)
            protected.chmod(0o555)
            try:
                check('output_permission_denied', normal, 'get', ['--key', 'source', '--output', existing, '--overwrite', 'true'],
                      'authorization', 'prepare')
                if existing.read_bytes() != sentinel or set(protected.iterdir()) != {existing}:
                    raise ValueError('denied download altered final content or left staging files')
            finally:
                protected.chmod(0o755)
            if set(storage.iterdir()) != {storage / 'source'}:
                raise ValueError('denied upload produced remote effects')
        if {item['name'] for item in report['results']} != EXPECTED_CASES:
            raise ValueError('incomplete local fault qualification')
        report['status'] = 'PASS'
        validate_report(report, report['binary_sha256'])
    except BaseException as error:
        report.update(status='FAIL', failure_type=type(error).__name__)
        raise
    finally:
        save()
    print(json.dumps(report))


if __name__ == '__main__':
    main()
