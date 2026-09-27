import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release_publication as publication
from versioning import workspace_version


class PublicationTests(unittest.TestCase):
    def test_roundtrip_inventory_rejects_changed_bytes_extra_assets_and_stale_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            version = workspace_version().native
            names = [f'plenora-storage-{version}-source.tar.gz', f'plenora-storage-{version}-qualification.tar.gz']
            platforms = []
            for target in publication.TARGETS:
                names.append(f'plenora-storage-{version}-{target}' + ('.zip' if 'windows' in target else '.tar.gz'))
                wheel = f'wheel-{target}.whl'
                names.append(wheel)
                platforms.append(dict(target=target, wheel=wheel, wheel_sha256=hashlib.sha256(b'artifact').hexdigest()))
            for name in names:
                (root / name).write_bytes(b'artifact')
            receipt = dict(version=version, source_revision='a' * 40, schema_version=2,
                           status='qualified_for_publication', platforms=platforms,
                           additional_gates=[{'name': 'fixture', 'sha256': 'b' * 64}])
            receipt_path = root / 'release-qualification.json'
            receipt_path.write_text(json.dumps(receipt))
            names.append(receipt_path.name)
            index = dict(version=version, files={name: publication.digest(root / name) for name in names})
            index_path = root / 'publication-index.json'
            index_path.write_text(json.dumps(index))
            with patch.object(publication.subprocess, 'check_output', return_value='a' * 40):
                publication.check_files(root)
                (root / names[0]).write_bytes(b'changed')
                with self.assertRaises(ValueError):
                    publication.check_files(root)
                (root / names[0]).write_bytes(b'artifact')
                (root / 'extra').write_bytes(b'artifact')
                index['files']['extra'] = publication.digest(root / 'extra')
                index_path.write_text(json.dumps(index))
                with self.assertRaises(ValueError):
                    publication.check_files(root)
                del index['files']['extra']
                receipt['status'] = 'RUNNING'
                receipt_path.write_text(json.dumps(receipt))
                index['files'][receipt_path.name] = publication.digest(receipt_path)
                index_path.write_text(json.dumps(index))
                with self.assertRaises(ValueError):
                    publication.check_files(root)

    def test_only_qualified_files_can_enter_public_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / publication.TARGETS[0]
            folder.mkdir()
            artifact = folder / 'fixture.bin'
            artifact.write_bytes(b'artifact')
            (folder / 'release-manifest.json').write_text(json.dumps({'artifacts': [
                {'name': artifact.name, 'sha256': publication.digest(artifact)}]}))
            (folder / 'SHA256SUMS').write_text('fixture')
            (root / 'SHA256SUMS').write_text('fixture')
            (root / 'unrelated-private-file').write_text('must not be packaged')
            receipt = dict(status='qualified_for_publication', schema_version=2,
                           platforms=[{'target': publication.TARGETS[0], 'evidence': []}], additional_gates=[])
            (root / 'release-qualification.json').write_text(json.dumps(receipt))
            selected = publication.qualified_files(root)
            self.assertIn(f'{publication.TARGETS[0]}/fixture.bin', selected)
            self.assertNotIn('unrelated-private-file', selected)
            artifact.write_bytes(b'changed')
            with self.assertRaises(ValueError):
                publication.qualified_files(root)

    def test_already_published_release_cannot_be_overwritten(self):
        with patch.object(publication, 'check_tag'), patch.object(publication.subprocess, 'check_output',
                  return_value=json.dumps({'isDraft': False, 'tagName': 'v1.0.0'})):
            with self.assertRaises(ValueError):
                publication.draft('v1.0.0')
