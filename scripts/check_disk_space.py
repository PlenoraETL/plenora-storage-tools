"""Reserve filesystem headroom before transfer workloads; never clean data automatically."""
import argparse
import json
from pathlib import Path
import shutil
import tempfile

GIB = 1024**3


def requirement(total, payload_bytes, workers, *, spool_uploads=False):
    if total <= 0 or payload_bytes <= 0 or workers <= 0:
        raise ValueError('space inputs must be positive')
    # Reserve 10% (at least 8 GiB) independently of working data. Four payloads
    # cover input, staged source/destination and download for each worker.
    # Private-file publication also needs a complete local preparation file.
    return max(8 * GIB, (total + 9) // 10) + (5 if spool_uploads else 4) * payload_bytes * workers


def inspect(paths, payload_bytes, workers, *, spool_uploads=False):
    results = []
    for label, path in paths.items():
        usage = shutil.disk_usage(path)
        required = requirement(usage.total, payload_bytes, workers, spool_uploads=spool_uploads)
        results.append({'filesystem': label, 'total_bytes': usage.total, 'free_bytes': usage.free,
                        'required_bytes': required, 'status': 'PASS' if usage.free >= required else 'FAIL'})
    return {'schema_version': 1, 'status': 'PASS' if all(row['status'] == 'PASS' for row in results) else 'FAIL',
            'payload_bytes': payload_bytes, 'workers': workers, 'spool_uploads': spool_uploads, 'results': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=Path.cwd())
    parser.add_argument('--backend-data', type=Path, action='append', default=[])
    parser.add_argument('--bytes', type=int, default=GIB)
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--spool-uploads', action='store_true')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    paths = {'workspace': args.workspace, 'temporary': Path(tempfile.gettempdir())}
    paths.update({f'backend-{index}': path for index, path in enumerate(args.backend_data)})
    report = inspect(paths, args.bytes, args.workers, spool_uploads=args.spool_uploads)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    if report['status'] != 'PASS':
        raise SystemExit('insufficient qualification headroom; preserve evidence and free build cache explicitly')
    print('PASS qualification disk headroom')


if __name__ == '__main__':
    main()
