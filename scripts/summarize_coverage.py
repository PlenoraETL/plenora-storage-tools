"""Report Rust coverage by product crate, keeping the vendored SMB fork separate."""
import argparse
import json
from pathlib import Path


def summarize(document):
    crates = {}
    seen = set()
    for section in document['data']:
        for source in section['files']:
            path = source['filename'].replace('\\', '/')
            if '/crates/' not in path or '/src/' not in path:
                continue
            if path in seen:
                raise ValueError('duplicate coverage file')
            seen.add(path)
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
            'scope': 'Rust source including inline test modules; Python wrapper and server interoperability are separate gates',
            'threshold_status': 'baseline_measurement_only'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(summarize(json.loads(args.input.read_text())), indent=2) + '\n')
