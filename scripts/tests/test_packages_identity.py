"""Source identity at every distribution boundary, independent of scanning."""
from pathlib import Path
import subprocess
import hashlib
import tempfile
import unittest
from unittest.mock import patch

from test_packages import archive, gate, rsa_key, source_fixture


class SourceIdentityTests(unittest.TestCase):
    def setUp(self):
        (gate.ROOT / 'target').mkdir(exist_ok=True)

    def test_policy_cannot_promote_sources_to_generated_containers(self):
        name = 'plenora-storage-3.0.3-source.tar.gz'
        rule = {'container': {'format': 'gzip', 'distribution': True}}
        for context in ('source', 'crates/plenora-storage-core', 'cli-linux', 'qualification'):
            with self.subTest(context=context), self.assertRaisesRegex(ValueError, 'undeclared generated'):
                gate.scan_entries([(name, archive([('anything', b'text')]))], {name: rule}, 0, context)

    @gate.repository_snapshot
    def test_changed_source_inside_archive_and_qualification(self):
        name = 'plenora-storage-3.0.3-source.tar.gz'
        member = 'README.md'
        original = gate.checkout_source_bytes(member)
        nested = 'x86_64-pc-windows-msvc/' + name
        rule = {'container': {'format': 'gzip', 'distribution': True}}
        policy = {'source': [member], 'qualification': [nested],
                  'member_rules': {'qualification': {nested: rule}}}
        with patch.object(gate, 'registry', return_value=policy):
            for payload in (original, original + b'changed', original + rsa_key().hex().encode()):
                packed = archive([('plenora-storage-3.0.3/' + member, payload)])
                bundle = archive([('3.0.3/' + nested, packed)])
                for data, filename in ((packed, name),
                                       (bundle, 'plenora-storage-3.0.3-qualification.tar.gz')):
                    if payload == original:
                        gate.scan_distribution(data, filename)
                    else:
                        with self.assertRaisesRegex(ValueError, 'differs from git'):
                            gate.scan_distribution(data, filename)

    @gate.repository_snapshot
    def test_text_and_untracked_sources_across_distributions(self):
        cases = [('source', 'README.md'), ('contracts', 'README.md'),
                 ('cli-linux', 'README.md'), ('cli-windows', 'README.md'),
                 ('crates/plenora-storage-core', 'README.md'),
                 ('crates/plenora-storage-core', 'Cargo.toml.orig'),
                 ('crates/plenora-storage-py', 'README.md'),
                 ('cp310-abi3-win_amd64', 'plenora_storage/__init__.py'),
                 ('cp310-abi3-manylinux_2_34_x86_64', 'plenora_storage/__init__.pyi'),
                 ('qualification', 'README.md'), ('public-documents', 'README.md')]
        for context, name in cases:
            with self.subTest(context=context, name=name):
                path = gate.path_lookup(gate.tracked_paths(), gate.repository_path(context, name))
                original = gate.checkout_source_bytes(path)
                gate.scan_entries([(name, original)], {name: {}}, 0, context)
                for changed in (original + b'changed', original + rsa_key().hex().encode()):
                    with self.assertRaisesRegex(ValueError, 'differs from git'):
                        gate.scan_entries([(name, changed)], {name: {}}, 0, context)
                # Even admitting the filename in policy cannot authorize it.
                with self.assertRaisesRegex(ValueError, 'not tracked'):
                    gate.scan_entries([('untracked.txt', b'ordinary text')],
                                      {'untracked.txt': {}}, 0, context)

    @gate.repository_snapshot
    def test_every_generated_exception_is_scanned_and_closed(self):
        for kind, names in gate.GENERATED_MEMBERS.items():
            context = {'crates': 'crates/plenora-storage-core',
                       'wheel': 'cp310-abi3-win_amd64'}.get(kind, kind)
            for template in names:
                name = template.replace('{version}', '3.0.3').replace('{python_version}', '3.0.3')
                with self.subTest(context=context, name=name):
                    for allowed in (source_fixture(context, name),):
                        gate.scan_entries([(name, allowed)], {name: {}}, 0, context)
                    for forbidden in (b'-----BEGIN PRIVATE KEY-----\nYWJj\n', b'opaque\x00data'):
                        with self.assertRaises(ValueError):
                            gate.scan_entries([(name, forbidden)], {name: {}}, 0, context)
                    self.assertFalse(gate.generated_member(context, name + '.extra'))
                    self.assertFalse(gate.generated_member('source', name))
        self.assertFalse(gate.generated_member('cp310-abi3-win_amd64',
                                              'plenora_storage-3.0.3.dist-info/licenses/LICENSE'))

    def test_git_checkout_eol_and_dirty_attributes(self):
        # Only this disposable fixture gets a synthetic Git history.
        with tempfile.TemporaryDirectory(dir=gate.ROOT / 'target') as temporary:
            root = Path(temporary)
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.PIPE)
            git('init', '-q')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            (root / '.gitattributes').write_bytes(b'*.txt text\nlf.txt text eol=lf\ncrlf.txt text eol=crlf\n*.bin binary\n')
            for name in ('auto.txt', 'lf.txt', 'crlf.txt'):
                (root / name).write_bytes(b'one\ntwo\n')
            (root / 'wire.bin').write_bytes(b'one\r\ntwo\x00')
            git('-c', 'core.autocrlf=false', 'add', '.')
            git('-c', 'core.autocrlf=false', 'commit', '-qm', 'fixture')
            for autocrlf in ('false', 'true'):
                git('config', 'core.autocrlf', autocrlf)
                git('config', 'core.eol', 'lf')
                for name in ('auto.txt', 'lf.txt', 'crlf.txt', 'wire.bin'):
                    (root / name).unlink()
                git('checkout-index', '--all', '--force')
                expected = {name: (root / name).read_bytes() for name in
                            ('auto.txt', 'lf.txt', 'crlf.txt', 'wire.bin')}
                # A dirty attributes file must not redefine the release reference.
                (root / '.gitattributes').write_bytes(b'* text eol=crlf\n')
                with patch.object(gate, 'ROOT', root):
                    for name, data in expected.items():
                        with self.subTest(autocrlf=autocrlf, name=name):
                            self.assertEqual(gate.checkout_source_bytes(name), data)
                            gate.scan_entries([(name, data)], {name: {} if name.endswith('.txt') else
                                {'sha256': hashlib.sha256(data).hexdigest()}}, 0, 'source')
                            changed = data.replace(b'\r\n', b'\n') if b'\r\n' in data else data.replace(b'\n', b'\r\n')
                            with self.assertRaisesRegex(ValueError, 'differs from git'):
                                gate.scan_entries([(name, changed)], {name: {}}, 0, 'source')
                            from record_package_digests import record_rule
                            rule = {'source': name, 'sha256': '0' * 64}
                            record_rule(rule, data, name, 'source')
                            self.assertEqual(rule['sha256'], hashlib.sha256(data).hexdigest())
                            with patch.object(gate, 'registry', return_value={
                                    'member_rules': {'source': {name: rule}}}):
                                gate.check_source_digests()
                git('checkout', '--', '.gitattributes')

    def test_source_builder_uses_checkout_bytes(self):
        import io
        import tarfile
        import package_source
        with tempfile.TemporaryDirectory(dir=gate.ROOT / 'target') as temporary:
            raw, result = Path(temporary) / 'raw.tar', Path(temporary) / 'source.tar.gz'
            with tarfile.open(raw, 'w') as stream:
                info = tarfile.TarInfo('root/file.txt')
                info.size = 4
                stream.addfile(info, io.BytesIO(b'one\n'))
            with patch.object(package_source, 'registry', return_value={'source': ['file.txt']}), \
                    patch.object(package_source, 'checkout_source_bytes', return_value=b'one\r\n'):
                package_source.select_source_archive(raw, result, 'root', checkout_bytes=True)
            self.assertEqual(gate.members(result.read_bytes()), [('root/file.txt', b'one\r\n')])

class GeneratedStructureTests(unittest.TestCase):
    @gate.repository_snapshot
    def test_generated_metadata_rejects_hex_and_extra_fields(self):
        import json
        from test_packages import source_fixture
        cases = [('crates/plenora-storage-core', n) for n in
                 ('.cargo_vcs_info.json', 'Cargo.toml', 'Cargo.lock')]
        cases += [('cp310-abi3-win_amd64', 'plenora_storage-3.0.3.dist-info/' + n)
                  for n in ('METADATA', 'WHEEL')]
        for context, name in cases:
            good = source_fixture(context, name)
            gate.scan_entries([(name, good)], {name: {}}, 0, context)
            for bad in (good + rsa_key().hex().encode(), b'ordinary arbitrary text'):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    gate.scan_entries([(name, bad)], {name: {}}, 0, context)
        name = '.cargo_vcs_info.json'
        good = json.loads(source_fixture('crates/plenora-storage-core', name))
        for field, value in [('extra', 'abcdef'), ('git', {'sha1': '0' * 40}),
                             ('path_in_vcs', 'crates/wrong')]:
            with self.assertRaises(ValueError):
                gate.scan_entries([(name, json.dumps(dict(good, **{field: value})).encode())],
                                  {name: {}}, 0, 'crates/plenora-storage-core')

    def test_record_exact_members_hashes_sizes_and_duplicates(self):
        from package_generated import validate_record
        from test_packages import wheel_record
        name = 'plenora_storage-3.0.3.dist-info/RECORD'
        entries = wheel_record([('data.py', b'content'), (name, b'')])
        validate_record(entries, 'cp310-abi3-win_amd64')
        for bad in (entries + [('extra', b'')], entries + entries[:1],
                    [('data.py', b'changed'), entries[1]],
                    [entries[0], (name, entries[1][1].replace(b',7', b',8'))],
                    [entries[0], (name, entries[1][1] + entries[1][1])]):
            with self.assertRaises(ValueError):
                validate_record(bad, 'cp310-abi3-win_amd64')
        with self.assertRaisesRegex(ValueError, 'complete wheel'):
            gate.classify_member(entries[1][1], name, rule={}, distribution='cp310-abi3-win_amd64')

    def test_text_member_recorder_rejects_substitution_without_digest(self):
        import record_package_digests as recorder
        with tempfile.TemporaryDirectory(dir=gate.ROOT / 'target') as tmp:
            path = Path(tmp) / 'input.txt'
            path.write_bytes(b'replacement text')
            policy = {'source': ['README.md']}
            with patch.object(gate, 'registry', return_value=policy), \
                    patch.object(gate, 'REGISTRY', Path(tmp) / 'policy.json'):
                with self.assertRaisesRegex(ValueError, 'differs from git'):
                    recorder.record([], member=('source', 'README.md', str(path)))
                self.assertFalse((Path(tmp) / 'policy.json').exists())

    def test_git_minimum_version(self):
        for version, accepted in [('2.38.9', False), ('2.39.5', True), ('2.51.0.windows.1', True)]:
            with patch.object(gate.subprocess, 'check_output', return_value=('git version ' + version).encode()):
                if accepted:
                    gate.check_git_version()
                else:
                    with self.assertRaisesRegex(ValueError, 'Git >= 2.39'):
                        gate.check_git_version()
