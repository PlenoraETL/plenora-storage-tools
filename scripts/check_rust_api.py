"""Inventory compiler-resolved Rust APIs and compare the committed, target-specific baseline."""
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / 'tools/api-inventory/Cargo.toml'
RUST_VERSION = '1.92.0'


def run(command, **kwargs):
    return subprocess.run(command, cwd=ROOT, check=True, text=True, **kwargs)


def differences(expected, actual):
    # Multisets matter: public-api may emit equally rendered items in different impls.
    if sorted(expected.splitlines()) == sorted(actual.splitlines()):
        return ''
    return ''.join(difflib.unified_diff(sorted(expected.splitlines(keepends=True)),
                                       sorted(actual.splitlines(keepends=True)),
                                       fromfile='baseline', tofile='current'))


def packages():
    workspace = tomllib.loads((ROOT / 'Cargo.toml').read_text())['workspace']
    result = []
    for member in workspace['members']:
        manifest = tomllib.loads((ROOT / member / 'Cargo.toml').read_text())
        if (ROOT / member / 'src/lib.rs').exists() and manifest['package']['name'] != 'plenora-storage-py':
            result.append((manifest['package']['name'], manifest.get('lib', {}).get('name',
                           manifest['package']['name'].replace('-', '_'))))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', action='store_true', help='Write a review candidate; never overwrite the baseline')
    parser.add_argument('--output', type=Path, default=ROOT / 'target/api-current')
    args = parser.parse_args()
    args.output = args.output.resolve()
    rustc = run(['rustc', '-vV'], capture_output=True).stdout
    metadata = dict(line.split(': ', 1) for line in rustc.splitlines() if ': ' in line)
    if metadata['release'] != RUST_VERSION:
        raise SystemExit(f'API snapshots require pinned rustc {RUST_VERSION}')
    target = metadata['host']
    baseline = ROOT / 'api/rust' / target
    destination = args.output / 'rust' / target
    destination.mkdir(parents=True, exist_ok=True)
    run(['cargo', 'build', '--locked', '--offline', '--manifest-path', str(TOOL)])
    tool_target = Path(os.environ.get('CARGO_TARGET_DIR', TOOL.parent / 'target')).resolve()
    executable = tool_target / 'debug' / ('storage-api-inventory.exe' if os.name == 'nt' else 'storage-api-inventory')
    # rustdoc JSON is unstable. Scope the opt-in to documentation subprocesses;
    # product builds and tests retain the normal stable toolchain settings.
    environment = dict(os.environ, RUSTC_BOOTSTRAP='1')
    cargo_target = Path(os.environ.get('CARGO_TARGET_DIR', ROOT / 'target')).resolve()
    results, failures = [], []
    names = set()
    for package, library in packages():
        run(['cargo', 'rustdoc', '--locked', '--offline', '-p', package, '--lib', '--all-features',
             '--', '-Z', 'unstable-options', '--output-format', 'json'], env=environment)
        document = cargo_target / 'doc' / (library + '.json')
        actual = run([str(executable), str(document)], capture_output=True).stdout
        name = package + '.txt'
        names.add(name)
        (destination / name).write_text(actual, encoding='utf-8', newline='\n')
        expected = baseline / name
        delta = differences(expected.read_text(encoding='utf-8'), actual) if expected.exists() else 'Missing baseline: ' + name
        if delta:
            failures.append(name)
            (destination / (name + '.diff')).write_text(delta, encoding='utf-8')
        results.append({'package': package, 'lines': len(actual.splitlines()),
                        'sha256': hashlib.sha256(actual.encode()).hexdigest(),
                        'matches': not bool(delta)})
    if {path.name for path in baseline.glob('*.txt')} != names:
        failures.append('package inventory differs')
    report = {'schema_version': 1, 'status': 'CANDIDATE' if args.candidate else 'FAIL' if failures else 'PASS',
              'rustc': metadata['release'], 'target': target, 'features': 'all',
              'source_revision': run(['git', 'rev-parse', 'HEAD'], capture_output=True).stdout.strip(),
              'source_dirty': bool(run(['git', 'status', '--porcelain'], capture_output=True).stdout.strip()),
              'packages': results, 'differences': failures}
    (destination / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    if failures and not args.candidate:
        raise SystemExit('Rust API differs; inspect target/api-current before proposing a baseline change')


if __name__ == '__main__':
    main()
