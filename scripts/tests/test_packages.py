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


class PackageGuardTests(unittest.TestCase):
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
            with self.subTest(kind=kind), patch.object(gate, 'registry', return_value={kind: ['one', 'two']}):
                good = [(prefix+'one', b'one'), (prefix+'two', b'two')]
                gate.scan(archive(good), name)
                for files in (good[:1], good+[(prefix+'extra', b'extra')]):
                    with self.assertRaises(ValueError):
                        gate.scan(archive(files), name)

    def test_prerelease_qualification_uses_python_version(self):
        name = 'plenora-storage-1.2.3-rc.1-qualification.tar.gz'
        with patch.object(gate, 'registry', return_value={'qualification': ['artifact-{python_version}']}):
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
                package = archive([('plenora-storage-core-3.0.3/README.md', data)])
                with self.assertRaises(ValueError):
                    gate.scan(package, 'plenora-storage-core-3.0.3.crate')
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
            self.assertEqual(gate.members(candidate), [('p' * 155 + '/' + 'a' * 100, candidate[512:])])
            with self.assertRaisesRegex(ValueError, 'digest'):
                gate.scan(candidate, rule={'container': declaration})
        rule = binary_rule(data, declaration)
        gate.scan(data, rule=rule)
        with self.assertRaisesRegex(ValueError, 'digest'):
            gate.scan(changed, rule=rule)
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

    def test_printable_container_digest_recording(self):
        import tempfile
        from record_package_digests import record
        with tempfile.TemporaryDirectory(dir=gate.ROOT / 'target') as temporary:
            root = Path(temporary)
            data = printable_tar()
            (root / 'nested').write_bytes(data)
            package = root / 'plenora-storage-1.2.3-x86_64-pc-windows-msvc.zip'
            package.write_bytes(archive([('nested', data)], True))
            policy = root / 'policy.json'
            initial = {'cli-windows': ['nested'], 'source': [], 'cli-linux': [],
                       'member_rules': {'cli-windows': {'nested': {'container': {
                           'format': 'tar', 'members': {'p' * 155 + '/' + 'a' * 100: {}}}}}}}
            for repository in (False, True):
                with self.subTest(repository=repository):
                    policy.write_text(json.dumps(initial))
                    with patch.object(gate, 'REGISTRY', policy), patch.object(gate, 'ROOT', root):
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
        with tempfile.TemporaryDirectory(dir=gate.ROOT / 'target') as temporary:
            root = Path(temporary)
            policy = root / 'policy.json'
            policy.write_text(json.dumps({'cli-windows': ['app']}))
            package = root / 'plenora-storage-1.2.3-x86_64-pc-windows-msvc.zip'
            data = b'opaque\x00binary'
            package.write_bytes(archive([('app', data)], True))
            with patch.object(gate, 'REGISTRY', policy):
                record([package])
                first = policy.read_bytes()
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
        nested = archive([('plenora-example-1.0.0/readme', b'hello')])
        name = 'plenora-example-1.0.0.crate'
        policy = {'crates': {'plenora-example': ['readme']}}
        with patch.object(gate, 'registry', return_value=policy):
            with self.assertRaisesRegex(ValueError, 'undeclared'):
                gate.scan(nested, name, rule=binary_rule(nested))
            gate.scan(nested, name, rule=binary_rule(nested, {'format': 'gzip', 'distribution': True}))

    def test_standalone_release_members_use_exact_rules(self):
        import tempfile
        with tempfile.TemporaryDirectory(dir=gate.ROOT / 'target') as temporary:
            path = Path(temporary) / 'README.md'
            path.write_bytes(b'opaque\x00binary')
            policy = {'qualification': ['target/README.md'], 'public-documents': [],
                      'member_rules': {'qualification': {'target/README.md': binary_rule(path.read_bytes())}}}
            with patch.object(gate, 'registry', return_value=policy):
                gate.verify_distribution(path, member='target/README.md')
                with self.assertRaisesRegex(ValueError, 'undeclared'):
                    gate.verify_distribution(path)
                path.write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, 'digest'):
                    gate.verify_distribution(path, member='target/README.md')

    def test_two_level_container_does_not_hide_unknown_binary(self):
        inner = archive([('readme', b'unknown\x00format')], True)
        outer = archive([('nested', inner)], True)
        rule = binary_rule(outer, {'format': 'zip', 'members': {
            'nested': binary_rule(inner, {'format': 'zip', 'members': {'readme': {}}})}})
        with self.assertRaisesRegex(ValueError, 'digest'):
            gate.scan(outer, rule=rule)

    def test_prerelease_wheel_with_binary_rule(self):
        payload = b'native\x00binary'
        policy = {'wheel-windows': ['module', 'metadata-{version}'],
                  'member_rules': {'wheel-windows': {'module': binary_rule(payload)}}}
        with patch.object(gate, 'registry', return_value=policy):
            gate.scan(archive([('module', payload), ('metadata-1.2.3rc1', b'metadata')], True),
                      'plenora_storage-1.2.3rc1-cp310-abi3-win_amd64.whl')
