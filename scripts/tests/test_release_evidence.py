"""Reject incomplete or substituted qualification, even when summaries say PASS."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_performance import compare
from release_evidence import (Evidence, ROOT, TARGETS, PYTHONS, PROVIDERS, BUFFERED,
                              FUZZ_TARGETS, packages, validate_bundle, validate_soak,
                              validate_transfers)
from summarize_coverage import summarize, apply_thresholds

REVISION = 'a' * 40
BINARY = 'b' * 64
WHEEL = 'c' * 64


def transfer(size=1024**2, workers=4, rounds=5):
    rows = []
    for round_ in range(rounds):
        for provider in PROVIDERS:
            bounded = provider in BUFFERED and size > 64 * 1024**2
            operations = (['test', 'put', 'put', 'get', 'delete', 'delete'] if bounded else
                          ['test', 'put', 'stat', 'copy', 'get', 'delete', 'delete'])
            if provider == 's3' and size > 64 * 1024**2:
                operations += ['put', 'stat']
            for _ in range(workers):
                rows.append(dict(provider=provider, round=round_, payload_bytes=size, status='PASS',
                                 mode='documented_limit_preserves_destination' if bounded else
                                 'buffered_roundtrip' if provider in BUFFERED else 'streaming_roundtrip',
                                 measurements=[dict(operation=op, status='PASS', peak_rss_bytes=1000,
                                                    elapsed_seconds=1.0) for op in operations]))
    return dict(status='PASS', binary_sha256=BINARY, platform='linux', payload_bytes=size,
                campaign_id=str(uuid.uuid4()),
                workers=workers, rounds=rounds, rss_limit_bytes=256 * 1024**2, results=rows,
                source_revision=REVISION, dirty=False,
                environment=dict(machine='x86_64', kernel='fixture', cpu_count=4,
                                 cpu_model='fixture', fixture_sha256='d' * 64))


def soak():
    resources = dict(rss_bytes=1000, threads=4, file_descriptors=10)
    return dict(status='PASS', wheel_sha256=WHEEL, version='1.0.0rc1', duration_seconds=7200,
                elapsed_seconds=7201, workers=4, completed_cycles=1, providers=list(PROVIDERS),
                after_provider={p: resources.copy() for p in PROVIDERS}, baseline=resources.copy(),
                peak=resources.copy(), latest=resources.copy())


class ReleaseEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.subjects = {t: dict(binary_sha256=BINARY, wheel_sha256=WHEEL) for t in TARGETS}
        self.policy = json.loads((ROOT / 'scripts/coverage-policy.json').read_text())

    def write(self, name, document):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = document if isinstance(document, bytes) else json.dumps(document).encode()
        path.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def bundle(self):
        for target in TARGETS:
            items = []
            for name, _ in packages():
                raw = (ROOT / 'api/rust' / target / (name + '.txt')).read_bytes()
                sha = self.write(f'api/{target}/{name}.txt', raw)
                items.append(dict(package=name, sha256=sha, matches=True, lines=len(raw.splitlines())))
            self.write(f'api/{target}/report.json', dict(status='PASS', target=target, features='all',
                       document_hidden_items=True, differences=[], packages=items,
                       source_revision=REVISION, source_dirty=False))
            for python in PYTHONS:
                folder = f'sdk/{target}/{python}'
                log = self.write(folder + '/sdk-tests.log', b'Ran 25 tests in 1.0s\n\nOK\n')
                counts = dict(num_statements=10, covered_lines=10, num_branches=2, covered_branches=2)
                modules = ['__init__.py', 'types.py']
                files = {'/installed/plenora_storage/' + name: {'summary': counts} for name in modules}
                raw = self.write(folder + '/coverage.json', dict(meta=dict(branch_coverage=True, version='7.16.1'), files=files))
                typing = dict(status='PASS', wheel_sha256=WHEEL, python=python + '.0', mypy='2.3.1',
                              platform='win32' if 'windows' in target else 'linux',
                              consumer_sha256=hashlib.sha256((ROOT / 'crates/plenora-storage-py/typing/consumer.py').read_bytes()).hexdigest(),
                              log_sha256=self.write(folder + '/sdk-typing.log', b'Success: no issues found in 1 source file\n'))
                self.write(folder + '/sdk-typing.json', typing)
                self.write(folder + '/sdk-tests.json', dict(status='PASS', wheel_sha256=WHEEL, python=python + '.0', typing=typing,
                           platform='win32' if 'windows' in target else 'linux', tests_passed=25, tests_log_sha256=log,
                           coverage=dict(branch=True, tool_version='7.16.1', report_sha256=raw,
                                         policy=self.policy['python'], files={'plenora_storage/' + name:
                                         dict(lines=10, covered_lines=10, branches=2, covered_branches=2,
                                              line_percent=100, branch_percent=100) for name in modules})))
        targets = {}
        for target in FUZZ_TARGETS:
            self.write(f'fuzz/{target}/run.log', b'#120 DONE cov: 54\nstat::number_of_executed_units: 120\n')
            seed = self.write(f'fuzz/{target}/corpus/fixture', b'fixture')
            targets[target] = dict(status='PASS', returncode=0, executions=120, coverage_edges=54,
                                   elapsed_seconds=61, seeds={'fixture': seed})
        self.write('fuzz/report.json', dict(status='PASS', source_commit=REVISION, dirty=False,
                   sanitizer='address', seconds_per_target=60, targets=targets,
                   lock_sha256=hashlib.sha256((ROOT / 'fuzz/Cargo.lock').read_bytes()).hexdigest()))
        raw = {'data': [{'files': [dict(filename=f'/workspace/crates/{crate}/src/lib.rs',
                                      summary={'lines': {'count': 100, 'covered': 100}})
                                  for crate in self.policy['rust_lines']]}]}
        digest = self.write('coverage/rust-coverage.json', raw)
        summary = apply_thresholds(summarize(raw), self.policy['rust_lines'])
        summary.update(source_revision=REVISION, dirty=False, raw_report_sha256=digest)
        self.write('coverage/coverage-summary.json', summary)
        self.write('transfers/large.json', transfer(1024**3, 1, 1))
        self.write('transfers/workers4.json', transfer(rounds=2))
        self.write('transfers/workers16.json', transfer(workers=16, rounds=1))
        self.write('soak/report.json', soak())
        baseline = self.write('performance/baseline.json', transfer())
        candidate = self.write('performance/candidate.json', transfer())
        report = compare(transfer(), transfer(), BINARY, json.loads((ROOT / 'scripts/performance-policy.json').read_text()))
        report.update(baseline_sha256=baseline, candidate_sha256=candidate)
        self.write('performance/report.json', report)

    def test_complete_bundle_records_raw_files_and_rejects_every_missing_gate(self):
        self.bundle()
        files = validate_bundle(self.root, REVISION, self.subjects, '1.0.0rc1')
        self.assertGreater(len(files), 60)
        for name in ['soak/report.json', 'transfers/workers16.json', 'fuzz/report.json',
                     'coverage/rust-coverage.json', 'performance/baseline.json',
                     f'api/{TARGETS[1]}/report.json', f'sdk/{TARGETS[1]}/3.14/sdk-tests.json']:
            with self.subTest(name=name):
                path = self.root / name
                raw = path.read_bytes()
                path.unlink()
                with self.assertRaises(ValueError):
                    validate_bundle(self.root, REVISION, self.subjects, '1.0.0rc1')
                path.write_bytes(raw)

    def test_pass_cannot_hide_stale_source_different_wheel_or_modified_raw_log(self):
        self.bundle()
        with self.assertRaises(ValueError):
            validate_bundle(self.root, 'e' * 40, self.subjects, '1.0.0rc1')
        wrong = deepcopy(self.subjects)
        wrong[TARGETS[1]]['wheel_sha256'] = 'f' * 64
        with self.assertRaises(ValueError):
            validate_bundle(self.root, REVISION, wrong, '1.0.0rc1')
        self.write(f'sdk/{TARGETS[0]}/3.10/sdk-tests.log', b'Ran 25 tests in 1.0s\nFAILED\n')
        with self.assertRaises(ValueError):
            validate_bundle(self.root, REVISION, self.subjects, '1.0.0rc1')

    def test_transfer_pass_cannot_hide_missing_provider_or_weakened_budget(self):
        for change in (lambda r: r['results'].pop(), lambda r: r.update(rss_limit_bytes=1024**3),
                       lambda r: r.update(binary_sha256='wrong'),
                       lambda r: r['results'][0]['measurements'].clear()):
            report = transfer()
            change(report)
            with self.assertRaises(ValueError):
                validate_transfers(report, BINARY, size=1024**2, workers=4, rounds=5)

    def test_soak_must_finish_on_exact_wheel_and_preserve_resource_limits(self):
        validate_soak(soak(), WHEEL, '1.0.0rc1')
        for change in (lambda r: r.update(status='RUNNING'), lambda r: r.update(elapsed_seconds=7199),
                       lambda r: r.update(wheel_sha256='wrong'), lambda r: r.update(completed_cycles=0),
                       lambda r: r['peak'].update(file_descriptors=100)):
            report = soak()
            change(report)
            with self.assertRaises(ValueError):
                validate_soak(report, WHEEL, '1.0.0rc1')

    def test_two_hours_are_required_for_every_version_stage(self):
        for version in ('1.0.0a1', '1.0.0b1', '1.0.0rc1', '1.0.0'):
            with self.subTest(version=version):
                report = soak()
                report.update(version=version, duration_seconds=7200, elapsed_seconds=7200)
                validate_soak(report, WHEEL, version)
                report.update(duration_seconds=7199, elapsed_seconds=7199)
                with self.assertRaises(ValueError):
                    validate_soak(report, WHEEL, version)

    def test_private_file_transfers_cannot_reuse_default_or_partial_evidence(self):
        report = transfer(1024**3, 1, 2)
        with self.assertRaises(ValueError):
            validate_transfers(report, BINARY, size=1024**3, workers=1, rounds=2, spool_uploads=True)
        report['spool_uploads'] = True
        # Merely relabeling the old rejection campaign must still fail.
        with self.assertRaises(ValueError):
            validate_transfers(report, BINARY, size=1024**3, workers=1, rounds=2, spool_uploads=True)
        for row in report['results']:
            if row['provider'] in BUFFERED:
                row['mode'] = 'private_file_roundtrip'
                row['measurements'].extend(dict(operation=op, status='PASS', peak_rss_bytes=1000,
                                                elapsed_seconds=1.0) for op in ('stat', 'copy'))
        validate_transfers(report, BINARY, size=1024**3, workers=1, rounds=2, spool_uploads=True)
        with self.assertRaises(ValueError):
            validate_transfers(report, BINARY, size=1024**3, workers=1, rounds=2)
        report['results'].pop()
        with self.assertRaises(ValueError):
            validate_transfers(report, BINARY, size=1024**3, workers=1, rounds=2, spool_uploads=True)

    def test_combined_soak_requires_both_modes_for_every_complete_cycle(self):
        report = soak()
        with self.assertRaises(ValueError):
            validate_soak(report, WHEEL, report['version'], both_upload_modes=True)
        report.update(upload_modes=['buffered', 'private_file'],
                      completed_cycles_by_mode={'buffered': 1, 'private_file': 1},
                      after_mode_provider={mode: deepcopy(report['after_provider']) for mode in ('buffered', 'private_file')})
        validate_soak(report, WHEEL, report['version'], both_upload_modes=True)
        for change in (lambda r: r['completed_cycles_by_mode'].update(private_file=0),
                       lambda r: r['after_mode_provider']['private_file'].pop('azure'),
                       lambda r: r.update(duration_seconds=7199, elapsed_seconds=7199)):
            bad = deepcopy(report)
            change(bad)
            with self.assertRaises(ValueError):
                validate_soak(bad, WHEEL, bad['version'], both_upload_modes=True)

    def test_performance_rejects_environment_changes_and_detects_regression(self):
        policy = json.loads((ROOT / 'scripts/performance-policy.json').read_text())
        candidate = transfer()
        with self.assertRaises(ValueError):
            compare(candidate, candidate, BINARY, policy)
        candidate['environment']['cpu_count'] = 8
        with self.assertRaises(ValueError):
            compare(transfer(), candidate, BINARY, policy)
        candidate = transfer()
        for row in candidate['results']:
            for item in row['measurements']:
                item['elapsed_seconds'] *= 2
        self.assertEqual(compare(transfer(), candidate, BINARY, policy)['status'], 'FAIL')

    def test_evidence_paths_cannot_leave_bundle(self):
        self.write('report.json', {})
        evidence = Evidence(self.root / 'nested')
        with self.assertRaises(ValueError):
            evidence.read('../report.json')

    def test_timing_floor_does_not_waive_material_latency_or_memory_regressions(self):
        policy = json.loads((ROOT / 'scripts/performance-policy.json').read_text())
        baseline, candidate = transfer(), transfer()
        for document, value in ((baseline, .003), (candidate, .005)):
            for row in document['results']:
                for measure in row['measurements']:
                    measure['elapsed_seconds'] = value
        self.assertEqual(compare(baseline, candidate, BINARY, policy)['status'], 'PASS')
        for row in candidate['results']:
            for measure in row['measurements']:
                measure['elapsed_seconds'] = .1
        self.assertEqual(compare(baseline, candidate, BINARY, policy)['status'], 'FAIL')
        for row in candidate['results']:
            for measure in row['measurements']:
                measure['elapsed_seconds'] = .003
                measure['peak_rss_bytes'] = 1200
        self.assertEqual(compare(baseline, candidate, BINARY, policy)['status'], 'FAIL')
