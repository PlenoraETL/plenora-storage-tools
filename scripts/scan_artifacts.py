"""Scan the exact CLI archive and wheel using a pinned Syft executable.

Each scan covers extracted distribution bytes, not the checkout or the build
host. Preserve native Syft output, a CycloneDX view and the input digest. A
successful scan can discover no packages: that is a documented detection limit,
not proof that the binary contains no dependencies.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'scripts/syft.yaml'
SYFT_VERSION = '1.52.0'
TARGETS = ('x86_64-unknown-linux-gnu', 'x86_64-pc-windows-msvc')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def native_inventory(raw, expected):
    files = {f['location']['path'].replace('\\', '/').lstrip('/'): f for f in raw.get('files', [])}
    result = {}
    for name, identity in expected.items():
        found = files.get(name, {})
        executable = found.get('executable', {})
        if (found.get('metadata', {}).get('size') != identity['size']
                or executable.get('format') not in {'elf', 'pe'}):
            raise ValueError('scanner omitted a native file or its executable metadata')
        # Syft does not emit per-file digests on every host. Our extractor hashes
        # every native subject; verify scanner digests too whenever available.
        hashes = [d['value'] for d in found.get('digests', []) if d.get('algorithm') in {'sha256', 'sha-256'}]
        if hashes and hashes != [identity['sha256']]:
            raise ValueError('scanner file digest differs from extracted bytes')
        result[name] = {'format': executable['format'],
                        'imported_libraries': sorted(executable.get('importedLibraries') or [])}
    return result


def unpack(archive, destination):
    """Extract regular files only, with a bounded, unique and confined inventory."""
    seen = set()
    total = 0

    def write(name, size, read):
        nonlocal total
        relative = PurePosixPath(name)
        if (relative.is_absolute() or '..' in relative.parts or '\\' in name
                or ':' in name or relative.as_posix() != name or name in seen or size < 0):
            raise ValueError('unsafe or duplicate distribution member')
        seen.add(name)
        total += size
        if total > 1024**3 or len(seen) > 10000:
            raise ValueError('distribution extraction budget exceeded')
        path = destination.joinpath(*relative.parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = read()
        if len(data) != size:
            raise ValueError('distribution member size differs')
        path.write_bytes(data)

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as stream:
            for member in stream.infolist():
                if member.is_dir():
                    continue
                if stat.S_IFMT(member.external_attr >> 16) not in (0, stat.S_IFREG):
                    raise ValueError('distribution contains a non-regular file')
                write(member.orig_filename, member.file_size, lambda: stream.read(member))
    else:
        with tarfile.open(archive) as stream:
            for member in stream.getmembers():
                if not member.isfile():
                    raise ValueError('distribution contains a non-regular file')
                write(member.name, member.size, lambda: stream.extractfile(member).read())


def scan_target(folder, output, syft, revision, *, development=False):
    manifest = json.loads((folder / 'release-manifest.json').read_text())
    if manifest['source_revision'] != revision or not manifest['source_committed']:
        raise ValueError('scan requires the exact committed distribution source')
    target = manifest['target']
    if target not in TARGETS:
        raise ValueError('unsupported scan target')
    version = manifest['version']
    binary = 'plenora-storage.exe' if 'windows' in target else 'plenora-storage'
    suffix = '.zip' if 'windows' in target else '.tar.gz'
    artifacts = {item['name']: item for item in manifest['artifacts']}
    wheels = [name for name in artifacts if name.endswith('.whl')]
    if len(wheels) != 1:
        raise ValueError('expected exactly one wheel per target')
    subjects = {'cli': f'plenora-storage-{version}-{target}{suffix}', 'wheel': wheels[0]}
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / 'report.json'
    report_path.unlink(missing_ok=True)
    rows = []
    for role, name in subjects.items():
        archive = folder / name
        if digest(archive) != artifacts[name]['sha256']:
            raise ValueError('scan input differs from release manifest')
        with tempfile.TemporaryDirectory(prefix='storage-artifact-scan-') as temporary:
            directory = Path(temporary)
            unpack(archive, directory)
            if role == 'cli' and digest(directory / binary) != artifacts[binary]['sha256']:
                raise ValueError('CLI archive differs from the qualified executable')
            native = {path.relative_to(directory).as_posix(): {'sha256': digest(path), 'size': path.stat().st_size}
                      for path in directory.rglob('*') if path.is_file()
                      and (path.name == binary or path.suffix in {'.so', '.pyd', '.dll'})}
            if not native:
                raise ValueError('distribution has no native scan subject')
            raw_path = output / (role + '.syft.json')
            cdx_path = output / (role + '.cdx.json')
            environment = {key: value for key, value in os.environ.items() if not key.startswith('SYFT_')}
            subprocess.run([syft, 'scan', 'dir:' + str(directory), '--config', str(CONFIG),
                            '--source-name', name, '--source-version', version,
                            '-o', 'syft-json=' + str(raw_path),
                            '-o', 'cyclonedx-json=' + str(cdx_path)],
                           cwd=ROOT, env=environment, check=True)
            raw = json.loads(raw_path.read_text(encoding='utf-8'))
            if raw.get('descriptor', {}).get('version') != SYFT_VERSION:
                raise ValueError('scanner version differs from pinned policy')
            detected = native_inventory(raw, native)
            if any(digest(directory / name) != identity['sha256'] for name, identity in native.items()):
                raise ValueError('native bytes changed during scanner execution')
            rows.append({'role': role, 'artifact': name, 'artifact_sha256': digest(archive),
                         'native_imports': detected,
                         'native_files': native, 'packages_detected': len(raw['artifacts']),
                         'raw': raw_path.name, 'raw_sha256': digest(raw_path),
                         'cyclonedx': cdx_path.name, 'cyclonedx_sha256': digest(cdx_path)})
    report = {'schema_version': 1, 'status': 'DEVELOPMENT' if development else 'PASS',
              'source_revision': revision, 'dirty': development, 'version': version, 'target': target,
              'scanner': {'name': 'syft', 'version': SYFT_VERSION, 'config_sha256': digest(CONFIG)},
              'scope': 'extracted CLI archive and wheel; detected packages, not all linked or host dependencies',
              'results': rows}
    report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8', newline='\n')
    return report


def validate(evidence, prefix, revision, subjects):
    report = evidence.json(prefix + '/report.json')
    target = prefix.rsplit('/', 1)[-1]
    if (report['status'] != 'PASS' or report['source_revision'] != revision or report['dirty'] is not False
            or report['target'] != target or report['scanner'] != {
                'name': 'syft', 'version': SYFT_VERSION, 'config_sha256': digest(CONFIG)}):
        raise ValueError('native inventory identity or scanner policy differs')
    rows = report['results']
    if len(rows) != 2 or {row['role'] for row in rows} != {'cli', 'wheel'}:
        raise ValueError('native scan omitted or duplicated a distribution surface')
    for row in rows:
        expected = subjects['cli_archive_sha256' if row['role'] == 'cli' else 'wheel_sha256']
        if row['artifact_sha256'] != expected or not row['native_files']:
            raise ValueError('native scan describes different distribution bytes')
        raw = json.loads(evidence.read(prefix + '/' + row['raw'], row['raw_sha256']))
        cdx = json.loads(evidence.read(prefix + '/' + row['cyclonedx'], row['cyclonedx_sha256']))
        if (raw.get('descriptor', {}).get('name') != 'syft'
                or raw['descriptor'].get('version') != SYFT_VERSION
                or not isinstance(raw.get('artifacts'), list)
                or len(raw['artifacts']) != row['packages_detected']
                or cdx.get('bomFormat') != 'CycloneDX' or cdx.get('specVersion') != '1.6'):
            raise ValueError('native scan summary differs from attached scanner output')
        component = cdx.get('metadata', {}).get('component', {})
        if (raw.get('source', {}).get('name') != row['artifact']
                or raw['source'].get('version') != report['version']
                or component.get('name') != row['artifact'] or component.get('version') != report['version']):
            raise ValueError('scanner output describes a different distribution identity')
        detected = {(item['name'], item.get('version', '')) for item in raw['artifacts']}
        converted = {(item['name'], item.get('version', '')) for item in cdx.get('components', [])
                     if item['type'] != 'file'}
        if detected != converted:
            raise ValueError('CycloneDX packages differ from the original scanner inventory')
        if native_inventory(raw, row['native_files']) != row['native_imports']:
            raise ValueError('native import summary differs from scanner output')
        if row['role'] == 'cli' and subjects['binary_sha256'] not in {
                item['sha256'] for item in row['native_files'].values()}:
            raise ValueError('scanner did not inventory the qualified CLI executable')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--syft', default='syft')
    parser.add_argument('--development', action='store_true', help='Non-qualifying local scanner check')
    args = parser.parse_args()
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    if not args.development and subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip():
        raise ValueError('qualification scans require a clean checkout')
    manifests = list(args.directory.rglob('release-manifest.json'))
    if not manifests:
        raise ValueError('no release manifests to scan')
    targets = set()
    for path in manifests:
        manifest = json.loads(path.read_text())
        target = manifest['target']
        if target in targets:
            raise ValueError('duplicate release target')
        targets.add(target)
        scan_target(path.parent.resolve(), (args.output / target).resolve(), args.syft,
                    manifest['source_revision'] if args.development else revision,
                    development=args.development)
    print('PASS scanner execution' if not args.development else 'Completed non-qualifying development scan')


if __name__ == '__main__':
    main()
