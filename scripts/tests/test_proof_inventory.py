import ast
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_proof_inventory as gate
from proof_sources import passed_names


class ProofInventoryTests(unittest.TestCase):
    def test_current_checkout(self):
        self.assertEqual(gate.check()['kind'], 'static_source_inventory')

    def mutate(self, path, before, after, message):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('scripts', '.github', 'fuzz'):
                shutil.copytree(gate.ROOT / name, root / name,
                                ignore=shutil.ignore_patterns('target', '__pycache__'))
            for name in ('docker-compose.yml', 'compose.extended.yml',
                         'crates/plenora-storage-engine/Cargo.toml',
                         'crates/plenora-storage-core/src/runtime.rs'):
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(gate.ROOT / name, target)
            target = root / path
            text = target.read_text(encoding='utf-8')
            self.assertIn(before, text)
            target.write_text(text.replace(before, after), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, message):
                gate.check(root)

    def test_new_provider_requires_fixture_and_sdk_probes(self):
        self.mutate('scripts/qualify_python.py', "'s3': {'endpoint':", "'unwired': {'endpoint':", "qualify_python.py")

    def test_removed_python_matrix_version_is_rejected(self):
        self.mutate('.github/workflows/release-candidate.yml', "'3.13', '3.14'", "'3.13'", "Python versions")

    def test_removed_fixture_probe_is_rejected(self):
        self.mutate('scripts/check_fixtures.py', "'smb': lambda:", "'unknown': lambda:", "fixture application probes")

    def test_removed_pr_audit_is_rejected(self):
        self.mutate('.github/workflows/ci.yml', 'python3 scripts/check_proof_inventory.py', 'echo omitted', 'required command')

    def test_stale_packaged_evidence_is_rejected(self):
        self.mutate('scripts/package_generated.py', 'evidence/gates/fuzz/s3_xml/run.log', 'evidence/gates/fuzz/obsolete/run.log', 'packaged fuzz evidence')

    def test_manual_workflow_stale_option_is_rejected_on_pr(self):
        self.mutate('.github/workflows/reliability.yml', '--rounds', '--obsolete-rounds', 'workflow arguments')

    def test_probe_names_follow_producer_loops_without_running_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'producer.py').write_text("raise RuntimeError('must not run')\nfor p in ['a', 'b']:\n    passed(p + '_ok')\n")
            self.assertEqual(passed_names('producer.py', root), {'a_ok', 'b_ok'})
