"""Enforce exact direct dependency pins with reviewed public-boundary exceptions."""
import json
from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[1]
EXACT = re.compile(r'=\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?')
COMPATIBLE = re.compile(r'\d+\.\d+\.\d+')


def dependencies(document, prefix=''):
    for section, value in document.items():
        if not isinstance(value, dict):
            continue
        if section in {'dependencies', 'dev-dependencies', 'build-dependencies'}:
            for name, spec in value.items():
                yield prefix + section, name, spec
        elif section in {'workspace', 'target'} or prefix.startswith('target.'):
            yield from dependencies(value, prefix + section + '.')


def check_manifest(document, *, workspace=False, exceptions=()):
    errors = []
    for section, name, spec in dependencies(document):
        if isinstance(spec, dict) and (spec.get('workspace') or 'path' in spec):
            continue
        version = spec if isinstance(spec, str) else spec.get('version', '')
        if EXACT.fullmatch(version):
            continue
        if workspace and section == 'workspace.dependencies' and name in exceptions and COMPATIBLE.fullmatch(version):
            continue
        errors.append(f'{section}.{name}: exact version pin required')
    return errors


def main():
    policy = json.loads((ROOT / 'scripts/dependency-policy.json').read_text())
    exceptions = policy['compatible_workspace_requirements']
    workspace = tomllib.loads((ROOT / 'Cargo.toml').read_text())
    if not all(isinstance(value, str) and value.strip() for value in exceptions.values()):
        raise ValueError('dependency exceptions require a reason')
    if not set(exceptions) <= set(workspace['workspace']['dependencies']):
        raise ValueError('stale dependency exception')
    manifests = [ROOT / 'Cargo.toml', *sorted((ROOT / 'crates').glob('*/Cargo.toml')),
                 ROOT / 'fuzz/Cargo.toml', ROOT / 'tools/api-inventory/Cargo.toml']
    failures = []
    for path in manifests:
        if path.relative_to(ROOT).as_posix() == policy['excluded_manifest']:
            continue
        failures.extend(f'{path.relative_to(ROOT)}: {error}' for error in check_manifest(
            tomllib.loads(path.read_text()), workspace=path == ROOT / 'Cargo.toml', exceptions=exceptions))
    if failures:
        raise SystemExit('\n'.join(failures))
    print('PASS direct dependency pins and reviewed public API exceptions')


if __name__ == '__main__':
    main()
