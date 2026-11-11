import gzip
import base64
import bz2
import hashlib
import lzma
import io
import json
from pathlib import Path
import sys
import tarfile
import unittest
import zipfile
from unittest.mock import patch
from functools import lru_cache

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_packages as gate


def der(tag, payload):
    size = len(payload)
    length = bytes([size]) if size < 128 else bytes([130]) + size.to_bytes(2, 'big')
    return bytes([tag]) + length + payload


def integer(value):
    return der(2, value.to_bytes((value.bit_length()+8)//8 or 1, 'big'))


def rsa_key():
    # Tiny, deliberately insecure RSA numbers generated as a format test.
    return der(48, b''.join(integer(n) for n in (0, 61*53, 17, 2753, 61, 53, 53, 49, 38)))


def archive(files, zip_format=False):
    files = wheel_record(files) if zip_format else files
    output = io.BytesIO()
    if zip_format:
        with zipfile.ZipFile(output, 'w') as stream:
            for name, data in files:
                stream.writestr(name, data)
    else:
        with tarfile.open(fileobj=output, mode='w:gz') as stream:
            for name, data in files:
                member = tarfile.TarInfo(name)
                member.size = len(data)
                stream.addfile(member, io.BytesIO(data))
    return output.getvalue()


def printable_tar(payload=b'a' * 512):
    """Valid tar, one full block, no NUL padding or end markers."""
    assert len(payload) == 512
    header = bytearray(b' ' * 512)
    header[:100] = b'a' * 100
    header[100:124] = b'0000644 0000000 0000000 '
    header[124:136] = b'00000001000 '
    header[136:148] = b'00000000000 '
    header[156] = ord('0')
    header[257:263] = b'ustar '
    header[345:500] = b'p' * 155
    header[148:156] = f'{sum(header):06o}  '.encode()
    return bytes(header) + payload


def binary_rule(data, container=None):
    rule = {'sha256': hashlib.sha256(data).hexdigest()}
    if container is not None:
        rule['container'] = container
    return rule


def wheel_record(files):
    import csv
    files = list(files)
    records = [n for n, d in files if n.endswith('.dist-info/RECORD')]
    if len(records) != 1:
        return files
    record = records[0]
    out = io.StringIO(newline='')
    csv.writer(out, lineterminator='\n').writerows([
        [n, '' if n == record else 'sha256=' + base64.urlsafe_b64encode(
            hashlib.sha256(d).digest()).rstrip(b'=').decode(), '' if n == record else str(len(d))]
        for n, d in files])
    return [(n, out.getvalue().encode() if n == record else d) for n, d in files]


@lru_cache(maxsize=None)
def cargo_fixture():
    from package_generated import cargo_reference
    return cargo_reference()


@lru_cache(maxsize=None)
def source_fixture(context, name):
    if gate.generated_member(context, name):
        if context.startswith('crates/'):
            if name == '.cargo_vcs_info.json':
                return json.dumps({'git': {'sha1': gate.release_revision()},
                                   'path_in_vcs': context}, indent=2).encode()
            return cargo_fixture()[context[7:]][name]
        if context.startswith('cp'):
            from package_generated import wheel_metadata
            fields, body, version = wheel_metadata(context)
            if name.endswith('/METADATA'):
                return ('\n'.join(k + ': ' + v for k, v in fields) + '\n\n' + body).encode()
            if name.endswith('/WHEEL'):
                return ('Wheel-Version: 1.0\nGenerator: maturin (' + version + ')\n'
                        'Root-Is-Purelib: false\nTag: ' + context + '\n').encode()
            if name.endswith('/RECORD'):
                return (name + ',,\n').encode()
        return b'safe text'
    source = gate.repository_path(context, name)
    return gate.checkout_source_bytes(gate.path_lookup(gate.tracked_paths(), source))


class GitSnapshotTestCase(unittest.TestCase):
    def setUp(self):
        token = gate._GIT_CACHE.set({})
        self.addCleanup(gate._GIT_CACHE.reset, token)


class PackageGuardTests(GitSnapshotTestCase):
    def test_safe_text_public_certificate_and_native_data(self):
        for data in (b'ordinary source', b'password field documentation',
                     b'MZ' + bytes(range(256)),
                     b'-----BEGIN CERTIFICATE-----\nZGF0YQ==\n-----END CERTIFICATE-----'):
            gate.scan(data, rule={"sha256": hashlib.sha256(data).hexdigest()} if not gate.is_text(data) else {})

    def test_public_digest_info_is_not_an_encrypted_private_key(self):
        sha256 = der(48, der(6, bytes.fromhex('608648016503040201')) + der(5, b''))
        digest_info = der(48, sha256 + der(4, bytes(32)))
        for data in (digest_info, b'MZ prefix' + digest_info + b'suffix'):
            gate.scan(data, rule={'sha256': hashlib.sha256(data).hexdigest()})

    def test_private_pem_variants_are_rejected(self):
        for label in ('PRIVATE KEY', 'RSA PRIVATE KEY', 'EC PRIVATE KEY',
                      'ENCRYPTED PRIVATE KEY', 'OPENSSH PRIVATE KEY'):
            value = ('-----BEGIN ' + label + '-----\nYWJj\n-----END ' + label + '-----').encode()
            with self.subTest(label=label), self.assertRaises(ValueError):
                gate.scan(value, 'innocent.txt')

    def test_der_private_keys_and_encrypted_containers(self):
        algorithm = der(48, der(6, bytes.fromhex('2a864886f70d010101')) + der(5, b''))
        encryption = der(48, der(6, bytes.fromhex('2a864886f70d01050d')) + der(48, b''))
        oid_data = der(6, bytes.fromhex('2a864886f70d010701'))
        containers = [rsa_key(), der(48, integer(1) + der(4, bytes(range(32)))),
                      der(48, integer(0) + algorithm + der(4, rsa_key())),
                      der(48, encryption + der(4, bytes(range(32)))),
                      der(48, integer(3) + der(48, oid_data + der(160, der(4, der(48, b'')))))]
        for data in containers:
            with self.subTest(length=len(data)), self.assertRaises(ValueError):
                gate.scan(data, 'renamed.rs')
        with self.assertRaises(ValueError):
            gate.scan(b'MZ prefix' + rsa_key() + b'suffix', 'app.exe')

    def test_kerberos_ccache_and_keytab_regardless_of_extension(self):
        from fuzz_parsers import synthetic_ccache
        for data in (synthetic_ccache(0x0503), synthetic_ccache(0x0504),
                     bytes([5, 1, 0, 0, 0, 0]), bytes([5, 2, 0, 0, 0, 0])):
            with self.subTest(version=data[1]), self.assertRaises(ValueError):
                gate.scan(data, 'README.md')

    def test_inventory_extra_missing_duplicate_and_unsafe(self):
        gate.compare(['b', 'a'], ['a', 'b'])
        for names in (['a'], ['a', 'b', 'c'], ['a', 'a'], ['../a'], ['/a'], ['a\\b'], ['a/./b']):
            with self.subTest(names=names), self.assertRaises(ValueError):
                gate.compare(names, ['a', 'b'])

    def test_nested_material_and_inventory_are_checked(self):
        for zipped in (True, False):
            nested = archive([('payload.dat', rsa_key())], zipped)
            with self.assertRaises(ValueError):
                gate.scan(archive([('nested.bin', nested)]))
        good = archive([('safe.txt', b'safe')], True)
        rule = binary_rule(good, {'format': 'zip', 'members': {'safe.txt': {}}})
        gate.scan(good, rule=rule)
        bad = archive([('safe.txt', b'safe'), ('extra.txt', b'extra')], True)
        rule['sha256'] = hashlib.sha256(bad).hexdigest()
        with self.assertRaisesRegex(ValueError, 'inventory mismatch'):
            gate.scan(bad, rule=rule)

    def test_compressed_material_and_links_are_rejected(self):
        with self.assertRaises(ValueError):
            gate.scan(gzip.compress(rsa_key()))
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as stream:
            member = tarfile.TarInfo('link')
            member.type = tarfile.SYMTYPE
            member.linkname = 'elsewhere'
            stream.addfile(member)
        with self.assertRaises(ValueError):
            gate.scan(output.getvalue())

    def test_credential_document_and_fixed_samba_password(self):
        public_fixture = b'printf "testpass' + b'\\ntestpass\\n" | smbpasswd -a -s testuser'
        gate.scan(public_fixture)
        for extra in (rsa_key(), b'\n' + b'printf "other\\nother\\n" | smbpasswd -a -s user'):
            with self.subTest(extra=extra[:5]), self.assertRaises(ValueError):
                gate.scan(public_fixture + extra)
        with self.assertRaises(ValueError):
            gate.scan(json.dumps({'password': 'generated-only-in-test'}).encode())
        with self.assertRaises(ValueError):
            gate.scan(b'printf "literal' + b'\\nliteral\\n" | smbpasswd -a -s user')

    def test_each_distribution_has_a_bidirectional_inventory(self):
        cases = [
            ('source', 'plenora-storage-1.2.3-source.tar.gz', 'plenora-storage-1.2.3/'),
            ('qualification', 'plenora-storage-1.2.3-qualification.tar.gz', '1.2.3/'),
            ('contracts', 'plenora-storage-contracts-1.2.3.tar.gz', ''),
            ('cli-windows', 'plenora-storage-1.2.3-x86_64-pc-windows-msvc.zip', ''),
            ('cli-linux', 'plenora-storage-1.2.3-x86_64-unknown-linux-gnu.tar.gz', ''),
            ('wheel-windows', 'plenora_storage-1.2.3-cp310-abi3-win_amd64.whl', ''),
            ('wheel-linux', 'plenora_storage-1.2.3-cp310-abi3-manylinux_2_34_x86_64.whl', ''),
        ]
        for kind, name, prefix in cases:
            with self.subTest(kind=kind), patch.object(gate, 'registry', return_value={kind: ['one', 'two'], 'wheel-tags': gate.registry()['wheel-tags']}), patch.object(gate, 'classify_member'):
                good = [(prefix+'one', b'one'), (prefix+'two', b'two')]
                gate.scan(archive(good, kind in {'cli-windows', 'wheel-windows', 'wheel-linux'}), name)
                for files in (good[:1], good+[(prefix+'extra', b'extra')]):
                    with self.assertRaises(ValueError):
                        gate.scan(archive(files, kind in {'cli-windows', 'wheel-windows', 'wheel-linux'}), name)

    def test_prerelease_qualification_uses_python_version(self):
        name = 'plenora-storage-1.2.3-rc.1-qualification.tar.gz'
        with patch.object(gate, 'registry', return_value={'qualification': ['artifact-{python_version}']}), patch.object(gate, 'classify_member'):
            gate.scan(archive([('1.2.3-rc.1/artifact-1.2.3rc1', b'safe')]), name)

    def test_undeclared_python_distribution_is_rejected(self):
        with self.assertRaises(ValueError):
            gate.scan(archive([('source.py', b'pass')]), 'plenora_storage-1.2.3.tar.gz')

    def test_embedded_cache_and_duplicate_credential_json_are_rejected(self):
        from fuzz_parsers import synthetic_ccache
        with self.assertRaises(ValueError):
            gate.scan(b'MZ-prefix' + synthetic_ccache(0x0504))
        with self.assertRaises(ValueError):
            gate.scan(b'{"password":"one","password":"two"}')

    def test_generated_fuzz_caches_are_deterministic_and_not_tracked(self):
        from fuzz_parsers import synthetic_ccache
        for version in (0x0503, 0x0504):
            self.assertEqual(synthetic_ccache(version), synthetic_ccache(version))
        self.assertFalse((gate.ROOT / 'fuzz/seeds/kerberos_messages/ccache-v4').exists())

    def test_original_readme_bypasses_and_unknown_binary(self):
        dsa = der(48, b''.join(integer(n) for n in (0, 23, 11, 2, 8, 3)))
        inner = archive([('key', rsa_key())])
        for data in (lzma.compress(gzip.decompress(inner)),
                     bz2.compress(gzip.decompress(inner)), dsa, b'unknown\x00binary'):
            with self.subTest(size=len(data)), patch.object(gate, 'registry', return_value={
                    'crates': {'plenora-storage-core': ['README.md']}}):
                package = archive([('plenora-storage-core-3.0.5/README.md', data)])
                with self.assertRaises(ValueError):
                    gate.scan(package, 'plenora-storage-core-3.0.5.crate')
        for data in (dsa, rsa_key()):
            with self.assertRaisesRegex(ValueError, 'base64'):
                gate.scan(base64.b64encode(data))

    def test_digest_is_exact_even_for_changed_text(self):
        data = b'opaque\x00binary'
        gate.scan(data, 'README.md', rule=binary_rule(data))
        for replacement in (data + b'!', b'replaced by text'):
            with self.assertRaisesRegex(ValueError, 'digest'):
                gate.scan(replacement, 'README.md', rule=binary_rule(data))
        for data in (b'\xff', b'hello\x00', 'hello\u0085'.encode()):
            with self.assertRaisesRegex(ValueError, 'digest'):
                gate.scan(data, 'README.md', rule={})

    def test_printable_tar_always_requires_exact_digest(self):
        data = printable_tar()
        changed = printable_tar(b'b' * 512)
        declaration = {'format': 'tar', 'members': {'p' * 155 + '/' + 'a' * 100: {}}}
        for candidate in (data, changed):
            self.assertTrue(all(32 <= byte < 127 for byte in candidate))
            self.assertTrue(gate.is_text(candidate))
            self.assertEqual(gate.container_format(candidate), 'tar')
            with self.assertRaisesRegex(ValueError, 'terminator'):
                gate.members(candidate)
            with self.assertRaisesRegex(ValueError, 'digest'):
                gate.scan(candidate, rule={'container': declaration})
        rule = binary_rule(data, declaration)
        with self.assertRaisesRegex(ValueError, 'terminator'):
            gate.scan(data, rule=rule)
        with self.assertRaisesRegex(ValueError, 'digest'):
            gate.scan(changed, rule=rule)
        with self.assertRaisesRegex(ValueError, 'terminator'):
            gate.scan(changed, rule=binary_rule(changed, declaration))

    def test_declared_limit_hex_private_key_is_not_detected(self):
        # STOR-LIMIT-003: documents non-detection, NOT guard protection.
        key = rsa_key()
        self.assertTrue(gate.private_der(key))
        gate.scan_entries([('admitted.txt', key.hex().encode())], {'admitted.txt': {}}, 0)

    def test_declared_limit_prefixed_base64_private_key_is_not_detected(self):
        # STOR-LIMIT-003: documents non-detection, NOT guard protection.
        key = rsa_key()
        self.assertTrue(gate.private_der(key))
        encoded = b'prefix' + base64.b64encode(key)
        gate.scan_entries([('admitted.txt', encoded)], {'admitted.txt': {}}, 0)

    def test_terminated_tar_container_digest_recording(self):
        import tempfile
        from record_package_digests import record
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = printable_tar() + bytes(1024)
            (root / 'nested').write_bytes(data)
            package = root / 'plenora-storage-1.2.3-x86_64-pc-windows-msvc.zip'
            package.write_bytes(archive([('nested', data)], True))
            policy = root / 'policy.json'
            initial = {'cli-windows': ['nested'], 'source': [], 'cli-linux': [],
                       'member_rules': {'cli-windows': {'nested': {'source': 'nested', 'container': {
                           'format': 'tar', 'members': {'p' * 155 + '/' + 'a' * 100: {}}}}}}}
            for repository in (False, True):
                with self.subTest(repository=repository):
                    policy.write_text(json.dumps(initial))
                    with patch.object(gate, 'REGISTRY', policy), patch.object(gate, 'ROOT', root), patch.object(gate, 'tracked_paths', return_value={'nested': 'nested'}), patch.object(gate, 'checkout_source_bytes', return_value=data), patch.object(gate, 'source_bytes', return_value=data):
                        record([] if repository else [package], repository=repository)
                        rule = gate.registry()['member_rules']['cli-windows']['nested']
                        self.assertEqual(rule['sha256'], hashlib.sha256(data).hexdigest())
                        gate.verify_distribution(package)

    def test_all_container_magics_fail_closed_even_with_digest(self):
        for kind, magic in (('gzip', b'\x1f\x8b'), ('bzip2', b'BZh'),
                            ('xz', b'\xfd7zXZ\x00'), ('zstd', b'\x28\xb5\x2f\xfd'),
                            ('lz4', b'\x04\x22\x4d\x18'), ('zip', b'PK\x03\x04'),
                            ('tar', gzip.decompress(archive([('a', b'a')]))),
                            ('7z', b'7z\xbc\xaf\x27\x1c'), ('rar', b'Rar!\x1a\x07'),
                            ('ar', b'!<arch>\n'), ('cpio', b'070701')):
            self.assertEqual(gate.container_format(magic), kind)
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                gate.scan(magic, rule=binary_rule(magic))
            if kind not in gate.SUPPORTED:
                with self.assertRaisesRegex(ValueError, 'unsupported'):
                    gate.scan(magic, rule=binary_rule(magic, {'format': kind, 'members': {}}))

    def test_two_levels_require_every_inventory_and_digest(self):
        inner = archive([('readme', b'hello')], True)
        inner_rule = binary_rule(inner, {'format': 'zip', 'members': {'readme': {}}})
        outer = archive([('nested', inner)], True)
        outer_rule = binary_rule(outer, {'format': 'zip', 'members': {'nested': inner_rule}})
        gate.scan(outer, rule=outer_rule)
        inner_rule.pop('container')
        with self.assertRaisesRegex(ValueError, 'undeclared container'):
            gate.scan(outer, rule=outer_rule)
        inner_rule['container'] = {'format': 'zip', 'members': {'other': {}}}
        with self.assertRaisesRegex(ValueError, 'inventory mismatch'):
            gate.scan(outer, rule=outer_rule)

    def test_declared_compression_recurses_into_tar(self):
        raw = gzip.decompress(archive([('readme', b'hello')]))
        for kind, compress in (('gzip', gzip.compress), ('bzip2', bz2.compress), ('xz', lzma.compress)):
            data = compress(raw)
            rule = binary_rule(data, {'format': kind, 'payload': binary_rule(raw, {
                'format': 'tar', 'members': {'readme': {}}})})
            gate.scan(data, rule=rule)
            rule['container']['payload']['container']['members'] = {}
            with self.assertRaisesRegex(ValueError, 'inventory mismatch'):
                gate.scan(data, rule=rule)

    def test_recording_is_reproducible_and_does_not_learn_members(self):
        import tempfile
        from record_package_digests import record
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy = root / 'policy.json'
            policy.write_text(json.dumps({'cli-windows': ['app'], 'member_rules': {'cli-windows': {'app': {'source': 'app'}}}}))
            package = root / 'plenora-storage-1.2.3-x86_64-pc-windows-msvc.zip'
            data = b'opaque\x00binary'
            package.write_bytes(archive([('app', data)], True))
            with patch.object(gate, 'REGISTRY', policy), patch.object(gate, 'source_bytes', return_value=data), patch.object(gate, 'tracked_paths', return_value={'app': 'app'}), patch.object(gate, 'checkout_source_bytes', return_value=data):
                record([package])
                first = policy.read_bytes()
                self.assertNotIn(b'\r\n', first)
                gate.verify_distribution(package)
                record([package])
                self.assertEqual(first, policy.read_bytes())
                package.write_bytes(archive([('app', data), ('extra', b'extra')], True))
                with self.assertRaisesRegex(ValueError, 'inventory mismatch'):
                    record([package])
                self.assertEqual(first, policy.read_bytes())
                package.write_bytes(archive([('app', gzip.compress(b'safe'))], True))
                with self.assertRaisesRegex(ValueError, 'undeclared'):
                    record([package])
                self.assertEqual(first, policy.read_bytes())

    def test_nested_distribution_name_never_grants_authority(self):
        nested = archive([('plenora-example-1.0.0/README.md', source_fixture('source', 'README.md'))])
        name = 'plenora-example-1.0.0.crate'
        policy = {'crates': {'plenora-example': ['README.md']}}
        with patch.object(gate, 'registry', return_value=policy):
            with self.assertRaisesRegex(ValueError, 'undeclared'):
                gate.scan(nested, name, rule=binary_rule(nested))
            gate.scan(nested, name, rule=binary_rule(nested, {'format': 'gzip', 'distribution': True}))

    def test_standalone_release_members_use_exact_rules(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'README.md'
            path.write_bytes(source_fixture('source', 'README.md'))
            policy = {'qualification': ['README.md'], 'public-documents': [],
                      'member_rules': {'qualification': {'README.md': binary_rule(path.read_bytes())}}}
            with patch.object(gate, 'registry', return_value=policy):
                gate.verify_distribution(path, member='README.md')
                with self.assertRaisesRegex(ValueError, 'undeclared'):
                    gate.verify_distribution(path)
                path.write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, 'digest'):
                    gate.verify_distribution(path, member='README.md')

    def test_two_level_container_does_not_hide_unknown_binary(self):
        inner = archive([('readme', b'unknown\x00format')], True)
        outer = archive([('nested', inner)], True)
        rule = binary_rule(outer, {'format': 'zip', 'members': {
            'nested': binary_rule(inner, {'format': 'zip', 'members': {'readme': {}}})}})
        with self.assertRaisesRegex(ValueError, 'digest'):
            gate.scan(outer, rule=rule)

    def test_prerelease_wheel_with_binary_rule(self):
        payload = source_fixture('cp310-abi3-win_amd64', 'plenora_storage/py.typed')
        policy = {'wheel-tags': gate.registry()['wheel-tags'],
                  'wheel-windows': ['plenora_storage/py.typed', 'plenora_storage-{version}.dist-info/METADATA'],
                  'member_rules': {'wheel-windows': {'plenora_storage/py.typed': binary_rule(payload)}}}
        with patch.object(gate, 'registry', return_value=policy):
            gate.scan(archive([('plenora_storage/py.typed', payload),
                               ('plenora_storage-1.2.3rc1.dist-info/METADATA', source_fixture('cp310-abi3-win_amd64', 'plenora_storage-1.2.3rc1.dist-info/METADATA'))], True),
                      'plenora_storage-1.2.3rc1-cp310-abi3-win_amd64.whl')


class BuildOutputTests(GitSnapshotTestCase):
    @staticmethod
    def native(target, library=False):
        data = bytearray(256)
        if target == 'x86_64-unknown-linux-gnu':
            data[:7] = bytes([127, 69, 76, 70, 2, 1, 1])
            data[16:18] = (3 if library else 2).to_bytes(2, 'little')
            data[18:20] = (62).to_bytes(2, 'little')
            data[20:24] = (1).to_bytes(4, 'little')
            data[52:54] = (64).to_bytes(2, 'little')
        else:
            data[:2] = b'MZ'
            data[60:64] = (128).to_bytes(4, 'little')
            data[128:132] = bytes([80, 69, 0, 0])
            data[132:134] = (0x8664).to_bytes(2, 'little')
            data[150:152] = (0x2002 if library else 2).to_bytes(2, 'little')
            data[152:154] = (0x20b).to_bytes(2, 'little')
        return bytes(data)

    def test_code_table_native_members(self):
        for context, native in gate.NATIVE_DISTRIBUTIONS.items():
            kind, name, fmt, library = native
            if name is None:
                continue
            context = kind if kind == 'qualification' else context
            data = self.native('x86_64-unknown-linux-gnu' if fmt == 'elf' else 'x86_64-pc-windows-msvc', library)
            with self.subTest(context=context, name=name):
                for candidate in (data, data + b'another build'):
                    gate.scan_entries([(name, candidate)], {name: {}}, 0, context)
                for bad in (b'text', b'MZ', gzip.compress(data), self.native('wrong' if fmt == 'elf' else 'x86_64-unknown-linux-gnu', library)):
                    with self.assertRaises(ValueError):
                        gate.scan_entries([(name, bad)], {name: {}}, 0, context)
                with self.assertRaises(ValueError):
                    gate.scan_entries([(name, data)], {name: {}}, 0, 'source')
                with self.assertRaises(ValueError):
                    gate.scan_entries([('extra', data)], {'extra': {}}, 0, context)

    def test_all_wheel_tags_and_unknown_platforms(self):
        for tag, kind in gate.registry()['wheel-tags'].items():
            name = 'plenora_storage-1.2.3rc1-' + tag + '.whl'
            actual_kind, prefix, expected = gate.distribution_policy(name)
            self.assertEqual(actual_kind, kind)
            files = []
            for member, rule in expected.items():
                native = gate.native_member(tag, member)
                data = self.native('x86_64-unknown-linux-gnu' if native[2] == 'elf' else 'x86_64-pc-windows-msvc', True) if native else source_fixture(tag, member)
                files.append((prefix + member, data))
            gate.scan(archive(files, True), name)
            import tempfile
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / name
                path.write_bytes(archive(files, True))
                gate.check_archive(path, kind)
                with self.assertRaisesRegex(ValueError, 'identity mismatch'):
                    gate.check_archive(path, 'cli-linux')
            for bad in (files[:-1], files + [('extra.so', self.native('x86_64-unknown-linux-gnu', True))]):
                with self.assertRaisesRegex(ValueError, 'inventory mismatch'):
                    gate.scan(archive(bad, True), name)
        for tag in ('cp311-cp311-win_amd64', 'cp310-abi3-win_arm64',
                    'cp310-abi3-manylinux_2_40_x86_64', 'cp310-abi3-linux_x86_64'):
            with self.assertRaisesRegex(ValueError, 'unknown wheel'):
                gate.distribution_policy('plenora_storage-1.2.3-' + tag + '.whl')
        with self.assertRaisesRegex(ValueError, 'unknown distribution'):
            gate.distribution_policy('plenora-storage-1.2.3-aarch64-unknown-linux-gnu.tar.gz')

    def test_built_crates_recurse_without_digest_and_keep_inventory(self):
        policy = gate.registry()
        for name, rule in policy['member_rules']['qualification'].items():
            if not name.endswith('.crate'):
                continue
            concrete = name.replace('{version}', '1.2.3')
            crate = name.rsplit('/', 1)[-1].removesuffix('-{version}.crate')
            prefix = crate + '-1.2.3/'
            files = [(prefix + item, source_fixture('crates/' + crate, item)) for item in policy['crates'][crate]]
            gate.scan(archive(files), concrete, rule=rule)
            for payload in (b'first build', b'second build'):
                with self.assertRaises(ValueError):
                    gate.scan(archive([(files[0][0], payload)] + files[1:]), concrete, rule=rule)
            for bad in (files[:-1], files + [(prefix + 'extra', b'safe')],
                        [(files[0][0], rsa_key())] + files[1:]):
                with self.assertRaises(ValueError):
                    gate.scan(archive(bad), concrete, rule=rule)
            with self.assertRaisesRegex(ValueError, 'undeclared container'):
                gate.scan(archive(files), concrete, rule={})

    def test_source_digests_match_git_and_reject_changed_policy(self):
        gate.check_source_digests()
        policy = gate.registry()
        rule = next(iter(policy['member_rules']['source'].values()))
        rule['sha256'] = '0' * 64
        with patch.object(gate, 'registry', return_value=policy):
            with self.assertRaisesRegex(ValueError, 'differs from git'):
                gate.check_source_digests()
        with self.assertRaisesRegex(ValueError, 'not tracked'):
            gate.source_bytes('target/not-a-source-file')

    def test_recorder_cannot_pin_builds_or_learn_source_provenance(self):
        from record_package_digests import record_rule
        rule = {}
        before = json.dumps(rule, sort_keys=True)
        record_rule(rule, self.native('x86_64-pc-windows-msvc'), 'plenora-storage.exe', 'cli-windows')
        self.assertEqual(before, json.dumps(rule, sort_keys=True))
        with self.assertRaisesRegex(ValueError, 'provenance'):
            record_rule({}, b'unknown\x00binary')
        with patch.object(gate, 'checkout_source_bytes', return_value=b'tracked bytes'):
            with self.assertRaisesRegex(ValueError, 'differ from git'):
                record_rule({'source': 'tracked', 'sha256': '0' * 64}, b'replacement')


    def test_complete_qualification_build_containers_and_roles(self):
        def distribution(name):
            kind, prefix, expected = gate.distribution_policy(name)
            context = name.rsplit('/', 1)[-1].split('-', 2)[2][:-4] if name.endswith('.whl') else kind
            files = []
            for member, rule in expected.items():
                if 'source' in rule:
                    payload = gate.source_bytes(rule['source'])
                elif 'container' in rule:
                    payload = distribution(member)
                elif (native := gate.native_member(context, member)):
                    payload = self.native('x86_64-unknown-linux-gnu' if native[2] == 'elf' else 'x86_64-pc-windows-msvc', native[3])
                else:
                    payload = source_fixture(context, member)
                files.append((prefix + member, payload))
            return archive(files, name.endswith(('.whl', '.zip')))
        name = 'plenora-storage-1.2.3-qualification.tar.gz'
        data = distribution(name)
        gate.scan_distribution(data, name)
        entries = gate.members(data)
        for bad in (entries[:-1], entries + [('1.2.3/unlisted.crate', archive([('key', rsa_key())]))]):
            with self.assertRaisesRegex(ValueError, 'inventory mismatch'):
                gate.scan_distribution(archive(bad), name)


    def test_standalone_archive_cannot_bypass_parent_inventory(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'plenora-storage-1.2.3-source.tar.gz'
            path.write_bytes(archive([('plenora-storage-1.2.3/readme', b'safe')]))
            with self.assertRaisesRegex(ValueError, 'undeclared standalone'):
                gate.verify_distribution(path, member='unknown/' + path.name)

    def test_generated_container_role_target_abi_and_format_are_binding(self):
        policy = gate.registry()
        name = 'x86_64-pc-windows-msvc/plenora_storage-1.2.3-cp310-abi3-win_amd64.whl'
        rule = policy['member_rules']['qualification'][name.replace('1.2.3', '{python_version}')]
        for field, value in (('role', 'source'), ('target', 'x86_64-unknown-linux-gnu'), ('abi', 'cp311-cp311')):
            bad = dict(rule, build={field: value})
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'build roles are forbidden'):
                gate.scan(b'', name, rule=bad)
        for name in ('plenora-storage-1.2.3-x86_64-pc-windows-msvc.tar.gz',
                     'plenora-storage-1.2.3-x86_64-unknown-linux-gnu.zip'):
            with self.assertRaisesRegex(ValueError, 'archive format'):
                gate.distribution_policy(name)
        with self.assertRaisesRegex(ValueError, 'container format mismatch'):
            gate.scan_distribution(archive([('safe', b'safe')]),
                                   'plenora_storage-1.2.3-cp310-abi3-win_amd64.whl')


    def test_three_policy_role_bypasses_are_rejected(self):
        native = self.native('x86_64-unknown-linux-gnu')
        build = {'build': {'role': 'cli', 'target': 'x86_64-unknown-linux-gnu', 'format': 'elf'}}
        cases = [('source', 'agents.md'), ('crates/plenora-storage-core', 'src/lib.rs'),
                 ('wheel-windows', 'plenora_storage-{version}.dist-info/METADATA')]
        for kind, name in cases:
            policy = gate.registry()
            policy['member_rules'].setdefault(kind, {})[name] = build
            with self.subTest(kind=kind), patch.object(gate, 'registry', return_value=policy):
                with self.assertRaisesRegex(ValueError, 'build roles are forbidden'):
                    gate.scan_entries([(name, native)], {name: build}, 0, kind)
                with self.assertRaisesRegex(ValueError, 'build roles are forbidden'):
                    gate.check_source_digests()
        # Removing the old role must not authorize the same binary substitution.
        for context, name in [('source', 'agents.md'), ('crates/plenora-storage-core', 'src/lib.rs'),
                              ('cp310-abi3-win_amd64', 'plenora_storage-1.2.3.dist-info/METADATA')]:
            with self.subTest(context=context), self.assertRaises(ValueError):
                gate.scan_entries([(name, native)], {name: {}}, 0, context)
        with self.assertRaisesRegex(ValueError, 'differs from git'):
            gate.scan_entries([('src/lib.rs', native)], {'src/lib.rs': binary_rule(native)}, 0, 'crates/plenora-storage-core')

    def test_normalized_paths_and_collisions(self):
        gate.compare(['RÉSUMÉ.txt'], ['résumé.txt'])
        self.assertEqual(gate.repository_path('crates/plenora-storage-core', 'SRC/lib.rs'), 'crates/plenora-storage-core/src/lib.rs')
        self.assertIn('agents.md', gate.tracked_paths())
        for names in [('A', 'a'), ('é', 'é'), ('a/../b',), ('a\\b',), ('a//b',), ('./a',), ('/a',), ('C:/a',)]:
            for zip_format in (True, False):
                with self.subTest(names=names, zip=zip_format), self.assertRaises(ValueError):
                    data = archive([(n, b'safe') for n in names], zip_format)
                    if names == ('a\\b',) and zip_format:
                        # ZipInfo normalizes separators on Windows even when writing.
                        data = data.replace(b'a/b', b'a\\b')
                    gate.members(data)
            with self.assertRaises(ValueError):
                gate.inventory(names)
        gate.scan_entries([('é.txt', b'safe')], {'é.txt': {}}, 0)

    def test_native_name_in_wrong_distribution_and_policy(self):
        name = 'plenora_storage/_native.abi3.so'
        data = self.native('x86_64-unknown-linux-gnu', True)
        with self.assertRaises(ValueError):
            gate.scan_entries([(name, data)], {name: {}}, 0, 'cp310-abi3-win_amd64')
        policy = gate.registry()
        policy['wheel-windows'].append(name)
        with patch.object(gate, 'registry', return_value=policy), self.assertRaisesRegex(ValueError, 'code table'):
            gate.check_source_digests()

    def test_stor_limit_004_only_table_members_skip_scanning(self):
        from fuzz_parsers import synthetic_ccache
        payloads = [archive([('credentials.json', b'{"password":"synthetic-only"}')], True),
                    synthetic_ccache(0x0503), synthetic_ccache(0x0504)]
        for context, native in gate.NATIVE_DISTRIBUTIONS.items():
            kind, name, fmt, library = native
            if name is None:
                continue
            context = kind if kind == 'qualification' else context
            for payload in payloads:
                data = self.native('x86_64-unknown-linux-gnu' if fmt == 'elf' else 'x86_64-pc-windows-msvc', library) + payload
                # Declared non-detection, not evidence of a safe build.
                gate.scan_entries([(name, data)], {name: {}}, 0, context)
                if payload in payloads[1:]:
                    with self.assertRaisesRegex(ValueError, 'embedded Kerberos'):
                        gate.scan_entries([('source.bin', data)], {'source.bin': binary_rule(data)}, 0)

class EntryPointRegressionTests(GitSnapshotTestCase):
    def test_same_native_bytes_through_all_release_paths(self):
        import tempfile
        import verify_release
        from versioning import parse_version
        version = '0.2.0'
        for target, kind, binary, suffix in (
                ('x86_64-unknown-linux-gnu', 'cli-linux', 'plenora-storage', '.tar.gz'),
                ('x86_64-pc-windows-msvc', 'cli-windows', 'plenora-storage.exe', '.zip')):
            data = BuildOutputTests.native(target)
            name = f'plenora-storage-{version}-{target}{suffix}'
            packed = archive([(binary, data)], suffix == '.zip')
            member = target + '/' + binary
            nested = target + '/' + name
            rule = {'container': {'format': 'zip' if suffix == '.zip' else 'gzip', 'distribution': True}}
            policy = {kind: [binary], 'qualification': [member, nested],
                      'member_rules': {'qualification': {nested: rule}}}
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary, \
                    patch.object(gate, 'registry', return_value=policy), \
                    patch.object(gate, 'workspace_version', return_value=parse_version(version)):
                root = Path(temporary) / version
                folder = root / target
                folder.mkdir(parents=True)
                (folder / binary).write_bytes(data)
                (folder / name).write_bytes(packed)
                gate.scan_distribution(packed, name)
                gate.verify_distribution(folder / name)
                gate.verify_distribution(folder / binary, member=member)
                gate.verify_distribution(folder / name, member=nested)
                bundle = archive([(version + '/' + member, data), (version + '/' + nested, packed)])
                gate.scan_distribution(bundle, f'plenora-storage-{version}-qualification.tar.gz')
                outer = archive([(name, packed)], True)
                gate.classify_member(outer, rule=binary_rule(outer, {'format': 'zip', 'members': {name: rule}}))
                artifacts = [{'name': n, 'sha256': hashlib.sha256(d).hexdigest(), 'size': len(d)}
                             for n, d in ((binary, data), (name, packed))]
                manifest = {'version': version, 'target': target, 'source_sha256': 'synthetic', 'artifacts': artifacts}
                (folder / 'release-manifest.json').write_text(json.dumps(manifest))
                (folder / 'SHA256SUMS').write_text('\n'.join(
                    hashlib.sha256((folder / n).read_bytes()).hexdigest() + '  ' + n
                    for n in (binary, name, 'release-manifest.json')))
                (folder / 'adoption-manifest-v4.json').write_text(json.dumps({'schema_version': 4, 'artifacts': []}))
                with patch.object(sys, 'argv', ['verify_release.py', str(root)]), \
                        patch.object(verify_release, 'adoption_errors', return_value=[]):
                    verify_release.main()
                (folder / binary).write_bytes(b'invalid native')
                with self.assertRaisesRegex(ValueError, 'native'):
                    gate.verify_distribution(folder / binary, member=member)

    def test_zip_entry_types_in_complete_wheel_and_nested_zip(self):
        import stat
        name = 'plenora_storage-1.2.3-cp310-abi3-win_amd64.whl'
        _, _, expected = gate.distribution_policy(name)
        files = [(n, BuildOutputTests.native('x86_64-pc-windows-msvc', True)
                  if gate.native_member('cp310-abi3-win_amd64', n) else source_fixture('cp310-abi3-win_amd64', n)) for n in expected]
        payload = b'-----BEGIN PRIVATE KEY-----\nYWJj\n'
        cases = [(stat.S_IFDIR, 0x10, '/', b'', True),
                 (0, 0x10, '/', b'', True),
                 (stat.S_IFDIR, 0x10, '/', payload, False),
                 (stat.S_IFREG, 0x10, '/', b'', False),
                 (stat.S_IFDIR, 0, '/', b'', False),
                 (0, 0, '/', b'', False),
                 (stat.S_IFDIR, 0x10, '', b'', False),
                 (stat.S_IFREG, 0x10, '', payload, False)]
        cases += [(kind, 0x10, '/', payload, False) for kind in
                  (stat.S_IFLNK, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK)]
        for kind, dos, slash, data, accepted in cases:
            with self.subTest(kind=kind, dos=dos, slash=slash, payload=bool(data)):
                out = io.BytesIO(archive(files, True))
                with zipfile.ZipFile(out, 'a') as stream:
                    info = zipfile.ZipInfo('plenora_storage' + slash)
                    info.create_system = 3
                    info.external_attr = ((kind | 0o755) << 16) | dos
                    stream.writestr(info, data)
                packed = out.getvalue()
                outer = archive([(name, packed)], True)
                for check in (lambda: gate.scan_distribution(packed, name),
                              lambda: gate.classify_member(outer, rule=binary_rule(outer, {
                                  'format': 'zip', 'members': {name: {'container': {
                                      'format': 'zip', 'distribution': True}}}}))):
                    if accepted:
                        check()
                        continue
                    with self.assertRaises(ValueError):
                        check()

    def test_tar_types_in_crate_source_and_compressed_containers(self):
        payload = b'-----BEGIN PRIVATE KEY-----\nYWJj\n'
        for kind in (tarfile.DIRTYPE, tarfile.SYMTYPE, tarfile.LNKTYPE,
                     tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE, tarfile.CONTTYPE):
            for data in (b'', payload):
                out = io.BytesIO()
                with tarfile.open(fileobj=out, mode='w', format=tarfile.USTAR_FORMAT) as stream:
                    info = tarfile.TarInfo('root/directory')
                    info.type, info.size = kind, len(data)
                    stream.addfile(info, io.BytesIO(data))
                    info = tarfile.TarInfo('root/directory/file')
                    info.size = 4
                    stream.addfile(info, io.BytesIO(b'safe'))
                raw = out.getvalue()
                for fmt, packed, declaration in (
                        ('tar', raw, {'format': 'tar', 'members': {'root/directory/file': {}}}),
                        *[(fmt, compress(raw), {'format': fmt, 'payload': binary_rule(raw, {
                            'format': 'tar', 'members': {'root/directory/file': {}}})})
                          for fmt, compress in (('gzip', gzip.compress), ('bzip2', bz2.compress), ('xz', lzma.compress))]):
                    with self.subTest(kind=kind, payload=bool(data), format=fmt):
                        if kind == tarfile.DIRTYPE and not data:
                            gate.classify_member(packed, rule=binary_rule(packed, declaration))
                        else:
                            with self.assertRaises(ValueError):
                                gate.classify_member(packed, rule=binary_rule(packed, declaration))
                for name in ('plenora-storage-core-1.2.3.crate', 'plenora-storage-1.2.3-source.tar.gz',
                             'plenora_storage-1.2.3.tar.gz'):
                    if kind != tarfile.DIRTYPE or data:
                        with self.subTest(name=name, kind=kind), self.assertRaisesRegex(
                                ValueError, 'sdist distribution|non-file archive|nonempty archive|private PEM'):
                            gate.scan_distribution(gzip.compress(raw), name)


class ContainerCoverageTests(GitSnapshotTestCase):
    def test_native_ccache_ranges(self):
        from fuzz_parsers import synthetic_ccache
        cache = synthetic_ccache(0x0504)[:48]
        for context, native in gate.NATIVE_DISTRIBUTIONS.items():
            if context.startswith('qualification') or context == '.crate':
                continue
            target = 'x86_64-unknown-linux-gnu' if native[2] == 'elf' else 'x86_64-pc-windows-msvc'
            wheel = context.startswith('cp')
            zipped = wheel or native[2] == 'pe'
            name = ('plenora_storage-1.2.3-' + context + '.whl' if wheel else
                    'plenora-storage-1.2.3-' + target + ('.zip' if zipped else '.tar.gz'))
            _, _, expected = gate.distribution_policy(name)
            files = [(n, BuildOutputTests.native(target, native[3]) + cache
                      if n == native[1] else source_fixture(context, n))
                     for n, rule in expected.items()]
            for method in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                with self.subTest(context=context, method=method):
                    def pack(entries):
                        if not zipped:
                            return gzip.compress(gzip.decompress(archive(entries)), compresslevel=0)
                        out = io.BytesIO()
                        with zipfile.ZipFile(out, 'w', compression=method) as stream:
                            for n, payload in wheel_record(entries):
                                stream.writestr(n, payload)
                        return out.getvalue()
                    good = pack(files)
                    gate.scan_distribution(good, name)
                    bad = [(n, payload if n == native[1] else cache) for n, payload in files]
                    with self.assertRaisesRegex(ValueError, 'Kerberos'):
                        gate.scan_distribution(pack(bad), name)
                    if not zipped:
                        raw = bytearray(gzip.decompress(good))
                        with tarfile.open(fileobj=io.BytesIO(raw)) as stream:
                            item = stream.getmember(native[1])
                        header = bytearray(raw[item.offset_data-512:item.offset_data])
                        marker = cache
                        header[400:400+len(marker)] = marker
                        header[148:156] = b'        '
                        header[148:156] = ('%06o\0 ' % sum(header)).encode()
                        changed = raw[:]
                        changed[item.offset_data-512:item.offset_data] = header
                        with self.assertRaisesRegex(ValueError, 'Kerberos'):
                            gate.scan_distribution(gzip.compress(changed), name)
                        end = item.offset_data + item.size
                        raw[end:end+len(marker)] = marker
                        with self.assertRaisesRegex(ValueError, 'padding'):
                            gate.scan_distribution(gzip.compress(raw), name)
                    else:
                        with patch.object(gate, 'check_content', wraps=gate.check_content) as scan:
                            gate.scan_distribution(good, name)
                        with zipfile.ZipFile(io.BytesIO(good)) as stream:
                            begin, end = gate.validate_zip(good, stream)[native[1]]
                        scanned = [call.args[0] for call in scan.call_args_list]
                        self.assertIn(good[:begin], scanned)
                        self.assertIn(good[end:], scanned)

    def test_complete_distributions_with_hidden_compressed_keys(self):
        crate = 'plenora-storage-core-3.0.5.crate'
        wheel = 'plenora_storage-3.0.5-cp310-abi3-win_amd64.whl'
        for name in (crate, wheel):
            _, prefix, expected = gate.distribution_policy(name)
            files = [(prefix + member, b'safe') for member in expected]
            complete = archive(files, name == wheel)
            if name == crate:
                hidden = gzip.decompress(archive([('hidden.gz', gzip.compress(rsa_key()))]))
                bad = gzip.compress(gzip.decompress(complete) + hidden)
            else:
                output = io.BytesIO()
                with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as stream:
                    stream.writestr('hidden', b'-----BEGIN PRIVATE KEY-----\nYWJj\n')
                bad = output.getvalue() + complete
            # Inventory is complete; failure must precede member classification.
            with patch.object(gate, 'scan_entries') as scan:
                gate.scan_distribution(complete, name)
                scan.assert_called_once()
                scan.reset_mock()
                with self.assertRaises(ValueError):
                    gate.scan_distribution(bad, name)
                scan.assert_not_called()

    def test_zip_gaps_comments_and_inconsistent_headers(self):
        good = archive([('one', b'safe'), ('two', b'safe')], True)
        gate.members(good)
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as stream:
            stream.writestr('one', b'safe')
            stream.fp.write(b'garbage')
            stream.start_dir = stream.fp.tell()
            stream.writestr('two', b'safe')
        bad = [out.getvalue()]
        out = io.BytesIO(good)
        with zipfile.ZipFile(out, 'a') as stream:
            stream.comment = b'comment'
        bad.append(out.getvalue())
        for offset in (14, 18, 22, 30):  # CRC, sizes, name
            changed = bytearray(good)
            changed[offset] ^= 1
            bad.append(bytes(changed))
        bad.extend((good + b'junk', good + good))
        for data in bad:
            with self.subTest(case=bad.index(data)), self.assertRaises(ValueError):
                gate.members(data)

    def test_gzip_stream_and_metadata_coverage_at_every_level(self):
        good = archive([('one', b'safe')])
        for bad in (good + gzip.compress(b'safe'), good + b'junk', good[:-1],
                    good[:3] + b'\x10' + good[4:10] + b'comment\0' + good[10:],
                    good[:3] + b'\x08' + good[4:10] + b'unknown\0' + good[10:],
                    good[:3] + b'\x04' + good[4:10] + b'\x01\0x' + good[10:]):
            with self.subTest(length=len(bad)):
                with self.assertRaises(ValueError):
                    gate.members(bad)
                rule = binary_rule(bad, {'format': 'gzip', 'payload': {}})
                with self.assertRaises(ValueError):
                    gate.scan(bad, rule=rule)

    def test_tar_terminator_and_padding_coverage(self):
        raw = gzip.decompress(archive([('one', b'safe')]))
        gate.members(raw)
        for bad in (raw[:1024], raw[:1024] + bytes(512), raw + b'x',
                    raw[:516] + b'x' + raw[517:], raw + raw):
            with self.assertRaises(ValueError):
                gate.members(bad)
