"""Local path identity, including real Windows 8.3 names, with mutations."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import filesystem_paths
from path_fixtures import short_path
import test_frozen_source_runner as source_test


class FilesystemPathTests(unittest.TestCase):
    def test_canonical_paths_equate_aliases_but_not_other_subjects(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'child').mkdir()
            def observe():
                self.assertEqual(filesystem_paths.canonical_path(root / 'child/..'),
                                 filesystem_paths.canonical_path(root))
                self.assertNotEqual(filesystem_paths.canonical_path(root / 'other'),
                                    filesystem_paths.canonical_path(root))
            observe()
            with patch.object(filesystem_paths, 'canonical_path', side_effect=Path), self.assertRaises(AssertionError):
                observe()

    def test_source_consumer_uses_the_same_subject_through_real_short_paths(self):
        with tempfile.TemporaryDirectory(prefix='long source path fixture ') as temp:
            short = short_path(Path(temp))
            with patch.object(tempfile, 'tempdir', str(short)):
                def run():
                    result = unittest.TestResult()
                    source_test.SourceRunnerTests('test_consumer_uses_extracted_reviewed_bytes_and_receipt_rejects_tool_failure').run(result)
                    return result
                result = run()
                self.assertEqual(result.testsRun, 1)
                self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
                self.assertFalse(result.skipped)
                with patch.object(source_test, 'canonical_path', side_effect=Path):
                    mutated = run()
                    self.assertTrue(mutated.failures)
                    self.assertFalse(mutated.errors)
