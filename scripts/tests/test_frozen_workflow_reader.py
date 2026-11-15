"""Behavioral contracts of the classifier's shared, dependency-free reader."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_release_guard_coverage as reader
import frozen_plan


FIXTURE = '''on:
  push:
jobs:
  check:
    strategy:
      fail-fast: false
      matrix:
        os: [linux, 'windows', "a,b"]
    timeout-minutes: 5
    steps:
      - run: |
          echo '# literal shell comment'
            echo nested

          echo end
        env:
          VALUE: 'it''s # literal'
      - uses: action@pin # annotation
        with:
          merge-multiple: true
          name: ${{ matrix.os }}
'''


class WorkflowReaderTests(unittest.TestCase):
    def test_values_and_literal_shell_bytes_survive_structured_read(self):
        document = reader.parse_workflow(FIXTURE)
        self.assertEqual(document['on'], {'push': None})
        job = document['jobs']['check']
        self.assertEqual(job['strategy'], {'fail-fast': False, 'matrix': {'os': ['linux', 'windows', 'a,b']}})
        self.assertEqual(job['timeout-minutes'], 5)
        self.assertEqual(job['steps'], [
            {'run': "echo '# literal shell comment'\n  echo nested\n\necho end\n",
             'env': {'VALUE': "it's # literal"}},
            {'uses': 'action@pin', 'with': {'merge-multiple': True, 'name': '${{ matrix.os }}'}}])
        for marker, expected in [('|-', 'echo end'), ('|+', 'echo end\n')]:
            self.assertTrue(reader.parse_workflow(FIXTURE.replace('run: |', 'run: ' + marker))
                            ['jobs']['check']['steps'][0]['run'].endswith(expected))

    def test_duplicate_keys_cannot_silently_replace_obligations(self):
        cases = [FIXTURE + 'jobs:\n  other:\n    runs-on: linux\n',
                 FIXTURE.replace('timeout-minutes: 5', 'timeout-minutes: 5\n    timeout-minutes: 6'),
                 FIXTURE.replace('        env:', '        run: bypass\n        env:')]
        for source in cases:
            with self.subTest(source=source), self.assertRaises(ValueError):
                reader.parse_workflow(source)

    def test_unsupported_syntax_is_rejected_instead_of_losing_meaning(self):
        for value in ('&anchor linux', '*anchor', '!tag linux', '>\n      folded',
                      '{a: b}', '~', '[a,,b]', '[[a]]', '["a" trailing]',
                      '"unterminated', '0x10', '1.5', '01'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                reader.parse_workflow('jobs:\n  check:\n    runs-on: ' + value + '\n')
        for source in ('jobs:\n\tcheck: linux\n',
                       'jobs:\n  check:\n    run: |\n        first\n      invalid\n',
                       'jobs:\n  check:\n    runs-on: linux\n      continuation\n'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                reader.parse_workflow(source)

    def test_classifier_and_frozen_plan_use_the_same_reader(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / '.github/workflows'
            folder.mkdir(parents=True)
            (folder / 'fixture.yml').write_text(FIXTURE, encoding='utf-8')
            with patch.object(reader, 'ROOT', root), patch.object(reader, 'parse_workflow', wraps=reader.parse_workflow) as parse:
                raw = reader.jobs('fixture.yml')
                structured = frozen_plan.workflows(root)
            self.assertEqual(parse.call_count, 2)
            self.assertEqual(raw, reader.parse_workflow(FIXTURE).raw_jobs)
            self.assertEqual(structured, {'fixture.yml': reader.parse_workflow(FIXTURE)})
        # Reading all current workflows exercises the same dialect as release CI.
        documents = frozen_plan.workflows(reader.ROOT)
        for name, doc in documents.items():
            self.assertEqual(set(reader.jobs(name)), set(doc['jobs']))


if __name__ == '__main__':
    unittest.main()
