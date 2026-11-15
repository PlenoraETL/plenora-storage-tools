"""Deterministic compression boundaries; no toolchain or external service."""
import bz2
import gzip
import hashlib
import io
import lzma
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import zipfile
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import package_classifier as classifier
import check_packages as gate
from test_packages import archive
import test_packages as fixtures


class CompressionTests(unittest.TestCase):
    def rejected(self, action):
        try:
            action()
        except Exception as error:
            # Native decoder exceptions are test failures, not mutation ERRORs.
            self.assertIs(type(error), ValueError)
            self.assertEqual(str(error), 'invalid compression stream')
            self.assertTrue(error.__suppress_context__)
        else:
            self.fail('corrupt stream accepted')

    def test_mtime_bytes_and_corrupt_streams(self):
        payload = gzip.decompress(archive([('member', b'ordinary public text')]))
        packed = gzip.compress(payload, mtime=0)
        # Each possible byte at each mtime position, plus recognizable signatures.
        stamps = {value << (8 * position) for position in range(4) for value in range(256)}
        stamps.update(int.from_bytes(signature.ljust(4, b'\0'), 'little')
                      for signature in (b'\x1f\x8b', b'BZh', b'\xfd7zX', b'\x05\x04', b'0\x82'))
        for stamp in sorted(stamps):
            with self.subTest(mtime=stamp):
                candidate = packed[:4] + stamp.to_bytes(4, 'little') + packed[8:]
                self.assertEqual(classifier.expand(candidate, 'gzip'), payload)
                self.assertEqual(gate.members(candidate), [('member', b'ordinary public text')])
                self.assertIsNone(classifier.container_format(b'prefix' + candidate))
                corrupt = bytearray(candidate)
                corrupt[-8] ^= 1  # gzip payload CRC, independent of header/time
                self.rejected(lambda: classifier.expand(corrupt, 'gzip'))
        for kind, compress in (('bzip2', bz2.compress), ('xz', lzma.compress)):
            good = compress(payload)
            self.assertEqual(classifier.expand(good, kind), payload)
            corrupt = bytearray(good)
            corrupt[len(corrupt)//2] ^= 0xff
            with self.subTest(kind=kind):
                self.rejected(lambda: classifier.expand(corrupt, kind))

    def test_zip_deflate_uses_same_failure_boundary(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as stream:
            stream.writestr(zipfile.ZipInfo('member'), b'ordinary text',
                            compress_type=zipfile.ZIP_DEFLATED)
        good = output.getvalue()
        self.assertEqual(gate.members(good), [('member', b'ordinary text')])
        with zipfile.ZipFile(io.BytesIO(good)) as stream:
            begin, _ = classifier.validate_zip(good, stream)['member']
        corrupt = bytearray(good)
        corrupt[begin] = 7  # final DEFLATE block with reserved BTYPE=3
        self.rejected(lambda: gate.members(corrupt))

    def test_archive_fixtures_do_not_read_wall_clock(self):
        for zipped in (False, True):
            with patch('time.time', return_value=1700000000):
                first = archive([('member', b'ordinary text')], zipped)
            with patch('time.time', return_value=1800000000):
                second = archive([('member', b'ordinary text')], zipped)
            self.assertEqual(first, second)
            self.assertEqual(gate.members(first), [('member', b'ordinary text')])

    def test_zip_library_decode_errors_use_the_same_boundary(self):
        good = archive([('member', b'ordinary text')], True)
        self.assertEqual(gate.members(good), [('member', b'ordinary text')])
        with patch.object(zipfile.ZipFile, 'read', side_effect=zlib.error('private decoder detail')):
            self.rejected(lambda: gate.members(good))

    def test_cli_deflate_date_bytes_preserve_streams_and_native_ranges(self):
        from fuzz_parsers import synthetic_ccache
        native = fixtures.BuildOutputTests.native('x86_64-pc-windows-msvc') + synthetic_ccache(0x0504)[:48]
        source = gzip.compress(b'ordinary public text', mtime=0)
        rule = {'source': 'public.gz', 'sha256': hashlib.sha256(source).hexdigest(),
                'container': {'format': 'gzip', 'payload': {}}}
        policy = {'cli-windows': ['plenora-storage.exe', 'public.gz'],
                  'member_rules': {'cli-windows': {'public.gz': rule}}}
        def pack(payload):
            output = io.BytesIO()
            with zipfile.ZipFile(output, 'w') as stream:
                for name, data in [('plenora-storage.exe', native), ('public.gz', payload)]:
                    stream.writestr(zipfile.ZipInfo(name), data, compress_type=zipfile.ZIP_DEFLATED)
            return output.getvalue()
        good = pack(source)
        with zipfile.ZipFile(io.BytesIO(good)) as stream:
            ranges = classifier.validate_zip(good, stream)
            # DOS time/date: local header +10; central header +12.
            offsets = [item.header_offset + 10 for item in stream.infolist()]
            cursor = stream.start_dir
            for item in stream.infolist():
                offsets.append(cursor + 12)
                cursor += 46 + len(item.filename.encode())
        dates = {value << (8 * position) for position in range(4) for value in range(256)}
        dates.update((0x8b1f0405, 0x218b1f, 0x425a6800, 0x30820504))
        name = 'plenora-storage-1.2.3-x86_64-pc-windows-msvc.zip'
        with patch.object(gate, 'registry', return_value=policy), \
                patch.object(gate, 'tracked_paths', return_value={'public.gz': 'public.gz'}), \
                patch.object(gate, 'checkout_source_bytes', side_effect=lambda _: source):
            for date in sorted(dates):
                with self.subTest(dos_time_date=hex(date)):
                    changed = bytearray(good)
                    for offset in offsets:
                        changed[offset:offset+4] = date.to_bytes(4, 'little')
                    candidate = bytes(changed)
                    # Exact failing sequence: first scan, then the same bytes with a spy.
                    gate.scan_distribution(candidate, name)
                    with patch.object(gate, 'check_content', wraps=gate.check_content) as scan:
                        gate.scan_distribution(candidate, name)
                    scanned = [call.args[0] for call in scan.call_args_list]
                    begin, end = ranges['plenora-storage.exe']
                    self.assertIn(candidate[:begin], scanned)
                    self.assertIn(candidate[end:], scanned)
                    self.assertEqual(dict(gate.members(candidate, name))['public.gz'], source)
            # A valid outer ZIP cannot promote an invalid nested gzip, even with
            # matching source authority, ZIP CRC and policy digest.
            corrupt = bytearray(source)
            corrupt[-8] ^= 1
            source = bytes(corrupt)
            rule['sha256'] = hashlib.sha256(source).hexdigest()
            self.rejected(lambda: gate.scan_distribution(pack(source), name))


if __name__ == '__main__':
    unittest.main()
