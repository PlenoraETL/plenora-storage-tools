"""Workflow graph/receipt mutations; does not claim to execute hosted CI."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import frozen_plan as plan

ROOT = Path(__file__).resolve().parents[2]


class PlanTests(unittest.TestCase):
    def test_every_workflow_job_step_and_matrix_instance_requires_receipt(self):
        graph = plan.compile_plan(plan.workflows(ROOT))
        subject = {'revision': 'fixture-revision', 'artifacts': {'candidate': 'a' * 64}, 'epoch': 'fixture-epoch'}
        calls = []
        def observer(identifier, node, bound, incoming):
            calls.append(identifier)
            return plan.observer(identifier, node, bound, incoming)
        bundle = plan.replay(graph, subject, observer)
        plan.verify(graph, subject, bundle)
        self.assertEqual(calls, graph['order'])
        self.assertTrue(any('/call/' in key for key in calls))
        for identifier in graph['order']:
            row = bundle['receipts'].pop(identifier)
            try:
                with self.subTest(missing=identifier), self.assertRaises(ValueError):
                    plan.verify(graph, subject, bundle)
            finally:
                bundle['receipts'][identifier] = row

    def test_receipts_bind_subject_scope_outcome_dependency_and_observation(self):
        graph = plan.compile_plan({'fixture.yml': {'jobs': {
            'build': {'steps': [{'run': 'producer'}]},
            'check': {'needs': 'build', 'steps': [{'run': 'consumer'}]}}}})
        subject = {'revision': 'a', 'target': 'fixture', 'mode': 'offline', 'artifacts': {'wheel': 'a' * 64}}
        good = plan.replay(graph, subject, plan.observer)
        plan.verify(graph, subject, good)
        node = 'fixture.yml/check/0/step/0'
        for field, value in [('status', 'SKIP'), ('status', 'FAIL'), ('subject', {'revision': 'other'}),
                             ('scope', 'qualified'), ('needs', {}), ('observation', {'status': 'PASS'}),
                             ('definition', 'other'), ('node', 'other')]:
            changed = deepcopy(good)
            changed['receipts'][node][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                plan.verify(graph, subject, changed)
        for key in subject:
            with self.subTest(subject=key), self.assertRaises(ValueError):
                plan.verify(graph, dict(subject, **{key: 'changed'}), good)

    def test_yaml_layout_is_irrelevant_but_new_step_runner_or_artifact_is_not(self):
        documents = plan.workflows(ROOT)
        graph = plan.compile_plan(documents)
        # The same reader accepts comments, quoting and alternate indentation.
        original = 'on:\n  push:\njobs:\n  check:\n    runs-on: linux\n    steps:\n      - run: echo ok\n'
        reformatted = '# comment\n"jobs":\n    check:\n        steps:\n          - run: "echo ok" # comment\n        runs-on: linux\non:\n    push:\n'
        self.assertEqual(plan.parse_workflow(original), plan.parse_workflow(reformatted))
        subject = {'revision': 'fixture'}
        bundle = plan.replay(graph, subject, plan.observer)
        for change in ('step', 'runner', 'artifact', 'needs'):
            mutated = deepcopy(documents)
            jobs = mutated['ci.yml']['jobs']
            if change == 'step':
                jobs['proof-inventory']['steps'].append({'run': 'new-check'})
            elif change == 'runner':
                jobs['proof-inventory']['runs-on'] = 'windows-latest'
            elif change == 'artifact':
                jobs['proof-inventory']['steps'][-1]['with']['path'] = 'another-subject.json'
            else:
                jobs['release-candidate-recheck']['needs'] = ['release-package-build']
            with self.subTest(change=change), self.assertRaises(ValueError):
                plan.verify(plan.compile_plan(mutated), subject, bundle)
        with self.assertRaises(ValueError):
            plan.parse_workflow('jobs:\n  a:\n    runs-on: linux\njobs:\n  b:\n    runs-on: linux\n')


if __name__ == '__main__':
    unittest.main()
