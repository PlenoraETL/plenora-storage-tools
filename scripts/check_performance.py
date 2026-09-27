"""Compare complete fixture transfer campaigns from the same measured environment."""
import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
from statistics import median
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]


def compare(baseline, candidate, binary, policy):
    # Import here: release_evidence also uses this comparator when sealing.
    from release_evidence import require, validate_transfers
    for report in (baseline, candidate):
        require(str(UUID(report['campaign_id'])) == report['campaign_id'], 'invalid performance campaign identity')
        validate_transfers(report, report['binary_sha256'], size=1024**2, workers=4,
                           rounds=policy['minimum_rounds'])
        require(report['dirty'] is False and len(report['source_revision']) == 40,
                'performance campaigns require clean identified source')
    require(candidate['binary_sha256'] == binary, 'performance used another candidate binary')
    require(baseline['campaign_id'] != candidate['campaign_id'], 'performance baseline and candidate must be separate campaigns')
    require(baseline['environment'] == candidate['environment'] and candidate['environment'],
            'performance environments differ')
    require(set(candidate['environment']) == {'machine', 'kernel', 'cpu_count', 'cpu_model', 'fixture_sha256'},
            'performance environment is incomplete')
    require(baseline['rounds'] == candidate['rounds'], 'performance sample counts differ')

    def samples(report):
        result = defaultdict(list)
        for row in report['results']:
            for measure in row['measurements']:
                for metric in ('elapsed_seconds', 'peak_rss_bytes'):
                    value = measure[metric]
                    require(math.isfinite(value) and value > 0, 'invalid performance sample')
                    result[(row['provider'], measure['operation'], metric)].append(value)
        return result

    old, new = samples(baseline), samples(candidate)
    require(old.keys() == new.keys(), 'performance operation inventory differs')
    results = []
    for key in sorted(old):
        require(len(old[key]) == len(new[key]) and len(new[key]) >= policy['minimum_rounds'],
                'performance samples are incomplete')
        for statistic in (('median', 'p95') if key[2] == 'elapsed_seconds' else ('maximum',)):
            def measure(values):
                return (median(values) if statistic == 'median' else max(values) if statistic == 'maximum'
                        else sorted(values)[math.ceil(len(values) * .95) - 1])
            previous, current = measure(old[key]), measure(new[key])
            limit = policy['maximum_regression_percent'][statistic]
            delta = (current / previous - 1) * 100
            absolute = (policy['minimum_timing_budget_seconds'][statistic]
                        if key[2] == 'elapsed_seconds' else 0)
            require(math.isfinite(absolute) and absolute >= 0, 'invalid absolute performance budget')
            allowed = previous + max(previous * limit / 100, absolute)
            results.append(dict(provider=key[0], operation=key[1], metric=key[2], statistic=statistic,
                                baseline=previous, candidate=current, regression_percent=delta,
                                allowed_maximum=allowed,
                                status='PASS' if current <= allowed else 'FAIL'))
    return {'status': 'PASS' if all(r['status'] == 'PASS' for r in results) else 'FAIL',
            'binary_sha256': binary, 'policy': policy, 'results': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baseline', type=Path)
    parser.add_argument('candidate', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text('{"status": "RUNNING"}\n')
    report = {'status': 'FAIL'}
    try:
        baseline, candidate = (json.loads(path.read_bytes()) for path in (args.baseline, args.candidate))
        policy = json.loads((ROOT / 'scripts/performance-policy.json').read_text())
        report = compare(baseline, candidate, candidate['binary_sha256'], policy)
        report['baseline_sha256'] = hashlib.sha256(args.baseline.read_bytes()).hexdigest()
        report['candidate_sha256'] = hashlib.sha256(args.candidate.read_bytes()).hexdigest()
    finally:
        args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    if report['status'] != 'PASS':
        raise SystemExit('performance regression budget exceeded')


if __name__ == '__main__':
    main()
