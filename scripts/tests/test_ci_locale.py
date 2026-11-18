"""Counterexamples for local execution: planning is never execution evidence."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ci_locale as ci


class LocalTests(unittest.TestCase):
    def setUp(self):
        (ci.ROOT / 'target').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ci.ROOT / 'target')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / '.github/workflows').mkdir(parents=True)
        self.host = 'windows-latest' if os.name == 'nt' else 'ubuntu-latest'

    def plan(self, jobs):
        (self.root / '.github/workflows/ci.yml').write_text(
            'on:\n  pull_request:\njobs:\n' + jobs, encoding='utf-8')
        return ci.derive(self.root)

    def execute(self, plan, runner=ci.invoke):
        with patch.object(ci, 'git', return_value='a' * 40):
            path, receipt = ci.execute(plan, self.root, runner=runner)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['status'], receipt['status'])
        return receipt

    def test_matrix_needs_and_real_execution(self):
        plan = self.plan(f'  first:\n    runs-on: {self.host}\n    strategy:\n      matrix:\n        value: [one, two]\n'
                         '    steps:\n      - run: python -c "print(123)"\n'
                         f'  second:\n    runs-on: {self.host}\n    needs: first\n'
                         '    steps:\n      - run: python -c "print(456)"\n')
        self.assertEqual(len(plan['jobs']), 3)
        self.assertEqual(len(plan['graph']['nodes']['ci.yml/second/0/step/0']['needs']), 2)
        receipt = self.execute(plan)
        self.assertEqual(receipt['status'], 'PASS')
        self.assertTrue(all(s['executed'] and s['argv'] for s in receipt['steps']))

    def test_failure_blocks_needs_even_after_always_pass(self):
        plan = self.plan(f'  first:\n    runs-on: {self.host}\n    steps:\n'
                         '      - run: python -c "raise SystemExit(7)"\n'
                         '      - if: always()\n        run: python -c "print(1)"\n'
                         f'  second:\n    runs-on: {self.host}\n    needs: first\n'
                         '    steps:\n      - run: python -c "print(2)"\n')
        receipt = self.execute(plan)
        self.assertEqual(receipt['status'], 'FAIL')
        self.assertEqual([s['status'] for s in receipt['steps']], ['FAIL', 'PASS', ci.NOT_RUN])

    def test_false_unknown_and_missing_steps_never_pass(self):
        for step in ('if: false\n        run: python -c "print(1)"',
                     'if: secrets.unknown\n        run: python -c "print(1)"',
                     'name: absent', 'uses: new/action@unknown',
                     'run: python scripts/absent.py'):
            with self.subTest(step=step):
                plan = self.plan(f'  test:\n    runs-on: {self.host}\n    steps:\n      - {step}\n')
                result = self.execute(plan)
                self.assertEqual(result['status'], ci.NOT_RUN)
                self.assertFalse(any(s['executed'] for s in result['steps']))

    def test_empty_job_and_skipped_test_are_incomplete(self):
        self.assertEqual(self.execute(self.plan(f'  test:\n    runs-on: {self.host}\n    steps: []\n'))['status'], ci.NOT_RUN)
        plan = self.plan(f'  test:\n    runs-on: {self.host}\n    steps:\n'
                         '      - run: python -c "print(\'skipped=1\')"\n')
        self.assertEqual(self.execute(plan)['status'], ci.NOT_RUN)

    def test_fixture_and_vm_commands_are_not_executed(self):
        for command in ('docker compose up', 'python scripts/run_vm_campaign.py', 'bash scripts/verify.sh'):
            plan = self.plan(f'  test:\n    runs-on: {self.host}\n    steps:\n      - run: {command}\n')
            result = self.execute(plan)
            self.assertEqual(result['status'], ci.NOT_RUN)
            self.assertIn('Docker o VM', result['steps'][0]['reason'])

    def test_action_pin_and_checkout_sha(self):
        checkout = next(k for k, v in ci.ACTION_MAP.items() if v == 'checkout')
        plan = self.plan(f'  test:\n    runs-on: {self.host}\n    steps:\n      - uses: {checkout}\n')
        self.assertEqual(self.execute(plan, lambda *a: (0, 'b' * 40, ''))['status'], ci.NOT_RUN)
        self.assertEqual(self.execute(plan, lambda *a: (0, 'a' * 40, ''))['status'], 'PASS')
        plan['graph']['nodes']['ci.yml/test/0/step/0']['definition']['uses'] += 'changed'
        self.assertEqual(self.execute(plan)['status'], ci.NOT_RUN)

    def test_contract_checkout_pin_is_checked_without_clone(self):
        (self.root / 'contracts/upstream').mkdir(parents=True)
        (self.root / 'contracts/upstream/source.json').write_text(json.dumps({'revision': 'c' * 40}))
        (self.root / '.plenora-contracts').mkdir()
        step = {'uses': next(k for k, v in ci.ACTION_MAP.items() if v == 'checkout'),
                'with': {'repository': 'PlenoraETL/plenora-contracts', 'path': '.plenora-contracts', 'ref': 'c' * 40}}
        self.assertEqual(ci.action_commands(step, self.root, 'a' * 40)[0][1], 'c' * 40)
        step['with']['ref'] = 'main'
        with self.assertRaises(ci.Unsupported):
            ci.action_commands(step, self.root, 'a' * 40)

    def test_reusable_caller_if_and_profiles(self):
        (self.root / '.github/workflows/called.yml').write_text(
            f'on:\n  workflow_call:\njobs:\n  test:\n    runs-on: {self.host}\n'
            '    steps:\n      - run: python -c "print(1)"\n', encoding='utf-8')
        plan = self.plan('  caller:\n    if: false\n    uses: ./.github/workflows/called.yml\n')
        result = self.execute(plan)
        self.assertFalse(any(s['status'] == 'PASS' for s in result['steps']))
        for profile in ('release', 'qualifica'):
            with self.assertRaises(ci.Unsupported):
                ci.execute(ci.derive(self.root, profile), self.root)

    def test_always_does_not_restore_success_condition(self):
        plan = self.plan(f'  test:\n    runs-on: {self.host}\n    steps:\n'
                         '      - run: python -c "raise SystemExit(4)"\n'
                         '      - if: always()\n        run: python -c "print(1)"\n'
                         '      - run: python -c "print(2)"\n')
        result = self.execute(plan)
        self.assertEqual([s['status'] for s in result['steps']], ['FAIL', 'PASS', ci.NOT_RUN])

    def test_environment_directory_and_shell_are_preserved(self):
        (self.root / 'work').mkdir()
        plan = self.plan(f'  test:\n    runs-on: {self.host}\n    env:\n      CARGO_BUILD_JOBS: 99\n'
                         '      LOCAL_VALUE: example\n    defaults:\n      run:\n        working-directory: work\n'
                         '    steps:\n      - shell: python\n        run: |\n'
                         '          import os\n          assert os.environ["CARGO_BUILD_JOBS"] == "4"\n'
                         '          assert os.environ["LOCAL_VALUE"] == "example"\n')
        result = self.execute(plan)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['steps'][0]['cwd'], str(self.root / 'work'))

    def test_executor_exception_is_redacted(self):
        plan = self.plan(f'  test:\n    runs-on: {self.host}\n    steps:\n'
                         '      - run: python -c "print(1)"\n')
        def broken(*args):
            raise RuntimeError('private-callback-payload')
        result = self.execute(plan, broken)
        self.assertEqual(result['status'], 'FAIL')
        self.assertNotIn('private-callback-payload', json.dumps(result))

    def test_yaml_extension_is_not_silently_omitted(self):
        self.plan(f'  test:\n    runs-on: {self.host}\n    steps:\n      - name: absent\n')
        (self.root / '.github/workflows/ci.yml').rename(self.root / '.github/workflows/ci.yaml')
        plan = ci.derive(self.root)
        self.assertEqual(len(plan['jobs']), 1)
        self.assertEqual(self.execute(plan)['status'], ci.NOT_RUN)

    def test_real_plan_covers_every_job_and_has_no_pass(self):
        plan = ci.derive()
        documents = ci.workflows(ci.ROOT)
        self.assertEqual({(j['context']['workflow'], j['id']) for j in plan['jobs']},
                         {(w, j) for w, d in documents.items() for j in d['jobs']})
        self.assertFalse(any('status' in s for j in plan['jobs'] for s in j['steps']))
        self.assertTrue(any('/call/' in j['key'] and j['selected'] for j in plan['jobs']))


if __name__ == '__main__':
    unittest.main()
