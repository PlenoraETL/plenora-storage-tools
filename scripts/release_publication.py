"""Prepare, smoke-test and publish only a fully qualified GitHub draft release."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

from package_source import extract
from release_evidence import TARGETS, require
from versioning import workspace_version

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_tag(tag):
    require(tag == 'v' + workspace_version().native, 'tag differs from workspace version')
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    tagged = subprocess.check_output(['git', 'rev-parse', tag + '^{commit}'], cwd=ROOT, text=True).strip()
    require(revision == tagged, 'checkout differs from release tag')
    return revision


def draft(tag):
    check_tag(tag)
    metadata = json.loads(subprocess.check_output(['gh', 'release', 'view', tag, '--json', 'isDraft,tagName'], text=True))
    require(metadata['isDraft'] is True and metadata['tagName'] == tag, 'publication requires an unpublished draft')


def qualified_files(release):
    """Only manifest and receipt entries may enter a public evidence archive."""
    receipt = json.loads((release / 'release-qualification.json').read_text())
    require(receipt['status'] == 'qualified_for_publication' and receipt['schema_version'] == 2,
            'release receipt is not qualified')
    paths = {'release-qualification.json', 'SHA256SUMS'}
    for platform in receipt['platforms']:
        target = platform['target']
        require(target in TARGETS, 'unsupported qualification target')
        folder = release / target
        manifest = json.loads((folder / 'release-manifest.json').read_text())
        paths.update([f'{target}/release-manifest.json', f'{target}/SHA256SUMS'])
        for artifact in manifest['artifacts']:
            name = artifact['name']
            require(Path(name).name == name and '/' not in name and '\\' not in name, 'unsafe artifact name')
            require(digest(folder / name) == artifact['sha256'], 'manifest artifact changed')
            paths.add(f'{target}/{name}')
        for entry in platform['evidence']:
            name = entry['name']
            require(Path(name).name == name and '/' not in name and '\\' not in name, 'unsafe evidence name')
            common = name in ('audit.json', 'deny.log', 'smb-upstream-audit.json', f'{target}-tests.log')
            path = ('evidence/' if common else target + '/') + name
            require(digest(release / path) == entry['sha256'], 'qualified evidence changed')
            paths.add(path)
    for entry in receipt['additional_gates']:
        path = release / 'evidence/gates' / entry['name']
        require(path.resolve().is_relative_to((release / 'evidence/gates').resolve()), 'escaped gate evidence')
        require(digest(path) == entry['sha256'], 'qualified gate evidence changed')
        paths.add(path.relative_to(release).as_posix())
    for name in paths:
        require((release / name).resolve().is_relative_to(release.resolve()), 'escaped qualified file')
    return sorted(paths)


def check_files(directory):
    index = json.loads((directory / 'publication-index.json').read_text())
    require(index['version'] == workspace_version().native, 'publication version differs')
    for name, sha in index['files'].items():
        require(Path(name).name == name and '/' not in name and '\\' not in name, 'unsafe publication filename')
        require(digest(directory / name) == sha, 'publication artifact digest differs')
    require(bool(index['files']), 'empty publication inventory')
    receipt = json.loads((directory / 'release-qualification.json').read_text())
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    require(receipt['version'] == index['version'] and receipt['source_revision'] == revision
            and receipt['status'] == 'qualified_for_publication' and receipt['schema_version'] == 2
            and bool(receipt['additional_gates']), 'publication receipt is not current and qualified')
    require(len(receipt['platforms']) == len(TARGETS)
            and {p['target'] for p in receipt['platforms']} == set(TARGETS), 'publication targets are incomplete')
    version = index['version']
    expected = {'release-qualification.json', f'plenora-storage-{version}-source.tar.gz',
                f'plenora-storage-{version}-qualification.tar.gz'}
    for platform in receipt['platforms']:
        target = platform['target']
        expected.add(f'plenora-storage-{version}-{target}' + ('.zip' if 'windows' in target else '.tar.gz'))
        expected.add(platform['wheel'])
        require(index['files'].get(platform['wheel']) == platform['wheel_sha256'], 'publication wheel differs from receipt')
    require(set(index['files']) == expected, 'publication asset inventory differs')
    return index


def prepare(archive, output, tag):
    check_tag(tag)
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix='storage-qualification-input-') as temporary:
        source = extract(archive, temporary, 'qualification')
        version = workspace_version().native
        release = source / 'dist' / version
        subprocess.run([sys.executable, str(ROOT / 'scripts/qualify_release.py'), str(release),
                        '--evidence', str(source / 'evidence')], cwd=ROOT, check=True)
        names = []
        for target in TARGETS:
            folder = release / target
            extension = '.zip' if 'windows' in target else '.tar.gz'
            cli = folder / f'plenora-storage-{version}-{target}{extension}'
            wheels = list(folder.glob('plenora_storage-*.whl'))
            require(len(wheels) == 1, 'expected one qualified wheel per target')
            for path in [cli, wheels[0], folder / f'plenora-storage-{version}-source.tar.gz']:
                destination = output / path.name
                if destination.exists():
                    require(digest(destination) == digest(path), 'shared source artifacts differ')
                else:
                    shutil.copyfile(path, destination)
                    names.append(path.name)
        shutil.copyfile(release / 'release-qualification.json', output / 'release-qualification.json')
        names.append('release-qualification.json')
        bundle = output / f'plenora-storage-{version}-qualification.tar.gz'
        with tarfile.open(bundle, 'w:gz') as stream:
            for name in qualified_files(release):
                stream.add(release / name, arcname=version + '/' + name, recursive=False)
        names.append(bundle.name)
        files = {name: digest(output / name) for name in sorted(names)}
        (output / 'publication-index.json').write_text(json.dumps(dict(version=version, files=files), indent=2) + '\n')
        (output / 'SHA256SUMS').write_text(''.join(f'{sha}  {name}\n' for name, sha in files.items())
                                         + f'{digest(output / "publication-index.json")}  publication-index.json\n')
    check_files(output)


def bundle_input(release, evidence, output):
    subprocess.run([sys.executable, str(ROOT / 'scripts/qualify_release.py'), str(release),
                    '--evidence', str(evidence)], cwd=ROOT, check=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents replacing a reviewable input from an earlier run.
    with tarfile.open(output, 'x:gz') as stream:
        for name in qualified_files(release):
            destination = ('qualification/' + name if name.startswith('evidence/') else
                           f'qualification/dist/{release.name}/{name}')
            stream.add(release / name, arcname=destination, recursive=False)


def smoke_cli(binary, version):
    """Use the same machine protocol required of installed CLI consumers."""
    identity = json.loads(subprocess.check_output([str(binary), '--format', 'json', '--version'], text=True))
    require(identity['status'] == 'ok' and identity['command'] == 'version'
            and identity['protocol_version'] == 2
            and identity['component_version'] == version
            and identity['result'] == {'component_version': version, 'cli_protocol_version': 2},
            'downloaded CLI version or protocol differs')
    catalog = json.loads(subprocess.check_output([str(binary), '--format', 'json', 'capabilities'], text=True))
    require(catalog['status'] == 'ok' and catalog['result']['component_version'] == version
            and len(catalog['result']['operations']) == 7, 'downloaded CLI discovery differs')
    return {'status': 'PASS', 'version': version, 'binary_sha256': digest(binary)}


def smoke(directory):
    check_files(directory)
    version = workspace_version().native
    target = TARGETS[1] if sys.platform == 'win32' else TARGETS[0]
    binary_name = 'plenora-storage.exe' if sys.platform == 'win32' else 'plenora-storage'
    extension = '.zip' if sys.platform == 'win32' else '.tar.gz'
    archive = directory / f'plenora-storage-{version}-{target}{extension}'
    with tempfile.TemporaryDirectory(prefix='storage-release-download-') as temporary:
        binary = Path(temporary) / binary_name
        if extension == '.zip':
            with zipfile.ZipFile(archive) as stream:
                binary.write_bytes(stream.read(binary_name))
        else:
            with tarfile.open(archive) as stream:
                binary.write_bytes(stream.extractfile(binary_name).read())
            binary.chmod(0o700)
        receipt = json.loads((directory / 'release-qualification.json').read_text())
        record = next(p for p in receipt['platforms'] if p['target'] == target)
        require(digest(binary) == record['binary_sha256'], 'downloaded CLI differs from qualified binary')
        smoke_cli(binary, version)
        wheel_tag = 'win_amd64' if sys.platform == 'win32' else 'manylinux'
        wheels = [path for path in directory.glob('*.whl') if wheel_tag in path.name]
        require(len(wheels) == 1, 'downloaded wheel is missing or ambiguous')
        subprocess.run([sys.executable, str(ROOT / 'scripts/check_installed_sdk.py'), str(wheels[0]),
                        '--coverage', '--typing', '--output', str(ROOT / 'target/release-download-smoke')], check=True)


def upload(directory, tag):
    draft(tag)
    index = check_files(directory)
    paths = [directory / name for name in [*index['files'], 'publication-index.json', 'SHA256SUMS']]
    subprocess.run(['gh', 'release', 'upload', tag, *map(str, paths), '--clobber'], check=True)


def publish(directory, tag):
    draft(tag)
    index = check_files(directory)
    with tempfile.TemporaryDirectory(prefix='storage-release-roundtrip-') as temporary:
        folder = Path(temporary)
        subprocess.run(['gh', 'release', 'download', tag, '--dir', str(folder)], check=True)
        require(check_files(folder) == index, 'GitHub download differs from qualified publication')
        require(digest(folder / 'publication-index.json') == digest(directory / 'publication-index.json')
                and digest(folder / 'SHA256SUMS') == digest(directory / 'SHA256SUMS'), 'downloaded inventory differs')
        actual = {p.name for p in folder.iterdir()}
        expected = set(index['files']) | {'publication-index.json', 'SHA256SUMS'}
        require(actual in (expected, expected | {'qualification-input.tar.gz'}),
                'draft contains assets outside the qualified publication')
        has_input = 'qualification-input.tar.gz' in actual
    # Remove the input archive while still a draft. Only the validated bundle is public.
    if has_input:
        subprocess.run(['gh', 'release', 'delete-asset', tag, 'qualification-input.tar.gz', '--yes'], check=True)
    command = ['gh', 'release', 'edit', tag, '--draft=false', '--prerelease=' +
               ('true' if workspace_version().stage else 'false')]
    subprocess.run(command, check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['bundle', 'draft', 'prepare', 'smoke', 'smoke-cli', 'upload', 'publish'])
    parser.add_argument('--tag')
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--binary', type=Path)
    args = parser.parse_args()
    if args.action == 'bundle':
        bundle_input(args.directory.resolve(), args.evidence.resolve(), args.archive.resolve())
    elif args.action == 'draft':
        draft(args.tag)
    elif args.action == 'prepare':
        prepare(args.archive, args.directory.resolve(), args.tag)
    elif args.action == 'smoke':
        smoke(args.directory.resolve())
    elif args.action == 'smoke-cli':
        print(json.dumps(smoke_cli(args.binary.resolve(), workspace_version().native)))
    elif args.action == 'upload':
        upload(args.directory.resolve(), args.tag)
    else:
        publish(args.directory.resolve(), args.tag)
