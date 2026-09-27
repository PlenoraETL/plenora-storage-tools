"""Report Rust coverage by product crate, keeping the vendored SMB fork separate."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from check_test_layout import test_files

ROOT = Path(__file__).resolve().parents[1]


def apply_thresholds(report, policy):
    if set(report['crates']) != set(policy):
        raise ValueError('coverage must include every crate in the policy, with no unreviewed crates')
    results = {}
    for crate, threshold in policy.items():
        count = report['crates'][crate]
        if not 0 <= threshold <= 100 or count['lines'] <= 0:
            raise ValueError('invalid coverage threshold or empty crate')
        passed = 100 * count['covered'] >= threshold * count['lines']
        results[crate] = {'minimum_percent': threshold, 'status': 'PASS' if passed else 'FAIL'}
    report['thresholds'] = results
    report['threshold_status'] = 'PASS' if all(item['status'] == 'PASS' for item in results.values()) else 'FAIL'
    return report


def summarize(document):
    crates = {}
    seen = set()
    dedicated = {'crates/' + p.relative_to(ROOT / 'crates').as_posix() for p in test_files(ROOT)[1]}
    excluded = []
    for section in document['data']:
        for source in section['files']:
            path = source['filename'].replace('\\', '/')
            if '/crates/' not in path or '/src/' not in path:
                continue
            if path in seen:
                raise ValueError('duplicate coverage file')
            seen.add(path)
            relative = 'crates/' + path.split('/crates/', 1)[1]
            if relative in dedicated:
                excluded.append(relative)
                continue
            crate = path.split('/crates/', 1)[1].split('/', 1)[0]
            if not crate.startswith('plenora-'):
                continue
            entry = crates.setdefault(crate, {'lines': 0, 'covered': 0, 'files': 0})
            lines = source['summary']['lines']
            if not 0 <= lines['covered'] <= lines['count']:
                raise ValueError('invalid coverage counts')
            entry['lines'] += lines['count']
            entry['covered'] += lines['covered']
            entry['files'] += 1
    if not crates:
        raise ValueError('no product coverage records')
    for entry in crates.values():
        entry['percent'] = round(100 * entry['covered'] / entry['lines'], 2) if entry['lines'] else None
    product = [value for key, value in crates.items() if key != 'plenora-smb2']
    lines = sum(value['lines'] for value in product)
    covered = sum(value['covered'] for value in product)
    return {'schema_version': 1, 'crates': dict(sorted(crates.items())),
            'product_excluding_smb_fork': {'lines': lines, 'covered': covered,
                                         'percent': round(100 * covered / lines, 2) if lines else None},
            'excluded_test_files': sorted(excluded),
            'scope': 'Owned Rust production sources; cfg(test) child files excluded. SMB fork is separate and retains upstream inline tests. Python wrapper and interoperability are separate gates.',
            'threshold_status': 'baseline_measurement_only'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--policy', type=Path, help='Enforce per-crate floors from a reviewed policy')
    args = parser.parse_args()
    report = {'schema_version': 1, 'threshold_status': 'RUNNING'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report) + '\n')
    try:
        raw = args.input.read_bytes()
        report = summarize(json.loads(raw))
        report['raw_report_sha256'] = hashlib.sha256(raw).hexdigest()
        report['source_revision'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        report['dirty'] = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip())
        if args.policy:
            apply_thresholds(report, json.loads(args.policy.read_text())['rust_lines'])
    except BaseException as error:
        report.update(threshold_status='FAIL', failure_type=type(error).__name__)
        raise
    finally:
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    if report['threshold_status'] == 'FAIL':
        raise SystemExit('Rust coverage is below the per-crate release floor')
