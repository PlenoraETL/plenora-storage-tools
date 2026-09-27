import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_webdav_fixture import validate_report


class WebDavFixtureEvidenceTests(unittest.TestCase):
    def test_rejects_incomplete_or_foreign_campaigns(self):
        report = {'status': 'PASS', 'source_revision': 'a' * 40, 'dirty': False, 'workers': 8,
                  'results': [{'statuses': [201] + [412] * 7, 'winner_preserved': True} for _ in range(30)]}
        validate_report(report, 'a' * 40)
        for field, value in [('status', 'RUNNING'), ('source_revision', 'b' * 40),
                             ('dirty', True), ('workers', 1), ('results', report['results'][:-1])]:
            changed = copy.deepcopy(report)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_report(changed, 'a' * 40)
        for statuses, preserved in [([201, 201] + [412] * 6, True),
                                    ([201] + [500] * 7, True), ([201] + [412] * 7, False)]:
            changed = copy.deepcopy(report)
            changed['results'][0] = {'statuses': statuses, 'winner_preserved': preserved}
            with self.subTest(statuses=statuses, preserved=preserved), self.assertRaises(ValueError):
                validate_report(changed, 'a' * 40)
