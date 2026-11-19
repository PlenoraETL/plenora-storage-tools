"""Offline executors used by the release coverage gate; never hosted CI results."""
import io
from pathlib import Path
import sys
import tempfile
import unittest
import platform
import uuid
import subprocess

from ci_plan import compile_plan, execute, verify_execution
from ci_policy import artifact_plan
from ci_artifacts import ArtifactExecutor, sha


BOUNDARIES = (
    'test_frozen_pipeline.PipelineTests.test_one_corpus_crosses_classifier_recorder_and_publication_recheck',
    'test_frozen_pipeline.PipelineTests.test_consistent_digest_cannot_promote_foreign_source',
    'test_frozen_pipeline.PipelineTests.test_archive_identity_and_truncation_are_checked_on_consumer_bytes',
    'test_frozen_pipeline.PipelineTests.test_upload_and_publish_verify_exact_bytes_before_external_effect',
    'test_frozen_pipeline.PipelineTests.test_prepare_runs_qualification_and_verifies_the_emitted_bundle',
    'test_frozen_pipeline.PipelineTests.test_downloaded_substitution_cannot_reach_publish',
    'test_frozen_source_runner.SourceRunnerTests.test_consumer_uses_extracted_reviewed_bytes_and_receipt_rejects_tool_failure',
    'test_frozen_target_plan.TargetPlanTests.test_every_target_step_binds_binary_wheel_and_continues_after_failure',
    'test_frozen_tool_runner.ToolRunnerTests.test_real_wrapper_preserves_argv_environment_log_and_failure_code',
)


def source_subject(root):
    paths = [p for folder in ('scripts', '.github/workflows') for p in (root / folder).rglob('*')
             if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc']
    paths += [root / name for name in ('Cargo.toml', '.python-version', 'rust-toolchain.toml')]
    revision = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                       text=True, stderr=subprocess.DEVNULL).strip()
    return {'sources': {p.relative_to(root).as_posix(): sha(p.read_bytes()) for p in sorted(paths)},
            'revision': revision, 'run': str(uuid.uuid4()), 'platform': sys.platform,
            'python': platform.python_version(),
            'scope': 'offline-executor-contract'}


def handoff(graph, subject):
    plan = artifact_plan(graph)
    fixtures = {'docker-conformance': ('dist/4.0.0/x86_64-unknown-linux-gnu/subject.bin', b'linux'),
                'native-windows': ('dist/4.0.0/x86_64-pc-windows-msvc/subject.bin', b'windows'),
                'release-package-build': ('target/package/fixture.crate', b'crate')}
    with tempfile.TemporaryDirectory(prefix='ci-handoff-') as temp:
        roots = {name: Path(temp) / name for name in [*fixtures, 'release-candidate-recheck']}
        for name, (relative, data) in fixtures.items():
            path = roots[name] / relative
            path.parent.mkdir(parents=True)
            path.write_bytes(data)
            (roots[name] / 'producer-cache').write_bytes(b'not transferable')
        roots['release-candidate-recheck'].mkdir()
        result = execute(plan, subject, ArtifactExecutor(roots), scope='local-artifact-contract')
        verify_execution(plan, subject, result, scope='local-artifact-contract')
        consumer = roots['release-candidate-recheck']
        actual = {p.relative_to(consumer).as_posix(): sha(p.read_bytes())
                  for p in consumer.rglob('*') if p.is_file()}
        expected = {'target/candidate-recheck/4.0.0/x86_64-unknown-linux-gnu/subject.bin': sha(b'linux'),
                    'target/candidate-recheck/4.0.0/x86_64-pc-windows-msvc/subject.bin': sha(b'windows'),
                    'target/release-package-regression/fixture.crate': sha(b'crate')}
        if actual != expected:
            raise ValueError('consumer artifact inventory or bytes differ')
        return {'plan': plan, 'execution': result, 'consumer': actual}


class TestExecutor:
    def __call__(self, identifier, node, subject, incoming):
        if node['kind'] == 'job':
            return 'PASS', {'completed': node['context']['job']}
        test = node['definition']['run']
        # unittest output can contain inspected payloads; publish only identities,
        # counts and categories. The detailed traceback stays in ordinary tests.
        suite = unittest.defaultTestLoader.loadTestsFromName(test)
        result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
        status = 'PASS' if (result.testsRun == 1 and result.wasSuccessful() and not result.skipped) else 'FAIL'
        return status, {'test': test, 'run': result.testsRun, 'failures': len(result.failures),
                        'errors': len(result.errors), 'skipped': len(result.skipped)}


def boundaries(root, subject):
    path = str(root / 'scripts/tests')
    sys.path.insert(0, path)
    try:
        plan = compile_plan({'boundary-contract': {'jobs': {
            str(i): {'steps': [{'run': test}]} for i, test in enumerate(BOUNDARIES)}}})
        result = execute(plan, subject, TestExecutor(), scope='offline-boundary-tests')
        verify_execution(plan, subject, result, scope='offline-boundary-tests')
        return {'plan': plan, 'execution': result}
    finally:
        sys.path.remove(path)
