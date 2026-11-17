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

    def test_workflow_checks_run_before_any_tag_or_draft(self):
        """A bundle that qualifies but fails what the release workflow repeats
        (assembly with closed inventories, smoke, attestations) stops locally."""
        import publish_qualified as module

        class Done(Exception):
            pass

        for failing in ('assemble', 'smoke', 'attestations'):
            with self.subTest(failing=failing), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                bundle = root / 'qualification-input.tar.gz'
                bundle.write_bytes(b'')
                notes = root / 'notes.md'
                notes.write_text('candidate')
                order = []

                def step(name):
                    def run(*args, **kwargs):
                        order.append(name)
                        if name == failing:
                            raise ValueError('workflow check failed')
                    return run

                def read_only(command, **kwargs):
                    if command == ('git', 'rev-parse', 'HEAD'):
                        return 'a' * 40
                    if command == ('git', 'status', '--porcelain'):
                        return ''
                    if command[:2] == ('git', 'ls-remote'):
                        return 'a' * 40 + '\trefs/heads/main'
                    raise Done('reached a tag, draft or GitHub request')

                qualified = type('Result', (), {'returncode': 0})()
                with patch.object(module, 'extract', return_value=root), \
                        patch.object(module.subprocess, 'run', return_value=qualified), \
                        patch.object(module.subprocess, 'check_output', side_effect=read_only), \
                        patch.object(module, 'assemble', side_effect=step('assemble')), \
                        patch.object(module, 'smoke', side_effect=step('smoke')), \
                        patch.object(module, 'attestations', side_effect=step('attestations')):
                    with self.assertRaisesRegex(ValueError, 'no tag or draft created'):
                        module.publish(bundle, root / 'publication', 'example/repository', notes)
                expected = ['assemble', 'smoke', 'attestations']
                self.assertEqual(order, expected[:expected.index(failing) + 1])
