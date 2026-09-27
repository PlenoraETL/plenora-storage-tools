import io
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from package_source import extract


class SourcePackageTests(unittest.TestCase):
    def test_valid_crate_extracts_through_noncanonical_destination(self):
        with tempfile.TemporaryDirectory(prefix='storage archive alias ') as temporary:
            root = Path(temporary)
            archive = root / 'example-1.0.0.crate'
            content = b'[package]\nname="example"\nversion="1.0.0"\n'
            with tarfile.open(archive, 'w:gz') as stream:
                member = tarfile.TarInfo('example-1.0.0/Cargo.toml')
                member.size = len(content)
                stream.addfile(member, io.BytesIO(content))
            destinations = [root / 'nested' / '..' / 'output']
            if os.name == 'nt':
                import ctypes
                buffer = ctypes.create_unicode_buffer(32768)
                length = ctypes.windll.kernel32.GetShortPathNameW(str(root), buffer, len(buffer))
                self.assertGreater(length, 0)
                destinations.append(Path(buffer.value) / 'short-output')
            for destination in destinations:
                extracted = extract(archive, destination, archive.stem)
                self.assertEqual(extracted, destination.resolve() / archive.stem)
                self.assertEqual((extracted / 'Cargo.toml').read_bytes(), content)

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
