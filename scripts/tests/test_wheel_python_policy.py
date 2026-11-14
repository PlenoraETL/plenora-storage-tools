from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_installed_sdk import ROOT, verify_wheel_policy
from versioning import workspace_version


class WheelPythonPolicyTests(unittest.TestCase):
    def test_rejects_obsolete_abi_metadata_and_identity_before_installation(self):
        minimum = (ROOT / '.python-version').read_text().strip()
        version = workspace_version().python
        abi = 'cp' + minimum.replace('.', '') + '-abi3-win_amd64'
        with tempfile.TemporaryDirectory() as directory:
            wheel = Path(directory) / f'plenora_storage-{version}-{abi}.whl'
            metadata = f'Name: plenora-storage\nVersion: {version}\nRequires-Python: >={minimum}\n'
            tag = f'Tag: {abi}\n'
            variants = [(metadata, tag, False),
                        (metadata.replace('>=' + minimum, '>=3.9'), tag, True),
                        (metadata + 'Requires-Python: >=3.9\n', tag, True),
                        (metadata.replace(version, '0.0.0'), tag, True),
                        (metadata, 'Tag: cp39-abi3-win_amd64\n', True),
                        (metadata, '', True)]
            for meta, tags, rejected in variants:
                with self.subTest(metadata=meta, tags=tags):
                    with zipfile.ZipFile(wheel, 'w') as archive:
                        prefix = f'plenora_storage-{version}.dist-info/'
                        archive.writestr(prefix + 'METADATA', meta)
                        archive.writestr(prefix + 'WHEEL', tags)
                    if rejected:
                        with self.assertRaises(ValueError):
                            verify_wheel_policy(wheel)
                    else:
                        verify_wheel_policy(wheel)
