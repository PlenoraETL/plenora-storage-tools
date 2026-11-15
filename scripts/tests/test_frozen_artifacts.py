"""Independent byte substitutions at the public artifact selection boundary."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release_publication as publication


class FrozenArtifactTests(unittest.TestCase):
    def test_every_target_and_evidence_class_rejects_substituted_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'SHA256SUMS').write_text('synthetic fixture only')
            platforms, bound = [], []
            digest = hashlib.sha256(b'qualified fixture').hexdigest()
            for target in publication.TARGETS:
                folder = root / target
                folder.mkdir()
                (folder / 'SHA256SUMS').write_text('synthetic fixture only')
                (folder / 'release-manifest.json').write_text(json.dumps({
                    'artifacts': [{'name': 'binary', 'sha256': digest}]}))
                platforms.append({'target': target, 'evidence': [
                    {'name': 'qualification.json', 'sha256': digest},
                    {'name': target + '-tests.log', 'sha256': digest}]})
                bound.extend([folder / 'binary', folder / 'qualification.json',
                              root / 'evidence' / (target + '-tests.log')])
            bound.append(root / 'evidence/gates/result.json')
            for path in bound:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'qualified fixture')
            (root / 'release-qualification.json').write_text(json.dumps({
                'status': 'qualified_for_publication', 'schema_version': 2,
                'platforms': platforms, 'additional_gates': [{'name': 'result.json', 'sha256': digest}]}))
            selected = publication.qualified_files(root)
            self.assertTrue({p.relative_to(root).as_posix() for p in bound} <= set(selected))
            for path in bound:
                with self.subTest(artifact=path.relative_to(root).as_posix()):
                    path.write_bytes(b'foreign run: PASS')
                    try:
                        with self.assertRaises(ValueError):
                            publication.qualified_files(root)
                    finally:
                        path.write_bytes(b'qualified fixture')
            self.assertEqual(publication.qualified_files(root), selected)
