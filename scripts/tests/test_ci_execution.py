"""Executor observations and destructive mutations; no Rust or hosted actions."""
from copy import deepcopy
from pathlib import Path
import sys
import shutil
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ci_plan as plan
import ci_contract as contract
import ci_artifacts as artifacts
import ci_policy as policy
import ci_shell as shell
import check_release_guard_coverage as coverage
from path_fixtures import short_path

ROOT = Path(__file__).resolve().parents[2]


class ExecutionTests(unittest.TestCase):
    def test_handoff_canonicalizes_real_short_roots_for_upload_and_download(self):
        with tempfile.TemporaryDirectory(prefix='long handoff root fixture ') as temp:
            short = short_path(Path(temp))
            graph = plan.compile_plan(plan.workflows(ROOT))
            with patch.object(tempfile, 'tempdir', str(short)):
                result = contract.handoff(graph, {'revision': 'short-path-fixture'})
                self.assertEqual(len(result['consumer']), 3)
                self.assertEqual({r['status'] for r in result['execution']['receipts'].values()}, {'PASS'})
                with self.assertRaises(ValueError):
                    artifacts.confined(short, '../outside')
                def unresolved(self, workspaces):
                    self.workspaces, self.store = workspaces, {}
                with patch.object(artifacts.ArtifactExecutor, '__init__', unresolved), self.assertRaises(ValueError):
                    contract.handoff(graph, {'revision': 'short-path-fixture'})

    def test_real_shell_receipts_reject_unreachable_commands_and_omitted_dispatch(self):
        graph = plan.compile_plan(plan.workflows(ROOT))
        receipt = shell.contracts(graph, coverage.invocations)
        self.assertEqual(len(receipt['rows']), 6)
        windows = sys.platform == 'win32'
        body = 'python scripts/release_version.py'
        expected = [['scripts/release_version.py']]
        shell.verify_commands(shell.observe(body, windows), expected)
        hidden = 'if ($false) { ' + body + ' }' if windows else 'if false; then ' + body + '; fi'
        def refuse():
            with self.assertRaises(ValueError):
                shell.verify_commands(shell.observe(hidden, windows), expected)
        refuse()
        with patch.object(shell, 'verify_commands'), self.assertRaises(AssertionError):
            refuse()
        with patch.object(shell, 'observe', return_value=[]), self.assertRaises(ValueError):
            shell.contracts(graph, coverage.invocations)

    @unittest.skipUnless(shutil.which('bash'), 'requires Bash (Git Bash is supported on Windows)')
    def test_bash_recheck_subjects_are_distinct_and_cannot_exchange_dispatch(self):
        documents = plan.workflows(ROOT)
        receipt = shell.contracts(plan.compile_plan(documents), coverage.invocations, windows=False)
        rows = {r['executor']: r for r in receipt['rows'] if r['job'] == 'release-candidate-recheck'}
        self.assertEqual(rows['verify-crate-packages']['observed'],
                         [['scripts/check_release_packages.py', 'target/release-package-regression']])
        self.assertEqual(rows['verify-release-packages']['observed'],
                         [['scripts/release_version.py'], ['scripts/check_release_packages.py',
                          'target/candidate-recheck/4.0.0', '--release']])
        for mutation in ('exchange', 'duplicate', 'missing', 'unreachable'):
            changed = deepcopy(documents)
            job = changed['ci.yml']['jobs']['release-candidate-recheck']
            steps = shell.recheck_steps(job, coverage.invocations)
            crate, release = steps['verify-crate-packages'], steps['verify-release-packages']
            if mutation == 'exchange':
                crate['run'], release['run'] = release['run'], crate['run']
            elif mutation == 'duplicate':
                crate['id'] = release['id']
            elif mutation == 'missing':
                job['steps'].remove(crate)
            else:
                release['run'] = 'if false; then\n' + release['run'] + '\nfi'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                shell.contracts(plan.compile_plan(changed), coverage.invocations, windows=False)

    @unittest.skipUnless(shutil.which('bash'), 'requires Bash (Git Bash is supported on Windows)')
    def test_bash_preserves_literal_arguments_and_propagates_child_failure(self):
        body = "docker compose run storage-rust bash -c 'set -euo pipefail\npython3 scripts/probe.py \"a b\" /usr/local/share/fixture\n'"
        expected = [['docker', 'compose', 'run', 'storage-rust', 'bash', '-c'],
                    ['scripts/probe.py', 'a b', '/usr/local/share/fixture']]
        self.assertEqual(shell.observe(body, False), expected)
        failing = shell.PROBE + "\nif args == ['scripts/probe.py', 'a b', '/usr/local/share/fixture']: sys.exit(7)\n"
        def rejects():
            with self.assertRaises(ValueError):
                shell.observe(body + '\npython3 scripts/should_not_run.py', False)
        with patch.object(shell, 'PROBE', failing):
            rejects()
            original = shell.subprocess.run
            def ignore_failure(*args, **kwargs):
                result = original(*args, **kwargs)
                result.returncode = 0
                return result
            with patch.object(shell.subprocess, 'run', side_effect=ignore_failure), self.assertRaises(AssertionError):
                rejects()

    def test_receipts_reject_missing_foreign_failed_and_relinked_execution(self):
        graph = plan.compile_plan({'fixture': {'jobs': {
            'build': {'steps': [{'run': 'build'}]},
            'consume': {'needs': 'build', 'steps': [{'run': 'consume'}]}}}})
        subject = {'revision': 'fixture', 'artifacts': {'candidate': 'a' * 64}}
        good = plan.execute(graph, subject, lambda *args: ('PASS', {'calls': 1}), scope='test')
        plan.verify_execution(graph, subject, good, scope='test')
        def reject():
            identifier = 'fixture/consume/0/step/0'
            for field, value in [('status', 'SKIP'), ('status', 'FAIL'), ('status', 'ERROR'),
                                 ('scope', 'qualified'), ('subject', {}), ('needs', {}),
                                 ('definition', 'other'), ('observation', {'calls': 0})]:
                changed = deepcopy(good)
                changed['receipts'][identifier][field] = value
                with self.assertRaises(ValueError):
                    plan.verify_execution(graph, subject, changed, scope='test')
            for key in graph['nodes']:
                changed = deepcopy(good)
                del changed['receipts'][key]
                with self.assertRaises(ValueError):
                    plan.verify_execution(graph, subject, changed, scope='test')
            with self.assertRaises(ValueError):
                plan.verify_execution(graph, {'revision': 'other'}, good, scope='test')
        reject()
        with patch.object(plan, 'verify_execution'), self.assertRaises(AssertionError):
            reject()

    def test_failure_blocks_dependents_but_preserves_independent_observations(self):
        graph = plan.compile_plan({'fixture': {'jobs': {
            'bad': {'steps': [{'run': 'fail'}]},
            'dependent': {'needs': 'bad', 'steps': [{'run': 'must not run'}]},
            'independent': {'steps': [{'run': 'observe'}]}}}})
        def observe(executor):
            calls = []
            def dispatch(identifier, node, *unused):
                calls.append(identifier)
                if node['context']['job'] == 'bad':
                    raise RuntimeError('PRIVATE CALLBACK CONTENT')
                return 'PASS', {'called': identifier}
            result = executor(graph, {}, dispatch, scope='test')
            self.assertNotIn('fixture/dependent/0/step/0', calls)
            self.assertIn('fixture/independent/0/step/0', calls)
            self.assertEqual(result['receipts']['fixture/bad/0/step/0']['status'], 'ERROR')
            self.assertEqual(result['receipts']['fixture/dependent/0/step/0']['status'], 'BLOCKED')
            self.assertNotIn('PRIVATE CALLBACK CONTENT', str(result))
        observe(plan.execute)
        def unguarded(graph, subject, executor, **kwargs):
            for key in graph['order']:
                try:
                    executor(key, graph['nodes'][key], subject, {})
                except Exception:
                    pass
            return {'receipts': {}}
        with self.assertRaises(AssertionError):
            observe(unguarded)

    def test_handoff_executes_workflow_options_and_rejects_wrong_merge_path_or_bytes(self):
        documents = plan.workflows(ROOT)
        graph = plan.compile_plan(documents)
        result = contract.handoff(graph, {'revision': 'fixture'})
        self.assertEqual(len(result['consumer']), 3)
        for change in ('merge', 'path', 'needs', 'upload'):
            mutated = deepcopy(documents)
            jobs = mutated['ci.yml']['jobs']
            steps = policy.actions(jobs['release-candidate-recheck'], 'actions/download-artifact')
            if change == 'merge':
                steps[-1]['with']['merge-multiple'] = False
            elif change == 'path':
                steps[-1]['with']['path'] = 'wrong-subject'
            elif change == 'needs':
                jobs['release-candidate-recheck']['needs'] = ['release-package-build']
            else:
                policy.actions(jobs['native-windows'], 'actions/upload-artifact')[0]['with']['path'] = 'producer-cache'
            with self.subTest(change=change), self.assertRaises(ValueError):
                contract.handoff(plan.compile_plan(mutated), {'revision': 'fixture'})
        original = artifacts.ArtifactExecutor.__call__
        def substitute(self, identifier, node, subject, incoming):
            if node['definition'].get('uses', '').startswith('actions/download-artifact@'):
                for asset in self.store.values():
                    asset['files'] = {key: b'substituted' for key in asset['files']}
            return original(self, identifier, node, subject, incoming)
        with patch.object(artifacts.ArtifactExecutor, '__call__', substitute), self.assertRaises(ValueError):
            contract.handoff(graph, {'revision': 'fixture'})

    def test_test_executor_never_promotes_zero_skip_error_or_failure(self):
        node = {'kind': 'step', 'definition': {'run': 'synthetic'}}
        def observe():
            for count, skipped, errors, failures in [(0, [], [], []), (1, [('id', 'skip')], [], []),
                                                     (1, [], [('id', 'error')], []), (1, [], [], [('id', 'fail')])]:
                result = unittest.TestResult()
                result.testsRun, result.skipped, result.errors, result.failures = count, skipped, errors, failures
                with patch.object(unittest.TextTestRunner, 'run', return_value=result):
                    status, observation = contract.TestExecutor()('node', node, {}, {})
                    self.assertEqual(status, 'FAIL')
                    self.assertEqual(observation['run'], count)
        observe()
        with patch.object(contract.TestExecutor, '__call__', return_value=('PASS', {'run': 0})), self.assertRaises(AssertionError):
            observe()

    def test_policy_uses_parsed_jobs_not_comment_or_layout_tokens(self):
        docs = plan.workflows(ROOT)
        graph = plan.compile_plan(docs)
        policy.check(graph, coverage.invocations)
        # Reordered keys and quoted scalars have the identical compiled graph.
        a = 'jobs:\n  one:\n    runs-on: linux\n    steps:\n      - run: echo ok\n'
        b = '# comment\njobs:\n    one:\n        steps:\n          - run: "echo ok"\n        runs-on: "linux"\n'
        self.assertEqual(plan.compile_plan({'x': plan.parse_workflow(a)}),
                         plan.compile_plan({'x': plan.parse_workflow(b)}))
        mutated = deepcopy(docs)
        job = mutated['ci.yml']['jobs']['release-candidate-recheck']
        job['runs-on'] = 'windows-latest'
        job['steps'].append({'run': '# runs-on: ubuntu-24.04'})
        def reject():
            with self.assertRaises(ValueError):
                policy.check(plan.compile_plan(mutated), coverage.invocations)
        reject()
        with patch.object(policy, 'check'), self.assertRaises(AssertionError):
            reject()
