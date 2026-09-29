import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campaign_state import Campaign, exclusive
from check_disk_space import GIB, inspect, requirement


class CampaignStateTests(unittest.TestCase):
    def test_lock_excludes_overlapping_controllers_and_is_reusable(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with exclusive(folder):
                with self.assertRaises(OSError):
                    with exclusive(folder):
                        self.fail('overlapping controller acquired the campaign')
            with exclusive(folder):
                self.assertTrue((folder / 'campaign.lock').is_file())

    def test_resume_binds_identity_and_all_successful_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with exclusive(folder):
                campaign = Campaign(folder, {'revision': 'a', 'binary': 'b'})
                output = campaign.phase('transfer', lambda path: (path / 'report.json').write_text('passed'))
                def unexpected(path):
                    self.fail('successful phase ran twice')
                self.assertEqual(Campaign(folder, campaign.state['identity']).phase('transfer', unexpected), output)
                with self.assertRaises(ValueError):
                    Campaign(folder, {'revision': 'a', 'binary': 'changed'})
                (output / 'report.json').write_text('altered')
                with self.assertRaises(ValueError):
                    campaign.phase('transfer', unexpected)

    def test_failures_and_interrupted_attempts_require_recorded_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            campaign = Campaign(Path(temporary), {'subject': 'same'})
            def failure(path):
                (path / 'report.json').write_text('failure')
                raise RuntimeError('private endpoint or callback detail')
            with self.assertRaises(RuntimeError):
                campaign.phase('transfer', failure)
            self.assertNotIn('private', campaign.path.read_text())
            with self.assertRaises(ValueError):
                campaign.phase('transfer', lambda path: None)
            campaign.state['phases']['transfer'][-1]['status'] = 'RUNNING'
            campaign.save()
            with self.assertRaises(ValueError):
                campaign.phase('transfer', lambda path: None)
            result = campaign.phase('transfer', lambda path: (path / 'report.json').write_text('success'),
                                    retry=True, reason='fixture capacity restored')
            self.assertEqual(result.name, '2')
            self.assertEqual((result.parent / '1/report.json').read_text(), 'failure')

    def test_space_headroom_is_reserved_before_payload_and_workers(self):
        self.assertEqual(requirement(100 * GIB, GIB, 1), 14 * GIB)
        self.assertEqual(requirement(10 * GIB, GIB, 2), 16 * GIB)
        from collections import namedtuple
        Usage = namedtuple('Usage', 'total used free')
        with patch('check_disk_space.shutil.disk_usage', return_value=Usage(100 * GIB, 87 * GIB, 13 * GIB)):
            self.assertEqual(inspect({'backend': Path('.')}, GIB, 1)['status'], 'FAIL')
        with patch('check_disk_space.shutil.disk_usage', return_value=Usage(100 * GIB, 86 * GIB, 14 * GIB)):
            self.assertEqual(inspect({'backend': Path('.')}, GIB, 1)['status'], 'PASS')
            self.assertEqual(inspect({'backend': Path('.')}, GIB, 1, spool_uploads=True)['status'], 'FAIL')
        with patch('check_disk_space.shutil.disk_usage', return_value=Usage(100 * GIB, 85 * GIB, 15 * GIB)):
            self.assertEqual(inspect({'backend': Path('.')}, GIB, 1, spool_uploads=True)['status'], 'PASS')
        for values in [(0, 1, 1), (1, 0, 1), (1, 1, 0)]:
            with self.assertRaises(ValueError):
                requirement(*values)
