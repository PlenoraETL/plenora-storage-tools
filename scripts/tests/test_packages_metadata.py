import bz2
import gzip
import io
import lzma
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_packages import archive, binary_rule, gate


class HiddenMetadataRegressionTests(unittest.TestCase):
    def test_complete_crate_rejects_hidden_tar_extensions(self):
        name = 'plenora-storage-core-1.2.3.crate'
        _, prefix, expected = gate.distribution_policy(name)
        pem = '-----BEGIN PRIVATE KEY-----\nYWJj\n'
        for extension in ('comment', 'global', 'gnu-name', 'gnu-link'):
            for value in ('harmless', pem):
                out = io.BytesIO()
                with tarfile.open(fileobj=out, mode='w', format=tarfile.PAX_FORMAT,
                                  pax_headers={'comment': value} if extension == 'global' else None) as stream:
                    if extension.startswith('gnu'):
                        header = tarfile.TarInfo('././@LongLink')
                        header.type = tarfile.GNUTYPE_LONGNAME if extension == 'gnu-name' else tarfile.GNUTYPE_LONGLINK
                        payload = value.encode() + b'\0'
                        header.size = len(payload)
                        stream.addfile(header, io.BytesIO(payload))
                    for index, member in enumerate(expected):
                        header = tarfile.TarInfo(prefix + member)
                        header.size = 4
                        if index == 0 and extension == 'comment':
                            header.pax_headers = {'comment': value}
                        stream.addfile(header, io.BytesIO(b'safe'))
                with self.subTest(extension=extension, private=value == pem), self.assertRaises(ValueError):
                    gate.scan_distribution(gzip.compress(out.getvalue()), name)

    def test_raw_stream_checks_padding_trailers_and_zip_comments(self):
        pem = b'-----BEGIN PRIVATE KEY-----\nYWJj\n'
        raw = gzip.decompress(archive([('a', b'a')]))
        for offset in (513, len(raw)):
            candidate = raw[:offset] + pem + raw[offset+len(pem):]
            for compress in (lambda b: b, gzip.compress, bz2.compress, lzma.compress):
                with self.subTest(offset=offset, compress=compress), self.assertRaisesRegex(ValueError, 'padding|terminator'):
                    gate.members(compress(candidate))
        out = io.BytesIO(archive([('a', b'a')], True))
        with zipfile.ZipFile(out, 'a') as stream:
            stream.comment = pem
        with self.assertRaisesRegex(ValueError, 'uncommented EOCD'):
            gate.members(out.getvalue())

    def test_deflated_zip_members_are_scanned_after_expansion(self):
        from test_packages import BuildOutputTests
        name = 'plenora_storage-1.2.3-cp310-abi3-win_amd64.whl'
        _, _, expected = gate.distribution_policy(name)
        for private_native in (True, False):
            out = io.BytesIO()
            with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED) as stream:
                for member in expected:
                    native = gate.native_member('cp310-abi3-win_amd64', member)
                    data = BuildOutputTests.native('x86_64-pc-windows-msvc', True) if native else b'safe'
                    if bool(native) == private_native:
                        data += b'-----BEGIN PRIVATE KEY-----\nYWJj\n'
                    stream.writestr(member, data)
            if private_native:
                # STOR-LIMIT-004: native payloads are deliberately opaque.
                gate.scan_distribution(out.getvalue(), name)
            else:
                with self.assertRaisesRegex(ValueError, 'private PEM'):
                    gate.scan_distribution(out.getvalue(), name)


    def test_closed_pax_keys_and_long_paths(self):
        name = 'directory/' + 'a' * 120
        for headers in ({'path': name, 'linkpath': '', 'size': '4', 'mtime': '1.25',
                         'uid': '0', 'gid': '0', 'uname': 'builder', 'gname': 'builder'},
                        {'linkpath': 'elsewhere'}, {'SCHILY.xattr.test': 'value'}):
            out = io.BytesIO()
            with tarfile.open(fileobj=out, mode='w') as stream:
                item = tarfile.TarInfo(name)
                item.size, item.pax_headers = 4, headers
                stream.addfile(item, io.BytesIO(b'safe'))
            if 'path' in headers:
                self.assertEqual(gate.members(out.getvalue()), [(name, b'safe')])
            else:
                with self.assertRaises(ValueError):
                    gate.members(out.getvalue())

    def test_global_pax_even_with_allowed_keys_is_rejected(self):
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode='w', pax_headers={'mtime': '0'}) as stream:
            stream.addfile(tarfile.TarInfo('a'))
        with self.assertRaisesRegex(ValueError, 'forbidden tar extension'):
            gate.members(out.getvalue())


class CanonicalRuleRegressionTests(unittest.TestCase):
    def test_same_rule_across_verification_paths(self):
        for spelling, canonical in [('README.md', 'readme.md'), ('readme.md', 'README.md'),
                                    ('ReAdMe.MD', 'readme.md'), ('R\u00c9SUM\u00c9.md', 're\u0301sume\u0301.md')]:
            good = b'original text'
            rule = binary_rule(good)
            crate = 'plenora-example'
            policy = {'source': [spelling], 'qualification': [spelling],
                      'public-documents': [spelling], 'crates': {crate: [spelling]},
                      'member_rules': {kind: {canonical: rule} for kind in
                                       ('source', 'qualification', 'public-documents', 'crates/' + crate)}}
            with tempfile.TemporaryDirectory() as temporary, patch.object(gate, 'registry', return_value=policy), patch.object(gate, 'tracked_paths', return_value={}):
                path = Path(temporary) / spelling
                for data in (good, b'changed text'):
                    path.write_bytes(data)
                    packed = archive([('plenora-example-1.2.3/' + spelling, data)])
                    crate_path = Path(temporary) / 'plenora-example-1.2.3.crate'
                    crate_path.write_bytes(packed)
                    checks = [lambda: gate.verify_distribution(path),
                              lambda: gate.verify_distribution(path, member=canonical),
                              lambda: gate.scan_entries([(spelling, data)], {canonical: rule}, 0),
                              lambda: gate.scan_distribution(packed, crate_path.name),
                              lambda: gate.verify_distribution(crate_path),
                              lambda: gate.check_crate(crate_path, crate, '1.2.3'),
                              lambda: gate.check_archive(crate_path, 'crates/' + crate),
                              lambda: gate.scan_distribution(archive([('plenora-storage-1.2.3/' + spelling, data)]),
                                                             'plenora-storage-1.2.3-source.tar.gz')]
                    for index, check in enumerate(checks):
                        with self.subTest(spelling=spelling, changed=data != good, entry=index):
                            if data == good:
                                check()
                            else:
                                with self.assertRaisesRegex(ValueError, 'digest'):
                                    check()

    def test_template_rules_are_preserved_and_expansion_collisions_rejected(self):
        rule = binary_rule(b'safe')
        policy = {'qualification': ['file-{version}'],
                  'member_rules': {'qualification': {'FILE-{version}': rule}}}
        self.assertIs(gate.path_lookup(gate.member_rules(policy, 'qualification'), 'file-{version}'), rule)
        self.assertIs(gate.path_lookup(gate.member_rules(policy, 'qualification', '1.2.3'), 'FILE-1.2.3'), rule)
        policy['qualification'].append('file-1.2.3')
        with self.assertRaises(ValueError):
            gate.member_rules(policy, 'qualification', '1.2.3')


    def test_lookup_missing_and_colliding_keys_fail_closed(self):
        for mapping, key in [({'README.md': {}}, 'missing'), ({'A': {}, 'a': {}}, 'a')]:
            with self.assertRaises(ValueError):
                gate.path_lookup(mapping, key)
        for context, item in gate.NATIVE_DISTRIBUTIONS.items():
            if item[1]:
                self.assertEqual(gate.native_member(context.upper(), item[1].upper()), item)


class BuilderRuleRegressionTests(unittest.TestCase):
    def test_cargo_listing_uses_same_normalized_digest(self):
        import json
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'README.md'
            policy = {'crates': {'plenora-example': ['README.md']},
                      'member_rules': {'crates/plenora-example': {'readme.md': binary_rule(b'original')}}}
            metadata = {'packages': [{'name': 'plenora-example', 'manifest_path': str(root / 'Cargo.toml')}]}
            def cargo(*args):
                return json.dumps(metadata) if args[0] == 'metadata' else 'README.md'
            with patch.object(gate, 'registry', return_value=policy), patch.object(gate, 'cargo', side_effect=cargo), \
                    patch.object(gate, 'check_source_digests'), patch.object(gate, 'tracked_paths', return_value={}):
                path.write_bytes(b'original')
                gate.check_cargo()
                path.write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, 'digest'):
                    gate.check_cargo()

    def test_source_builder_removes_inherited_git_global_comment(self):
        import package_source
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw, packed = root / 'git.tar', root / 'source.tar.gz'
            with tarfile.open(raw, 'w', pax_headers={'comment': 'synthetic commit'}) as stream:
                item = tarfile.TarInfo('source/README.md')
                item.size = 4
                stream.addfile(item, io.BytesIO(b'safe'))
            with patch.object(package_source, 'registry', return_value={'source': ['README.md']}):
                package_source.select_source_archive(raw, packed, 'source')
            self.assertEqual(gate.members(packed.read_bytes()), [('source/README.md', b'safe')])
