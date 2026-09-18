"""Verify artifact manifests, checksums and available qualification digests."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'contracts/upstream'))
from conformance_checks import adoption_errors


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path, help='A version directory under dist/')
    args = parser.parse_args()
    manifests = sorted(args.directory.glob('*/release-manifest.json'))
    assert manifests, 'no release manifests'
    sources = set()
    results = []
    for path in manifests:
        folder = path.parent
        manifest = json.loads(path.read_text())
        sources.add(manifest['source_sha256'])
        sums = {}
        for line in (folder / 'SHA256SUMS').read_text().splitlines():
            expected, name = line.split('  ', 1)
            assert Path(name).name == name and name not in sums, 'invalid checksum entry'
            actual = digest(folder / name)
            assert actual == expected, f'checksum mismatch: {name}'
            sums[name] = actual
        assert set(sums) == {'release-manifest.json'} | {item['name'] for item in manifest['artifacts']}
        for item in manifest['artifacts']:
            assert sums[item['name']] == item['sha256'], item['name']
            assert (folder / item['name']).stat().st_size == item['size'], item['name']
        adoption = json.loads((folder / 'adoption-manifest-v4.json').read_text())
        assert adoption['schema_version'] == 4 and not adoption_errors(adoption)
        for artifact in adoption['artifacts']:
            name = artifact['name']
            if name == 'plenora-storage-runtime-binding':
                name = f"plenora-storage-core-{manifest['version']}.crate"
            assert artifact['version'] == manifest['version']
            assert artifact['digest'] == 'sha256:' + sums[name], name
        qualification_path = folder / 'qualification.json'
        if qualification_path.exists():
            qualification = json.loads(qualification_path.read_text())
            binary = 'plenora-storage.exe' if 'windows' in manifest['target'] else 'plenora-storage'
            assert qualification['binary_sha256'] == sums[binary], 'qualification binary differs'
        for name in ['extended-qualification.json', 'extended-regressions.json', 'cli-regressions.json', 'commit-faults.json']:
            report_path = folder / name
            if report_path.exists():
                report = json.loads(report_path.read_text())
                binary = 'plenora-storage.exe' if 'windows' in manifest['target'] else 'plenora-storage'
                assert report['binary_sha256'] == sums[binary], f'{name}: binary differs'
        results.append({'target': manifest['target'], 'artifacts': len(manifest['artifacts']),
                        'qualification_present': qualification_path.exists(), 'status': 'PASS'})
    assert len(sources) == 1, 'source snapshots differ across targets'
    print(json.dumps({'source_sha256': sources.pop(), 'results': results}, indent=2))


if __name__ == '__main__':
    main()
