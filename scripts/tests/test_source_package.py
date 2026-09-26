import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from package_source import extract


class SourcePackageTests(unittest.TestCase):
    def test_escaping_names_and_links_are_rejected_before_extraction(self):
        for name, kind in [('source/../../escape', tarfile.REGTYPE),
                           ('/absolute/path', tarfile.REGTYPE),
                           ('source/drive:stream', tarfile.REGTYPE),
                           ('source/link', tarfile.SYMTYPE),
                           ('source/link', tarfile.LNKTYPE)]:
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive = root / 'source.tar'
                with tarfile.open(archive, 'w') as stream:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = '../../escape'
                    stream.addfile(member, io.BytesIO())
                with self.assertRaises(ValueError):
                    extract(archive, root / 'output', 'source')
                self.assertFalse((root / 'escape').exists())

    def test_duplicate_member_cannot_replace_first_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / 'source.tar'
            with tarfile.open(archive, 'w') as stream:
                for content in [b'original', b'replacement']:
                    member = tarfile.TarInfo('source/Cargo.toml')
                    member.size = len(content)
                    stream.addfile(member, io.BytesIO(content))
            with self.assertRaises(ValueError):
                extract(archive, root / 'output', 'source')
            self.assertEqual((root / 'output/source/Cargo.toml').read_bytes(), b'original')
