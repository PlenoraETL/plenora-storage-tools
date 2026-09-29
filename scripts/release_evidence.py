"""Validate the additional 1.0 gates against the exact release subjects.

Reports are evidence, not signatures. They must come from trusted qualification
runs. Recompute counters and policy decisions from their attached raw files;
never trust a summary's PASS alone or substitute another build of the same SHA.
"""
from collections import Counter
import hashlib
import json
import re
from pathlib import Path

from check_installed_sdk import enforce_coverage, test_count
from check_rust_api import packages
from check_test_layout import check as check_test_layout
from check_performance import compare
from soak_policy import SOAK_DURATION_SECONDS
from fixture_connections import BUFFERED, PROVIDERS
from fuzz_parsers import TARGETS as FUZZ_TARGETS, stats
from summarize_coverage import apply_thresholds, summarize
from scan_artifacts import validate as validate_native_inventory
from qualify_s3_disk_pressure import validate as validate_disk_pressure

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ('x86_64-unknown-linux-gnu', 'x86_64-pc-windows-msvc')
PYTHONS = ('3.10', '3.11', '3.12', '3.13', '3.14')


def require(condition, message):
    if not condition:
        raise ValueError(message)


class Evidence:
    """Keep every consumed file inside the bundle and record its identity."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.files = {}

    def read(self, name, sha256=None):
        path = (self.root / name).resolve()
        require(path.is_relative_to(self.root) and path.is_file(), 'missing or escaped evidence: ' + str(name))
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        require(sha256 is None or sha256 == digest, 'evidence digest differs: ' + str(name))
        self.files[path.relative_to(self.root).as_posix()] = digest
        return raw

    def json(self, name):
        return json.loads(self.read(name))


def clean_source(report, revision, revision_key='source_revision', dirty_key='dirty'):
    require(report[revision_key] == revision and report[dirty_key] is False,
            'evidence must describe the final clean source revision')


def validate_transfers(report, binary, *, size, workers, rounds, spool_uploads=False):
    require(report.get('spool_uploads', False) is spool_uploads, 'transfer upload strategy differs')
    require(report['status'] == 'PASS' and report['binary_sha256'] == binary,
            'transfer evidence failed or describes another binary')
    require(report['platform'] == 'linux' and report['payload_bytes'] == size
            and report['workers'] == workers and report['rounds'] >= rounds,
            'transfer campaign does not meet the required size/concurrency')
    require(0 < report['rss_limit_bytes'] <= 256 * 1024**2, 'transfer memory policy was weakened')
    expected = Counter({(p, r): workers for p in PROVIDERS for r in range(report['rounds'])})
    actual = Counter((row['provider'], row['round']) for row in report['results'])
    require(actual == expected, 'transfer campaign omitted or duplicated provider rounds')
    for row in report['results']:
        bounded = not spool_uploads and row['provider'] in BUFFERED and size > 64 * 1024**2
        mode = ('documented_limit_preserves_destination' if bounded else
                'buffered_roundtrip' if row['provider'] in BUFFERED else 'streaming_roundtrip')
        if spool_uploads and row['provider'] in BUFFERED:
            mode = 'private_file_roundtrip'
        require(row['status'] == 'PASS' and row['payload_bytes'] == size and row['mode'] == mode,
                'transfer outcome differs from the declared provider guarantee')
        operations = Counter(m['operation'] for m in row['measurements'])
        minimum = {'test': 1, 'put': 2 if bounded else 1, 'get': 1, 'delete': 2}
        if not bounded:
            minimum.update(stat=1, copy=1)
        if row['provider'] == 's3' and size > 64 * 1024**2:
            minimum.update(put=2, stat=2)
        require(all(operations[k] >= v for k, v in minimum.items()), 'transfer measurements are incomplete')
        for measure in row['measurements']:
            require(measure['status'] == 'PASS'
                    and 0 < measure['peak_rss_bytes'] <= report['rss_limit_bytes']
                    and measure['elapsed_seconds'] > 0, 'invalid transfer measurement')


def validate_soak(report, wheel, version, minimum_seconds=SOAK_DURATION_SECONDS, *, both_upload_modes=False):
    require(report['status'] == 'PASS' and report['wheel_sha256'] == wheel
            and report['version'] == version, 'soak failed or describes another wheel')
    require(report['duration_seconds'] >= minimum_seconds
            and report['elapsed_seconds'] >= report['duration_seconds']
            and report['workers'] >= 4 and report['completed_cycles'] > 0,
            'soak is incomplete')
    require(len(report['providers']) == len(PROVIDERS) and set(report['providers']) == set(PROVIDERS)
            and set(report['after_provider']) == set(PROVIDERS), 'soak provider matrix is incomplete')
    if both_upload_modes:
        modes = {'buffered', 'private_file'}
        require(report.get('upload_modes') == ['buffered', 'private_file']
                and set(report.get('completed_cycles_by_mode', {})) == modes
                and set(report.get('after_mode_provider', {})) == modes, 'soak upload strategy matrix is incomplete')
        for mode in modes:
            require(report['completed_cycles_by_mode'][mode] == report['completed_cycles']
                    and set(report['after_mode_provider'][mode]) == set(PROVIDERS),
                    'soak omitted cycles or providers in one upload strategy')
    for key, allowance in [('rss_bytes', 128 * 1024**2), ('threads', 16), ('file_descriptors', 16)]:
        baseline, peak, latest = (report[name][key] for name in ('baseline', 'peak', 'latest'))
        require(0 < baseline <= peak <= baseline + allowance and 0 < latest <= peak,
                'soak resource budget was exceeded')


def validate_bundle(root, revision, subjects, python_version, minimum_soak_seconds=SOAK_DURATION_SECONDS):
    """Return a file inventory only after all mandatory reports were checked.

subjects maps target triples to binary_sha256 and wheel_sha256 from manifests
already checked by verify_release.py. No caller-supplied waiver is accepted.
"""
    evidence = Evidence(root)
    version_core = tuple(map(int, re.match(r'(\d+)\.(\d+)\.(\d+)', python_version).groups()))
    if version_core >= (2, 0, 1):
        validate_disk_pressure(evidence.json('disk-pressure/report.json'), revision, subjects[TARGETS[0]]['binary_sha256'])
    if int(python_version.split('.', 1)[0]) >= 2:
        for target in TARGETS:
            validate_native_inventory(evidence, 'native-components/' + target, revision, subjects[target])
    check_test_layout(ROOT)
    policy = json.loads((ROOT / 'scripts/coverage-policy.json').read_text())
    for target in TARGETS:
        prefix = f'api/{target}'
        report = evidence.json(prefix + '/report.json')
        clean_source(report, revision, dirty_key='source_dirty')
        require(report['status'] == 'PASS' and report['target'] == target
                and report['features'] == 'all' and report['document_hidden_items'] is True
                and not report['differences'], 'API qualification failed')
        expected = {name for name, _ in packages()}
        require(len(report['packages']) == len(expected)
                and {p['package'] for p in report['packages']} == expected, 'API inventory is incomplete')
        for package in report['packages']:
            require(package['matches'] is True and package['lines'] > 0, 'API differs from baseline')
            name = package['package'] + '.txt'
            raw = evidence.read(prefix + '/' + name, package['sha256'])
            baseline = (ROOT / 'api/rust' / target / name).read_text(encoding='utf-8')
            require(sorted(raw.decode().splitlines()) == sorted(baseline.splitlines()), 'API baseline differs')

        for python in PYTHONS:
            folder = f'sdk/{target}/{python}'
            sdk = evidence.json(folder + '/sdk-tests.json')
            require(sdk['status'] == 'PASS' and sdk['wheel_sha256'] == subjects[target]['wheel_sha256']
                    and '.'.join(sdk['python'].split('.')[:2]) == python
                    and sdk['platform'] == ('win32' if 'windows' in target else 'linux'),
                    'SDK matrix contains a failed, misplaced or different wheel')
            log = evidence.read(folder + '/sdk-tests.log', sdk['tests_log_sha256']).decode('utf-8')
            require(test_count(log, 0) == sdk['tests_passed'], 'SDK test count differs')
            coverage = sdk['coverage']
            raw = evidence.json(folder + '/coverage.json')
            evidence.read(folder + '/coverage.json', coverage['report_sha256'])
            require(raw['meta']['branch_coverage'] is True and coverage['branch'] is True
                    and raw['meta']['version'] == coverage['tool_version'] == '7.16.1',
                    'SDK branch coverage is missing')
            measured = {}
            for path, entry in raw['files'].items():
                public = 'plenora_storage/' + path.replace('\\', '/').rsplit('/plenora_storage/', 1)[-1]
                count = entry['summary']
                lines, covered = count['num_statements'], count['covered_lines']
                branches, reached = count['num_branches'], count['covered_branches']
                require(public not in measured and lines > 0 and 0 <= covered <= lines
                        and 0 <= reached <= branches, 'invalid SDK coverage counters')
                measured[public] = dict(lines=lines, covered_lines=covered, branches=branches,
                                        covered_branches=reached, line_percent=covered * 100 / lines,
                                        branch_percent=reached * 100 / branches if branches else 100)
            require(measured and measured == coverage['files'] and coverage['policy'] == policy['python'],
                    'SDK coverage summary or policy differs')
            expected_files = {'plenora_storage/' + p.relative_to(ROOT / 'crates/plenora-storage-py/python/plenora_storage').as_posix()
                              for p in (ROOT / 'crates/plenora-storage-py/python/plenora_storage').rglob('*.py')}
            require(set(measured) == expected_files, 'SDK coverage omitted public modules')
            enforce_coverage(measured, policy['python'])
            typing = evidence.json(folder + '/sdk-typing.json')
            require(typing == sdk['typing'] and typing['status'] == 'PASS' and typing['mypy'] == '2.3.1'
                    and typing['wheel_sha256'] == sdk['wheel_sha256']
                    and typing['python'] == sdk['python'] and typing['platform'] == sdk['platform']
                    and typing['consumer_sha256'] == hashlib.sha256(
                        (ROOT / 'crates/plenora-storage-py/typing/consumer.py').read_bytes()).hexdigest(),
                    'SDK static consumer differs or failed')
            typing_log = evidence.read(folder + '/sdk-typing.log', typing['log_sha256']).decode('utf-8')
            require('Success: no issues found' in typing_log and ' error:' not in typing_log,
                    'SDK typing did not complete successfully')

    fuzz = evidence.json('fuzz/report.json')
    clean_source(fuzz, revision, 'source_commit')
    require(fuzz['status'] == 'PASS' and fuzz['sanitizer'] == 'address'
            and fuzz['seconds_per_target'] >= 60 and set(fuzz['targets']) == set(FUZZ_TARGETS),
            'parser fuzz campaign is incomplete')
    require(fuzz['lock_sha256'] == hashlib.sha256((ROOT / 'fuzz/Cargo.lock').read_bytes()).hexdigest(),
            'fuzz used a different dependency lock')
    for target, entry in fuzz['targets'].items():
        log = evidence.read(f'fuzz/{target}/run.log').decode('utf-8')
        counts = stats(log, entry['returncode'])
        require(counts['status'] == 'PASS' and all(entry[k] == v for k, v in counts.items()),
                'fuzz counters differ or campaign failed')
        require(entry['elapsed_seconds'] >= fuzz['seconds_per_target'], 'fuzz campaign ended early')
        require(bool(entry['seeds']), 'fuzz seed inventory is empty')
        for name, digest in entry['seeds'].items():
            evidence.read(f'fuzz/{target}/corpus/{name}', digest)

    coverage = evidence.json('coverage/coverage-summary.json')
    clean_source(coverage, revision)
    raw = evidence.read('coverage/rust-coverage.json', coverage['raw_report_sha256'])
    actual = apply_thresholds(summarize(json.loads(raw)), policy['rust_lines'])
    require(actual['threshold_status'] == coverage['threshold_status'] == 'PASS'
            and all(coverage[key] == actual[key] for key in ('crates', 'thresholds', 'product_excluding_smb_fork', 'excluded_test_files')),
            'Rust coverage failed or differs from raw counters')

    linux = subjects[TARGETS[0]]
    for name, size, workers, rounds in [('large', 1024**3, 1, 1), ('workers4', 1024**2, 4, 2),
                                       ('workers16', 1024**2, 16, 1)]:
        validate_transfers(evidence.json(f'transfers/{name}.json'), linux['binary_sha256'],
                           size=size, workers=workers, rounds=rounds)
        if version_core >= (2, 1, 0):
            report = evidence.json(f'transfers-spooled/{name}.json')
            clean_source(report, revision)
            validate_transfers(report, linux['binary_sha256'], size=size, workers=workers,
                               rounds=max(rounds, 2) if name == 'large' else rounds, spool_uploads=True)
    validate_soak(evidence.json('soak/report.json'), linux['wheel_sha256'], python_version, minimum_soak_seconds,
                  both_upload_modes=version_core >= (2, 1, 0))
    performance = evidence.json('performance/report.json')
    baseline = evidence.json('performance/baseline.json')
    candidate = evidence.json('performance/candidate.json')
    evidence.read('performance/baseline.json', performance['baseline_sha256'])
    evidence.read('performance/candidate.json', performance['candidate_sha256'])
    clean_source(candidate, revision)
    performance_policy = json.loads((ROOT / 'scripts/performance-policy.json').read_text())
    actual = compare(baseline, candidate, linux['binary_sha256'], performance_policy)
    require(actual['status'] == 'PASS' and all(performance[key] == value for key, value in actual.items()),
            'performance report differs or budget failed')
    return [{'name': name, 'sha256': digest} for name, digest in sorted(evidence.files.items())]
