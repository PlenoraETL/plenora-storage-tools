"""Adversarial consumer tests for Sempl-1 receipts (not release qualification)."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from frozen_proofs import sha, validate, Observations


class FrozenReceiptTests(unittest.TestCase):
    def test_subtest_errors_are_not_mutation_detections(self):
        for error, expected in [(AssertionError, 'FAIL'), (RuntimeError, 'ERROR')]:
            class Probe(unittest.TestCase):
                def runTest(self):
                    with self.subTest(case='injected'):
                        raise error('synthetic')
            result = Observations()
            Probe().run(result)
            self.assertEqual(result.testsRun, 1)
            self.assertEqual([row['status'] for row in result.rows], [expected])

    def test_receipt_binds_each_result_and_every_artifact_to_exact_subject(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            observation = {'count': 1, 'tests': [{'id': 'suite.case', 'status': 'PASS'}]}
            (root / 'result.json').write_text(json.dumps(observation))
            subject = {'revision': 'fixture', 'sources': {'runner.py': 'digest'},
                       'target': 'fixture-os', 'mode': 'offline', 'run': 'run-1'}
            receipt = {'schema': 1, 'subject': subject,
                       'tests': [{'id': 'suite.case', 'status': 'PASS', 'subject': subject,
                                  'artifact': 'result.json'}],
                       'artifacts': {'result.json': sha(root / 'result.json')}}
            validate(receipt, subject, ['suite.case'], root)
            with self.assertRaises(ValueError):
                validate(dict(receipt, details={'result.json': '0' * 64}), subject, ['suite.case'], root)
            for key in subject:
                changed = copy.deepcopy(subject)
                changed[key] = 'different'
                with self.subTest(key=key), self.assertRaises(ValueError):
                    validate(receipt, changed, ['suite.case'], root)
            for status in ('SKIP', 'RUNNING', 'FAIL', 'ERROR'):
                changed = copy.deepcopy(receipt)
                changed['tests'][0]['status'] = status
                with self.subTest(status=status), self.assertRaises(ValueError):
                    validate(changed, subject, ['suite.case'], root)
            for rows in ([], receipt['tests'] * 2,
                         [dict(receipt['tests'][0], id='homonymous.case')],
                         [dict(receipt['tests'][0], subject={'run': 'other'})]):
                with self.subTest(rows=rows), self.assertRaises(ValueError):
                    validate(dict(receipt, tests=rows), subject, ['suite.case'], root)
            with self.assertRaises(ValueError):
                validate(receipt, subject, ['suite.case', 'new.requirement'], root)
            with self.assertRaises(ValueError):
                validate(dict(receipt, artifacts={}), subject, ['suite.case'], root)
            (root / 'result.json').write_text('suite.case ... ok\nfrom another run')
            with self.assertRaises(ValueError):
                validate(receipt, subject, ['suite.case'], root)
            # A digest updated to cover a failed/empty artifact cannot turn it
            # into a passing test merely by changing the receipt summary.
            for observation in ({'count': 0, 'tests': []},
                                {'count': 1, 'tests': [{'id': 'suite.case', 'status': 'FAIL'}]}):
                (root / 'result.json').write_text(json.dumps(observation))
                changed = dict(receipt, artifacts={'result.json': sha(root / 'result.json')})
                with self.assertRaises(ValueError):
                    validate(changed, subject, ['suite.case'], root)
            with self.assertRaises(json.JSONDecodeError):
                json.loads(json.dumps(receipt)[:-1])
