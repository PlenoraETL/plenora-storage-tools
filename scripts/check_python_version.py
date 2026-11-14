"""Check the single Python runtime policy and its release projections."""
import argparse
import ast
import hashlib
import strict_json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check(root=ROOT, runtime=False):
    version = (root / '.python-version').read_text().strip()
    errors = []
    if not re.fullmatch(r'3\.\d+', version):
        return ['.python-version: expected a Python minor version']
    tag = 'cp' + version.replace('.', '')
    project = tomllib.loads((root / 'crates/plenora-storage-py/pyproject.toml').read_text())
    if project['project']['requires-python'] != '>=' + version:
        errors.append('pyproject: Python minimum differs')
    cargo = tomllib.loads((root / 'crates/plenora-storage-py/Cargo.toml').read_text())
    if [f for f in cargo['dependencies']['pyo3']['features'] if f.startswith('abi3-py')] != ['abi3-py' + version.replace('.', '')]:
        errors.append('PyO3: ABI minimum differs')
    for path in (root / '.github/workflows').glob('*.yml'):
        text = path.read_text()
        if re.search(r'^\s+python-version:', text, re.M):
            errors.append(path.name + ': setup-python must read .python-version')
        if text.count('uses: actions/setup-python@') != text.count('python-version-file: .python-version'):
            errors.append(path.name + ': missing Python version source')
        for matrix in re.findall(r'^\s+python: (\[.*\])$', text, re.M):
            if ast.literal_eval(matrix) != [version]:
                errors.append(path.name + ': Python matrix differs')
    for name in ('scripts/package_files.json', 'scripts/package_generated.py', 'scripts/check_packages.py'):
        text = (root / name).read_text()
        if any(t != tag for t in re.findall(r'(cp\d+)-abi3', text)):
            errors.append(name + ': wheel ABI differs')
        if any(v != version for v in re.findall(r'evidence/gates/sdk/[^/]+/(3\.\d+)/', text)):
            errors.append(name + ': SDK evidence runtime differs')
    tree = ast.parse((root / 'scripts/release_evidence.py').read_text())
    versions = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == 'PYTHONS' for t in n.targets))
    if versions != (version,):
        errors.append('release evidence: Python versions differ')
    installer = (root / 'scripts/install_python.sh').read_text()
    if not re.search(r'^release=' + re.escape(version) + r'\.\d+$', installer, re.M):
        errors.append('container installer: Python version differs')
    for name in ('docker/extended/Dockerfile', 'docker/minio/Dockerfile'):
        if not re.search(r'FROM python:' + re.escape(version) + r'\.', (root / name).read_text()):
            errors.append(name + ': Python image differs')
    for name in ('Dockerfile', 'fuzz/Dockerfile'):
        text = (root / name).read_text()
        if 'scripts/install_python.sh' not in text or '.python-version' not in text:
            errors.append(name + ': missing Python installer or policy')
    history = strict_json.loads((root / 'scripts/python_historical_evidence.json').read_text(encoding='utf-8'))
    old = re.compile(r'(?:Python|CPython)\s*(?:>=\s*)?3\.(?:10|11|12|13)\b|cp31[0-3]-abi3|abi3-py31[0-3]')
    paths = [root / 'README.md', root / 'CHANGELOG.md', *root.glob('docs/**/*.md'),
             *root.glob('crates/plenora-storage-*/README.md'), *root.glob('scripts/*.py')]
    for path in paths:
        name = path.relative_to(root).as_posix()
        if name in history and 'sha256' in history[name]:
            if hashlib.sha256(path.read_bytes()).hexdigest() != history[name]['sha256']:
                errors.append(name + ': historical evidence changed')
            continue
        found = [line for line in path.read_text(encoding='utf-8').splitlines() if old.search(line)]
        if found != history.get(name, {}).get('lines', []):
            errors.append(name + ': obsolete Python policy reference')
    if runtime and '.'.join(map(str, sys.version_info[:2])) != version:
        errors.append('runtime: interpreter differs from .python-version')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', action='store_true')
    args = parser.parse_args()
    errors = check(runtime=args.runtime)
    if errors:
        raise SystemExit('\n'.join(errors))
    print('PASS Python policy, ABI, CI, release evidence and containers')


if __name__ == '__main__':
    main()
