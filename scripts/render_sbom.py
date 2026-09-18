"""Deterministic CycloneDX 1.6 inventory of the complete Cargo.lock graph.

Scope includes dev, optional and target-specific packages, not a claim that every
package is linked in every binary. Python has no third-party runtime dependency.
OS packages and build tools are outside this inventory.
"""
import argparse
import hashlib
import json
from pathlib import Path
import tomllib
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def package_ref(package):
    source = hashlib.sha256(package.get('source', 'workspace').encode()).hexdigest()[:16]
    return f'cargo:{package["name"]}@{package["version"]}:{source}'


def render(artifacts=()):
    workspace = tomllib.loads((ROOT / 'Cargo.toml').read_text())['workspace']
    packages = tomllib.loads((ROOT / 'Cargo.lock').read_text())['package']
    version = workspace['package']['version']
    refs = {package_ref(package) for package in packages}
    if len(refs) != len(packages):
        raise ValueError('duplicate Cargo package identity')
    components = []
    dependencies = []
    for package in packages:
        component = {'type': 'library', 'bom-ref': package_ref(package), 'name': package['name'],
                     'version': package['version'],
                     'purl': f'pkg:cargo/{quote(package["name"])}@{quote(package["version"])}',
                     'properties': [{'name': 'plenora:cargo:source',
                                     'value': package.get('source', 'workspace')}]}
        if 'checksum' in package:
            component['hashes'] = [{'alg': 'SHA-256', 'content': package['checksum']}]
        if package['name'] == 'plenora-smb2':
            component['properties'].append({'name': 'plenora:vendored:provenance',
                                           'value': 'crates/plenora-smb2/PROVENANCE.md'})
            component['properties'].append({'name': 'plenora:vendored:provenance-sha256',
                                           'value': digest(ROOT / 'crates/plenora-smb2/PROVENANCE.md')})
        components.append(component)
        resolved = []
        for dependency in package.get('dependencies', []):
            fields = dependency.split(' ', 2)
            matches = [candidate for candidate in packages if candidate['name'] == fields[0]
                       and (len(fields) < 2 or candidate['version'] == fields[1])
                       and (len(fields) < 3 or candidate.get('source') == fields[2].strip('()'))]
            if len(matches) != 1:
                raise ValueError(f'ambiguous or missing Cargo dependency: {dependency}')
            resolved.append(package_ref(matches[0]))
        dependencies.append({'ref': package_ref(package), 'dependsOn': sorted(resolved)})
    artifact_refs = []
    for path in sorted(map(Path, artifacts), key=lambda p: p.name):
        ref = 'file:' + quote(path.name) + ':sha256:' + digest(path)
        artifact_refs.append(ref)
        components.append({'type': 'file', 'bom-ref': ref, 'name': path.name,
                           'hashes': [{'alg': 'SHA-256', 'content': digest(path)}]})
    root_ref = f'plenora:storage-tools:{version}:lockfile-inventory'
    dependencies.append({'ref': root_ref, 'dependsOn': sorted(refs | set(artifact_refs))})
    return {'bomFormat': 'CycloneDX', 'specVersion': '1.6', 'version': 1,
            'metadata': {'component': {'type': 'application', 'bom-ref': root_ref,
                                      'name': 'plenora-storage-tools', 'version': version},
                         'properties': [{'name': 'plenora:inventory:scope',
                                         'value': 'Cargo.lock union including dev, optional and all target dependencies; OS and build tools excluded'},
                                        {'name': 'plenora:lockfile:sha256', 'value': digest(ROOT / 'Cargo.lock')}]},
            'components': sorted(components, key=lambda value: value['bom-ref']),
            'dependencies': sorted(dependencies, key=lambda value: value['ref'])}


def verify(document, artifacts=()):
    if document != render(artifacts):
        raise ValueError('SBOM differs from Cargo.lock, provenance or artifact bytes')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if args.check:
        verify(json.loads(args.output.read_text()))
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(render(), indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
