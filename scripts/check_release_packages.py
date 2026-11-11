"""Exercise release guards on built artifacts; fixture evidence is not qualification."""
import argparse
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

from check_packages import (generated_member, member_rules, registry,
                            repository_snapshot, verify_distribution)
from versioning import workspace_version


@repository_snapshot
def verify_publication(paths):
    """Shared by prepare/upload/publish and the clean-runner PR regression."""
    for path in paths:
        verify_distribution(path)


@repository_snapshot
def check(directory):
    version = workspace_version().native
    expected = {f'{name}-{version}.crate' for name in registry()['crates']
                if name != 'plenora-storage-py'}
    actual = {path.name for path in directory.iterdir()}
    if actual != expected:
        raise ValueError('release regression requires the complete Cargo archive set')
    verify_publication(directory / name for name in sorted(expected))
    print(f'PASS release package guard ({len(expected)} built archives)')


@repository_snapshot
def check_release(directory, output):
    """Use real build outputs; only campaign-only evidence may be fixture text."""
    from release_publication import target_documents
    import strict_json
    version = workspace_version()
    targets = {'x86_64-pc-windows-msvc', 'x86_64-unknown-linux-gnu'}
    manifests = list(directory.glob('*/release-manifest.json'))
    if {p.parent.name for p in manifests} != targets:
        raise ValueError('release regression requires both complete target builds')
    subprocess.run([sys.executable, str(Path(__file__).with_name('verify_release.py')),
                    str(directory)], check=True)
    output.mkdir(parents=True, exist_ok=False)
    built = set()
    public = []
    for path in manifests:
        folder = path.parent
        manifest = strict_json.loads(path.read_text(encoding='utf-8'))
        required = {f'{name}-{version.native}.crate' for name in registry()['crates']
                    if name != 'plenora-storage-py'} | {
            f'plenora-storage-{version.native}-source.tar.gz', 'source-consumer.log',
            'source-consumer.json', 'python-tests.log', 'python-tests.json',
            'storage-sbom.cdx.json', 'qualification-sbom.cdx.json', 'adoption-manifest-v4.json'}
        names = {item['name'] for item in manifest['artifacts']}
        if not required <= names or len([n for n in names if n.endswith('.whl')]) != 1:
            raise ValueError('incomplete release build in package regression')
        built.update(folder.name + '/' + n for n in names | {'SHA256SUMS', path.name})
        public.extend(folder / n for n in names if n.endswith(('.crate', '.whl', '.tar.gz', '.zip')))
        for name, destination in target_documents(version.native, folder.name).items():
            shutil.copyfile(folder / name, output / destination)
            public.append(output / destination)
    rules = member_rules(registry(), 'qualification', version.native, version.python)
    if not built <= rules.keys():
        raise ValueError('built artifact missing from qualification inventory')
    fixture = b'{"scope":"PR package guard fixture; not qualification evidence"}\n'
    bundle = output / f'plenora-storage-{version.native}-qualification.tar.gz'
    with tarfile.open(bundle, 'w:gz') as archive:
        for name in rules:
            source = directory / name
            if name in built:
                data = source.read_bytes()
            elif generated_member('qualification', name) and not source.exists():
                data = fixture
            elif source.is_file():
                data = source.read_bytes()
            else:
                raise ValueError('non-evidence member missing from real release builds')
            member = tarfile.TarInfo(version.native + '/' + name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    receipt = output / 'release-qualification.json'
    receipt.write_bytes(fixture)
    verify_publication([*public, bundle, receipt])
    print('PASS target manifests, standalone publication assets and recursive qualification inventory')
    print('Campaign-only text is fixture data, not a qualification claim')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--release', action='store_true')
    parser.add_argument('--output', type=Path, default=Path('target/publication-guard-fixture'))
    args = parser.parse_args()
    if args.release:
        check_release(args.directory, args.output)
    else:
        check(args.directory)
