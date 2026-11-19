"""Sempl-1 test-only catalog, receipts and disposable mutation executor.

Never invokes Rust, GitHub, Docker, or a release entrypoint. Mutated sources
exist only in a TemporaryDirectory under target, not in the working sources.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from filesystem_paths import canonical_path
IMPORT_INPUTS = ('Cargo.toml', '.python-version', 'fuzz/Cargo.toml')
COPY_DIRS = ('scripts', 'docker', '.github/workflows')

# Identity/input catalog; outcomes are observed by unittest, not this table.
# Mutation text is an injection recipe, never a source-text acceptance guard.
CASES = [
    ('test_frozen_compression.CompressionTests.test_zip_library_decode_errors_use_the_same_boundary',
     'check_packages.py', 'payload = decode_compressed(archive.read, item)', 'payload = archive.read(item)', False),
    ('test_frozen_compression.CompressionTests.test_cli_deflate_date_bytes_preserve_streams_and_native_ranges',
     'check_packages.py', '    check_content(data[cursor:])', '    pass  # omitted trailing native range scan', False),
    ('test_frozen_compression.CompressionTests.test_mtime_bytes_and_corrupt_streams',
     'package_classifier.py', "raise ValueError('invalid compression stream') from None", 'raise', False),
    ('test_frozen_compression.CompressionTests.test_zip_deflate_uses_same_failure_boundary',
     'package_classifier.py', 'payload = decode_compressed(decoder.decompress, compressed)', 'payload = decoder.decompress(compressed)', False),
    ('test_frozen_compression.CompressionTests.test_archive_fixtures_do_not_read_wall_clock',
     'tests/test_packages.py', 'partial(gzip.compress, mtime=0)', 'partial(gzip.compress, mtime=None)', False),
    ('test_frozen_workflow_reader.WorkflowReaderTests.test_values_and_literal_shell_bytes_survive_structured_read',
     'check_release_guard_coverage.py', 'return content, i', 'return content.rstrip(), i', False),
    ('test_frozen_workflow_reader.WorkflowReaderTests.test_duplicate_keys_cannot_silently_replace_obligations',
     'check_release_guard_coverage.py', 'if key in result:', 'if False:', False),
    ('test_frozen_workflow_reader.WorkflowReaderTests.test_unsupported_syntax_is_rejected_instead_of_losing_meaning',
     'check_release_guard_coverage.py', "raise ValueError('unsupported workflow scalar')", 'return value', False),
    ('test_frozen_workflow_reader.WorkflowReaderTests.test_classifier_and_frozen_plan_use_the_same_reader',
     'check_release_guard_coverage.py', "return read_workflow(ROOT / '.github/workflows' / name).raw_jobs", 'return {}', False),
    ('test_frozen_source_runner.SourceRunnerTests.test_consumer_uses_extracted_reviewed_bytes_and_receipt_rejects_tool_failure',
     'package_source.py', 'if result.returncode:', 'if False:', False),
    ('test_frozen_pipeline.PipelineTests.test_downloaded_substitution_cannot_reach_publish',
     'release_publication.py', "require(check_files(folder) == index, 'GitHub download differs from qualified publication')",
     "require(True, 'GitHub download differs from qualified publication')", False),
    ('test_frozen_tool_runner.ToolRunnerTests.test_real_wrapper_preserves_argv_environment_log_and_failure_code',
     'run_logged.py', 'raise SystemExit(result.returncode)', 'raise SystemExit(0)', False),
    ('test_frozen_artifact_flow.ArtifactFlowTests.test_recheck_consumes_both_candidates_and_crates_on_a_clean_runner',
     '../.github/workflows/ci.yml', 'merge-multiple: true', 'merge-multiple: false', False),
    ('test_frozen_target_plan.TargetPlanTests.test_every_target_step_binds_binary_wheel_and_continues_after_failure',
     'qualify_target.py', "    run('qualify_cli', 'qualification.json')", '    pass  # qualification omitted', False),
    ('test_frozen_receipts.FrozenReceiptTests.test_subtest_errors_are_not_mutation_detections',
     'tests/frozen_proofs.py', "status = 'FAIL' if issubclass(err[0], test.failureException) else 'ERROR'", "status = 'FAIL'", False),
    ('test_frozen_pipeline.PipelineTests.test_one_corpus_crosses_classifier_recorder_and_publication_recheck',
     'check_packages.py', '        _scan_content(data, name, depth, rule)', '        pass  # omitted classifier', False),
    ('test_frozen_pipeline.PipelineTests.test_consistent_digest_cannot_promote_foreign_source',
     'check_packages.py', 'if data != checkout_source_bytes(path_lookup(tracked, source)):', 'if False:', False),
    ('test_frozen_pipeline.PipelineTests.test_archive_identity_and_truncation_are_checked_on_consumer_bytes',
     'check_release_packages.py', '        verify_distribution(path)', '        pass  # skipped consumer verification', False),
    ('test_frozen_pipeline.PipelineTests.test_upload_and_publish_verify_exact_bytes_before_external_effect',
     'release_publication.py', "    verify_publication(directory / name for name in index['files'])\n    paths =", 
     "    if False: verify_publication(directory / name for name in index['files'])\n    paths =", False),
    ('test_frozen_pipeline.PipelineTests.test_prepare_runs_qualification_and_verifies_the_emitted_bundle',
     'release_publication.py', '        verify_publication([bundle])', '        if False: verify_publication([bundle])', False),
    ('test_frozen_plan.PlanTests.test_every_workflow_job_step_and_matrix_instance_requires_receipt',
     'tests/frozen_plan.py', "raise ValueError('missing or extra job/step receipt')", 'return', False),
    ('test_frozen_plan.PlanTests.test_receipts_bind_subject_scope_outcome_dependency_and_observation',
     'tests/frozen_plan.py', "raise ValueError('unobserved or foreign execution')", 'return', False),
    ('test_frozen_plan.PlanTests.test_yaml_layout_is_irrelevant_but_new_step_runner_or_artifact_is_not',
     'tests/frozen_plan.py', "raise ValueError('plan or subject changed')", 'return', False),
    ('test_frozen_campaign_plan.CampaignPlanTests.test_vm_executes_each_phase_with_bound_argv_and_ledger_receipt',
     'run_vm_campaign.py', "phase('transfers-workers16',", "phase('omitted-workers16',", False),
    ('test_frozen_campaign_plan.CampaignPlanTests.test_controller_dispatch_has_one_bound_receipt_for_every_phase',
     'release_campaign.py', "sealed = phase('seal', seal)", 'sealed = output', False),
    ('test_frozen_shell.ShellPlanTests.test_ci_shell_reaches_verifier_with_exact_subject_and_rejects_unreachable_command',
     '../.github/workflows/ci.yml', 'python scripts/release_version.py', 'python scripts/other_release_version.py', False, 2),
    ('two_processes_cannot_hold_the_same_campaign', 'campaign_state.py',
     'lock.seek(0)\n        if lock.read', 'yield\n        return\n        lock.seek(0)\n        if lock.read', False),
    ('test_campaign_fence.SessionTests.test_the_epoch_is_compared_byte_for_byte', 'campaign_model.py',
     "if current != 'x' + self.epoch + '\\nx':", 'if False:', False),
    ('test_campaign_fence.SessionTests.test_a_strict_renewal_after_the_end_of_the_lease_sends_nothing',
     'campaign_model.py', 'if strict and sent >= self.lease_until:', 'if strict and sent > self.lease_until:', False),
    ('test_campaign_fence.SessionTests.test_the_lease_ends_on_the_controller_clock_without_renewal',
     'campaign_model.py', 'and self.clock() < self.lease_until)', 'and self.clock() <= self.lease_until)', False),
    ('test_campaign_fence.SessionTests.test_a_failed_renewal_or_a_finished_channel_is_a_lost_admission',
     'campaign_model.py', "if reply.strip() != 'renewed':", 'if False:', False),
    ('renewal_latency_does_not_extend_authority', 'campaign_model.py',
     'self.lease_until = sent + self.lease', 'self.lease_until = self.clock() + self.lease', False),
    ('round_budget_never_restarts_phase_budget', 'campaign_model.py',
     'self.round_number = number', 'self.round_number = number\n        self.phase_deadline = time.monotonic() + seconds', False),
    ('fsync_precedes_replace_and_failure_preserves_previous_ledger', 'campaign_model.py',
     'os.fsync(stream.fileno())', 'pass  # injected omitted durability barrier', False),
    ('crash_at_persistence_boundaries_keeps_complete_generation', 'campaign_model.py',
     'os.fsync(stream.fileno())', 'pass  # injected omitted crash boundary', False),
    ('process_crash_releases_lock_but_not_running_attempt', 'campaign_model.py',
     "if attempts and (not retry or not reason):", 'if False:', False),
    ('pass_inventory_rejects_added_removed_and_replaced_artifacts', 'campaign_model.py',
     "if actual != previous['files']:", 'if False:', False),
    ('receipt_binds_each_result_and_every_artifact_to_exact_subject', 'tests/frozen_proofs.py',
     "raise ValueError('receipt subject mismatch')", 'return  # injected acceptance of foreign subject', False),
    ('every_target_and_evidence_class_rejects_substituted_bytes', 'release_publication.py',
     "digest(folder / name) == artifact['sha256']", 'True', False),
    ('admission_inventory_and_reboot_lease', 'campaign_shell.py',
     'else wait=$lease; fi', 'else wait=999999999999; fi', True),
    ('tls_profiles_enumerate_every_path_under_all_umasks', 'campaign_model.py',
     "'fixture_file': Permission(0o644, 0o022)", "'fixture_file': Permission(0o644, 0o000)", True),
    ('preparation_profiles_enumerate_every_created_file', 'campaign_model.py',
     "'fixture_file': Permission(0o644, 0o022)", "'fixture_file': Permission(0o644, 0o000)", True),
    ('real_access_and_default_acl_refuse_before_creation', 'campaign_shell.py',
     '[[ "$acl" =~ $base_acl ]] || fail acl;', ':;', True),
]


def test_id(case):
    if case[0].startswith('test_'):
        return case[0]
    if case[0].startswith('every_target_'):
        return 'test_frozen_artifacts.FrozenArtifactTests.test_' + case[0]
    if case[0].startswith('receipt_'):
        return 'test_frozen_receipts.FrozenReceiptTests.test_' + case[0]
    module, cls = ('test_frozen_posix', 'FrozenPosixTests') if case[4] else ('test_frozen_campaign', 'FrozenCampaignTests')
    return f'{module}.{cls}.test_{case[0]}'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inventory(root):
    root = canonical_path(root)
    sources = {p.relative_to(root).as_posix(): sha(p)
            for folder in COPY_DIRS for p in sorted((root / folder).rglob('*'))
            if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
    sources.update({name: sha(root / name) for name in IMPORT_INPUTS})
    return sources


def validate(receipt, subject, required, folder):
    """Independent consumer: complete test IDs, exact subject and artifact bytes."""
    if receipt.get('schema') != 1 or receipt.get('subject') != subject:
        raise ValueError('receipt subject mismatch')
    rows = receipt.get('tests', [])
    names = [row['id'] for row in rows]
    if not required or len(names) != len(set(names)) or set(names) != set(required):
        raise ValueError('receipt test inventory mismatch')
    if any(row['status'] != 'PASS' or row['subject'] != subject for row in rows):
        raise ValueError('receipt test not passed for subject')
    for attachments in (receipt.get('details', {}), receipt.get('plan_contract', {}).get('files', {})):
        for name, expected_digest in attachments.items():
            path = folder / name
            if not canonical_path(path).is_relative_to(canonical_path(folder)) or sha(path) != expected_digest:
                raise ValueError('receipt attachment differs')
    artifacts = receipt.get('artifacts', {})
    if not artifacts or set(artifacts) != {row['artifact'] for row in rows}:
        raise ValueError('receipt artifact inventory mismatch')
    for name, digest in artifacts.items():
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or (folder / path).is_symlink():
            raise ValueError('receipt artifact path invalid')
        if sha(folder / path) != digest:
            raise ValueError('receipt artifact bytes changed')
        observed = json.loads((folder / path).read_text(encoding='utf-8'))
        expected = [{'id': row['id'], 'status': row['status']} for row in rows if row['artifact'] == name]
        if observed != {'count': 1, 'tests': expected} or len(expected) != 1:
            raise ValueError('receipt outcome differs from artifact')


class Observations(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.rows = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.rows.append({'id': test.id(), 'status': 'PASS'})

    def addFailure(self, test, err):
        super().addFailure(test, err)
        if not any(row['id'] == test.id() for row in self.rows):
            self.rows.append({'id': test.id(), 'status': 'FAIL'})

    def addError(self, test, err):
        super().addError(test, err)
        previous = next((row for row in self.rows if row['id'] == test.id()), None)
        if previous is not None:
            previous['status'] = 'ERROR'
        else:
            self.rows.append({'id': test.id(), 'status': 'ERROR'})

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.rows.append({'id': test.id(), 'status': 'SKIP'})

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            status = 'FAIL' if issubclass(err[0], test.failureException) else 'ERROR'
            previous = next((row for row in self.rows if row['id'] == test.id()), None)
            if previous is None:
                self.rows.append({'id': test.id(), 'status': status})
            elif status == 'ERROR':
                previous['status'] = status


def worker(name, output):
    sys.path.insert(0, str(ROOT / 'scripts/tests'))
    os.environ['FROZEN_DETAILS'] = str(Path(output).with_suffix('.details'))
    result = Observations()
    unittest.defaultTestLoader.loadTestsFromName(name).run(result)
    Path(output).write_text(json.dumps({'count': result.testsRun, 'tests': result.rows}), encoding='utf-8')
    return 0 if result.wasSuccessful() and result.testsRun == 1 and not result.skipped else 1


def detail(name, value):
    """Optional raw observations owned by this test invocation, no payload bytes."""
    if 'FROZEN_DETAILS' in os.environ:
        folder = Path(os.environ['FROZEN_DETAILS'])
        folder.mkdir(exist_ok=True)
        (folder / name).write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def execute(root, name, output):
    scratch = root / 'target/tmp'
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        child = subprocess.run([sys.executable, str(root / 'scripts/tests/frozen_proofs.py'),
                                '--worker', name, '--output', str(output)],
                               cwd=root, capture_output=True,
                               env=dict(os.environ, TMP=str(scratch), TEMP=str(scratch), TMPDIR=str(scratch)))
    except subprocess.TimeoutExpired:
        return 'HARNESS_TIMEOUT'
    output.with_suffix('.log').write_bytes(child.stdout + child.stderr)
    if not output.exists():
        return 'HARNESS_ERROR'
    record = json.loads(output.read_text(encoding='utf-8'))
    rows = record['tests']
    if record['count'] != 1 or len(rows) != 1 or rows[0]['id'] != name:
        return 'HARNESS_ERROR'
    status = rows[0]['status']
    if status == 'PASS' and child.returncode != 0:
        return 'HARNESS_ERROR'
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--posix', action='store_true')
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker, args.output)
    args.output = canonical_path(args.output)
    args.output.mkdir(parents=True, exist_ok=False)
    subject = {'sources': inventory(ROOT), 'target': sys.platform,
               'revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
               'python': platform.python_version(), 'mode': 'local-fixture' if args.posix else 'offline',
               'run': uuid.uuid4().hex}
    cases = [case for case in CASES if args.posix or not case[4]]
    receipt = {'schema': 1, 'subject': subject, 'tests': [], 'artifacts': {}, 'mutations': []}
    for index, case in enumerate(cases):
        name = test_id(case)
        artifact = f'{index:02d}-baseline.json'
        status = execute(ROOT, name, args.output / artifact)
        receipt['tests'].append({'id': name, 'status': status, 'subject': subject, 'artifact': artifact})
        if (args.output / artifact).exists():
            receipt['artifacts'][artifact] = sha(args.output / artifact)
        # Fresh copy for each mutation: process imports and descendants see the
        # same changed bytes; originals are never written, even on interruption.
        with tempfile.TemporaryDirectory(prefix='sempl-mutant-', dir=ROOT / 'target') as temporary:
            sandbox = Path(temporary)
            for folder in COPY_DIRS:
                shutil.copytree(ROOT / folder, sandbox / folder,
                                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            # Import-time version/contract readers need the same public inputs.
            for input_name in IMPORT_INPUTS:
                (sandbox / input_name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / input_name, sandbox / input_name)
            source = sandbox / 'scripts' / case[1]
            original = source.read_text(encoding='utf-8')
            # Self-mutation excludes the injection catalog above validate().
            offset = original.index('\ndef validate(') if case[1] == 'tests/frozen_proofs.py' else 0
            prefix, body = original[:offset], original[offset:]
            if body.count(case[2]) != (case[5] if len(case) > 5 else 1):
                raise ValueError('mutation injection site changed; review recipe')
            source.write_text(prefix + body.replace(case[2], case[3]), encoding='utf-8')
            mutant_source_sha256 = sha(source)
            mutation_artifact = f'{index:02d}-mutation.json'
            mutant = execute(sandbox, name, args.output / mutation_artifact)
            receipt['mutations'].append({'id': name, 'file': case[1], 'before': case[2], 'after': case[3],
                                         'mutated_source_sha256': mutant_source_sha256,
                                         'status': mutant, 'detected': status == 'PASS' and mutant == 'FAIL',
                                         'artifact': mutation_artifact,
                                         'sha256': sha(args.output / mutation_artifact) if (args.output / mutation_artifact).exists() else None})
        print(f'{name}: baseline={status}, mutation={mutant}', flush=True)
    unchanged = inventory(ROOT) == subject['sources']
    receipt['original_sources_unchanged'] = unchanged
    receipt['details'] = {p.relative_to(args.output).as_posix(): sha(p)
                          for p in sorted(args.output.glob('*.details/*.json'))}
    from frozen_plan import export_contract
    receipt['plan_contract'] = export_contract(ROOT, args.output, subject)
    (args.output / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    validate(receipt, subject, [test_id(case) for case in cases], args.output)
    return 0 if unchanged and all(m['detected'] for m in receipt['mutations']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
