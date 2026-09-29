from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from publish_qualified import publish


class PublicationCampaignTests(unittest.TestCase):
    def test_unqualified_input_cannot_create_tags_drafts_or_dispatch_workflows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / 'qualification-input.tar.gz'
            with tarfile.open(bundle, 'w:gz'):
                pass
            notes = root / 'notes.md'
            notes.write_text('candidate')
            calls = []
            def read_only(command, **kwargs):
                calls.append(command)
                if command == ('git', 'rev-parse', 'HEAD'):
                    return 'a' * 40
                if command == ('git', 'status', '--porcelain'):
                    return ''
                if command[:2] == ('git', 'ls-remote'):
                    return 'a' * 40 + '\trefs/heads/main'
                self.fail('unqualified bundle reached a mutation or GitHub request')
            with patch('publish_qualified.subprocess.check_output', side_effect=read_only):
                with self.assertRaises((ValueError, FileNotFoundError)):
                    publish(bundle, root / 'publication', 'example/repository', notes)
            self.assertTrue(calls)
