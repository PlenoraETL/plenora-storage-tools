import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_rust_api import differences
from check_api_metadata import snapshot, ROOT


class ApiSnapshotTests(unittest.TestCase):
    def test_rust_comparison_retains_duplicate_items_and_ignores_only_order(self):
        self.assertFalse(differences('pub fn a()\npub fn b()\n', 'pub fn b()\npub fn a()\n'))
        self.assertTrue(differences('pub fn a()\npub fn a()\n', 'pub fn a()\n'))
        self.assertTrue(differences('pub fn a() -> u32\n', 'pub fn a() -> u64\n'))

    def test_api_requirements_and_contracts_match(self):
        self.assertEqual(snapshot(), json.loads((ROOT / 'api/metadata.json').read_text()))
