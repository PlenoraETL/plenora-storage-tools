"""Deterministic CycloneDX 1.6 inventory of the complete Cargo.lock graph.

Scope includes dev, optional and target-specific packages, not a claim that every
package is linked in every binary. Python has no third-party runtime dependency.
OS packages and build tools are outside this inventory.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import tomllib
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def package_ref(package):
    source = hashlib.sha256(package.get('source', 'workspace').encode()).hexdigest()[:16]
    return f'cargo:{package["name"]}@{package["version"]}:{source}'


def with_serial_number(document):
    """Adds the CycloneDX `serialNumber` that SBOM attestation requires.

    The URN is a UUID version 8 derived from the SHA-256 of the canonical
    document without it: the same inventory always gets the same serial, and
    any change to components, dependencies or artifact digests gets a new one.
    Nothing random or time-dependent enters the inventory.
    """
    canonical = json.dumps(document, sort_keys=True, separators=(',', ':')).encode()
    raw = bytearray(hashlib.sha256(canonical).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x80
    raw[8] = (raw[8] & 0x3F) | 0x80
    value = raw.hex()
    uuid = f'{value[:8]}-{value[8:12]}-{value[12:16]}-{value[16:20]}-{value[20:]}'
    return {'bomFormat': document['bomFormat'], 'specVersion': document['specVersion'],
            'serialNumber': 'urn:uuid:' + uuid,
            **{key: value for key, value in document.items() if key not in {'bomFormat', 'specVersion'}}}


def render(artifacts=(), *, lockfile='Cargo.lock'):
    workspace = tomllib.loads((ROOT / 'Cargo.toml').read_text())['workspace']
    packages = tomllib.loads((ROOT / lockfile).read_text())['package']
    version = workspace['package']['version']
    def reference(package):
        prefix = '' if lockfile == 'Cargo.lock' else quote(lockfile, safe='') + ':'
        return prefix + package_ref(package)
    refs = {reference(package) for package in packages}
    if len(refs) != len(packages):
        raise ValueError('duplicate Cargo package identity')
    components = []
    dependencies = []
    for package in packages:
        component = {'type': 'library', 'bom-ref': reference(package), 'name': package['name'],
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
            resolved.append(reference(matches[0]))
        dependencies.append({'ref': reference(package), 'dependsOn': sorted(resolved)})
    artifact_refs = []
    for path in sorted(map(Path, artifacts), key=lambda p: p.name):
        ref = 'file:' + quote(path.name) + ':sha256:' + digest(path)
        artifact_refs.append(ref)
        components.append({'type': 'file', 'bom-ref': ref, 'name': path.name,
                           'hashes': [{'alg': 'SHA-256', 'content': digest(path)}]})
    root_ref = f'plenora:storage-tools:{version}:lockfile-inventory'
    if lockfile != 'Cargo.lock':
        root_ref += ':' + quote(lockfile, safe='')
    dependencies.append({'ref': root_ref, 'dependsOn': sorted(refs | set(artifact_refs))})
    return with_serial_number({'bomFormat': 'CycloneDX', 'specVersion': '1.6', 'version': 1,
            'metadata': {'component': {'type': 'application', 'bom-ref': root_ref,
                                      'name': 'plenora-storage-tools', 'version': version},
                         'properties': [{'name': 'plenora:inventory:scope',
                                         'value': f'{lockfile} union including dev, optional and all target dependencies; OS and externally installed build tools excluded'},
                                        {'name': 'plenora:lockfile:path', 'value': lockfile},
                                        {'name': 'plenora:lockfile:sha256', 'value': digest(ROOT / lockfile)}]},
            'components': sorted(components, key=lambda value: value['bom-ref']),
            'dependencies': sorted(dependencies, key=lambda value: value['ref'])})


def verify(document, artifacts=()):
    if document != render(artifacts):
        raise ValueError('SBOM differs from Cargo.lock, provenance or artifact bytes')


def render_qualification():
    """Inventory auxiliary lock graphs and declared Python qualification pins.

    Separate graph identities prevent differing resolutions of one package from
    being merged. Python declarations are not an installed environment or a
    resolved transitive dependency graph; no dependency edges are invented.
    """
    version = tomllib.loads((ROOT / 'Cargo.toml').read_text())['workspace']['package']['version']
    root_ref = f'plenora:storage-tools:{version}:qualification-inventory'
    components, dependencies, roots, properties = [], [], [], []
    for lockfile in ('fuzz/Cargo.lock', 'tools/api-inventory/Cargo.lock'):
        document = render(lockfile=lockfile)
        component = document['metadata']['component']
        component['name'] = lockfile.removesuffix('/Cargo.lock')
        component['properties'] = document['metadata']['properties']
        components.extend([component, *document['components']])
        dependencies.extend(document['dependencies'])
        roots.append(component['bom-ref'])
    for path in sorted((ROOT / 'scripts').glob('requirements-*.txt')):
        relative = path.relative_to(ROOT).as_posix()
        properties.append({'name': 'plenora:declaration:sha256:' + relative, 'value': digest(path)})
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.!+_-]*)', line)
            if not match:
                raise ValueError('qualification requirements must use explicit version pins')
            name, pinned = match.groups()
            name = re.sub(r'[-_.]+', '-', name).lower()
            ref = f'python-declaration:{quote(relative, safe="")}:{name}@{pinned}'
            roots.append(ref)
            components.append({'type': 'library', 'bom-ref': ref, 'name': name, 'version': pinned,
                               'purl': f'pkg:pypi/{quote(name)}@{quote(pinned)}',
                               'properties': [{'name': 'plenora:inventory:scope', 'value': 'declared qualification dependency'},
                                              {'name': 'plenora:declaration:path', 'value': relative}]})
    if len({c['bom-ref'] for c in components}) != len(components):
        raise ValueError('duplicate qualification dependency identity')
    dependencies.append({'ref': root_ref, 'dependsOn': sorted(roots)})
    return with_serial_number({'bomFormat': 'CycloneDX', 'specVersion': '1.6', 'version': 1,
            'metadata': {'component': {'type': 'application', 'bom-ref': root_ref,
                                      'name': 'plenora-storage-qualification', 'version': version},
                         'properties': [{'name': 'plenora:inventory:scope',
                                         'value': 'Auxiliary Cargo lock graphs and declared Python pins; Python transitives, installed environments, OS, native system libraries and externally installed Rust tools excluded'},
                                        *properties]},
            'components': sorted(components, key=lambda value: value['bom-ref']),
            'dependencies': sorted(dependencies, key=lambda value: value['ref'])})


def verify_qualification(document):
    if document != render_qualification():
        raise ValueError('qualification SBOM differs from auxiliary locks or Python declarations')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--qualification', action='store_true')
    args = parser.parse_args()
    if args.check:
        (verify_qualification if args.qualification else verify)(json.loads(args.output.read_text()))
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        document = render_qualification() if args.qualification else render()
        args.output.write_text(json.dumps(document, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
