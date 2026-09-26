"""Guard declared features, minimum runtimes and versioned JSON contracts."""
import argparse
import hashlib
import json
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def snapshot():
    workspace = tomllib.loads((ROOT / 'Cargo.toml').read_text())['workspace']
    packages = {}
    for member in workspace['members']:
        manifest = tomllib.loads((ROOT / member / 'Cargo.toml').read_text())
        packages[manifest['package']['name']] = {
            'features': manifest.get('features', {}),
            'lib': manifest.get('lib', {}),
            'rust_version': manifest['package'].get('rust-version', workspace['package']['rust-version']),
        }
    contracts = {}
    for path in sorted((ROOT / 'contracts').rglob('*.schema.json')):
        content = json.dumps(json.loads(path.read_text()), sort_keys=True, separators=(',', ':')).encode()
        contracts[path.relative_to(ROOT).as_posix()] = hashlib.sha256(content).hexdigest()
    return {'schema_version': 1, 'rust_version': workspace['package']['rust-version'],
            'edition': workspace['package']['edition'], 'packages': packages, 'contracts': contracts,
            'python_requires': tomllib.loads((ROOT / 'crates/plenora-storage-py/pyproject.toml').read_text())['project']['requires-python'],
            'upstream_contracts': json.loads((ROOT / 'contracts/upstream/source.json').read_text())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path)
    args = parser.parse_args()
    actual = snapshot()
    if args.candidate:
        args.candidate.parent.mkdir(parents=True, exist_ok=True)
        args.candidate.write_text(json.dumps(actual, indent=2, sort_keys=True) + '\n')
        print('CANDIDATE API requirements and contracts')
    else:
        expected = json.loads((ROOT / 'api/metadata.json').read_text())
        if actual != expected:
            raise SystemExit('API requirements or contracts differ from api/metadata.json')
        print('PASS API requirements and contracts')


if __name__ == '__main__':
    main()
