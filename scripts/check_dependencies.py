"""Enforce exact dependency pins: Cargo manifests with reviewed public-boundary
exceptions, Python requirement files and the SDK build backend."""
import json
from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[1]
EXACT = re.compile(r'=\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?')
COMPATIBLE = re.compile(r'\d+\.\d+\.\d+')
PYTHON_PIN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*==[0-9][0-9A-Za-z.+!-]*')


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


def check_python_pins(name, requirements):
    """Every entry is an exact `name==version`; transitive dependencies are
    listed explicitly, since pip resolves anything unlisted at install time."""
    return [f'{name}: {requirement!r}: exact version pin required'
            for requirement in requirements if not PYTHON_PIN.fullmatch(requirement)]


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
    for path in sorted((ROOT / 'scripts').glob('requirements-*.txt')):
        lines = [line.strip() for line in path.read_text().splitlines()]
        failures.extend(check_python_pins(path.relative_to(ROOT).as_posix(), [line for line in lines if line]))
    pyproject = tomllib.loads((ROOT / 'crates/plenora-storage-py/pyproject.toml').read_text())
    failures.extend(check_python_pins('crates/plenora-storage-py/pyproject.toml build-system',
                                      pyproject['build-system']['requires']))
    if failures:
        raise SystemExit('\n'.join(failures))
    print('PASS dependency pins, Python requirements and reviewed public API exceptions')


if __name__ == '__main__':
    main()
