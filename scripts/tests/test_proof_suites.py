"""The VM collector must retain every result and finish red after any failure."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which('bash'), 'requires Bash (Git Bash is supported)')
class SuiteCollectorTests(unittest.TestCase):
    def test_vm_plan_reaches_both_masks_and_frozen_after_first_failure(self):
        helper = (ROOT / 'scripts/proof_suites.sh').read_text(encoding='utf-8')
        def observe(source):
            # Only exercise scheduling. The interpreter and platform preflight
            # tools are inert fixtures; none of these are real proof results.
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / 'scripts').mkdir()
                (root / 'scripts/proof_suites.sh').write_text(source, encoding='utf-8', newline='\n')
                shutil.copyfile(ROOT / 'scripts/collect_storage_proofs.sh', root / 'scripts/collect_storage_proofs.sh')
                tools = root / 'tools'
                tools.mkdir()
                for name in ('python3', 'flock', 'getfacl', 'setfacl', 'openssl', 'ssh-keygen', 'git', 'dpkg-query', 'sudo', 'id', 'fixture-python'):
                    path = tools / name
                    body = ("case \"$*\" in *test_ci_execution.py*) exit 7;; esac\nexit 0\n"
                            if name == 'fixture-python' else 'echo 0\n')
                    path.write_text('#!/bin/sh\n' + body, encoding='utf-8', newline='\n')
                    path.chmod(0o755)
                env = dict(os.environ, PYTHON=(tools / 'fixture-python').as_posix(),
                           PATH=str(tools) + os.pathsep + os.environ['PATH'], MSYS2_ARG_CONV_EXCL='*')
                result = subprocess.run([shutil.which('bash'), '--noprofile', '--norc', 'scripts/collect_storage_proofs.sh'],
                                        cwd=root, env=env, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 1)
                summaries = list((root / 'target').glob('sempl-3-vm-*/summary.tsv'))
                self.assertEqual(len(summaries), 1)
                rows = [line.split('\t') for line in summaries[0].read_text().splitlines()[1:]]
                statuses = {r[0]: r[1] for r in rows}
                self.assertEqual(len(rows), len(statuses))
                for mask in ('002', '022'):
                    self.assertEqual(statuses[mask + '/test_ci_execution'], '7')
                    self.assertEqual(statuses[mask + '/frozen-catalog'], '0')
                    self.assertEqual(statuses[mask + '/frozen-discovery'], '0')
                    self.assertEqual(statuses[mask + '/test_release_workflow'], '0')
                    self.assertEqual(sum(key.startswith(mask + '/') for key in statuses), 23)
        observe(helper)
        for before, after in [('return 0', 'exit "$status"'), ('suite_failures=1', 'suite_failures=0')]:
            with self.subTest(mutation=before), self.assertRaises(AssertionError):
                observe(helper.replace(before, after))

    def test_collection_continues_and_retains_failure_with_mutations(self):
        helper = (ROOT / 'scripts/proof_suites.sh').read_text(encoding='utf-8')
        def observe(source):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / 'collector.sh').write_text(source, encoding='utf-8', newline='\n')
                body = '''set -uo pipefail
source ./collector.sh
suite_summary=summary.tsv
run_suite first first.log "$BASH" -c 'echo first; exit 7'
run_suite frozen frozen.log "$BASH" -c 'echo frozen-reached; exit 0'
run_suite last last.log "$BASH" -e -c 'echo last; false; echo unreachable'
finish_suites
'''
                (root / 'run.sh').write_text(body, encoding='utf-8', newline='\n')
                env = dict(os.environ, MSYS2_ARG_CONV_EXCL='*')
                result = subprocess.run([shutil.which('bash'), '--noprofile', '--norc', 'run.sh'],
                                        cwd=root, env=env, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 1)
                rows = [line.split('\t') for line in (root / 'summary.tsv').read_text().splitlines()]
                self.assertEqual([(r[0], r[1]) for r in rows], [('first', '7'), ('frozen', '0'), ('last', '1')])
                self.assertIn('frozen-reached', (root / 'frozen.log').read_text())
                self.assertNotIn('unreachable', (root / 'last.log').read_text())
        observe(helper)
        for before, after in [('return 0', 'exit "$status"'), ('suite_failures=1', 'suite_failures=0')]:
            with self.subTest(mutation=before), self.assertRaises(AssertionError):
                observe(helper.replace(before, after))
