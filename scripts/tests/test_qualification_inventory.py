"""The closed public inventories follow the evidence the campaign produces."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from fuzz_parsers import TARGETS, boundary_seeds
from package_generated import GENERATED_MEMBERS


def replay_logs():
    """One replay log per initial seed, as fuzz_parsers names them."""
    names = set()
    for target in TARGETS:
        with tempfile.TemporaryDirectory() as temporary:
            corpus = Path(temporary) / 'corpus'
            shutil.copytree(ROOT / 'fuzz/seeds' / target, corpus)
            boundary_seeds(target, corpus)
            names |= {f'evidence/gates/fuzz/{target}/replay-{path.name}.log' for path in corpus.iterdir()}
    return names


class QualificationInventoryTests(unittest.TestCase):
    def setUp(self):
        self.files = set(json.loads((ROOT / 'scripts/package_files.json').read_text(encoding='utf-8'))['qualification'])
        self.generated = set(GENERATED_MEMBERS['qualification'])

    def test_every_seed_replay_log_is_inventoried_in_both_lists(self):
        logs = replay_logs()
        self.assertTrue(logs)
        self.assertLessEqual(logs, self.files)
        self.assertLessEqual(logs, self.generated)

    def test_no_replay_log_without_a_seed(self):
        listed = {name for name in self.files | self.generated if '/replay-' in name}
        self.assertEqual(listed, replay_logs())

    def test_removed_performance_gate_is_not_expected(self):
        self.assertFalse({n for n in self.files | self.generated if n.startswith('evidence/gates/performance/')})


if __name__ == '__main__':
    unittest.main()
