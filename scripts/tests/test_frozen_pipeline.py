"""Artifact classifier, provenance and execution boundaries, without build tools."""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_packages as gate
import record_package_digests as recorder
import check_release_packages as recheck
import release_publication as publication


def digest(data):
    return hashlib.sha256(data).hexdigest()


def archive(entries, zipped=False):
    buffer = io.BytesIO()
    if zipped:
        with zipfile.ZipFile(buffer, 'w') as out:
            for name, data in entries:
                out.writestr(name, data)
    else:
        with tarfile.open(fileobj=buffer, mode='w:gz') as out:
            for name, data in entries:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                out.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def corpus():
    """Independent bytes and expected verdicts, shared by every boundary."""
    private = b'-----BEGIN PRIVATE KEY-----\nYWJj\n-----END PRIVATE KEY-----\n'
    good = archive([('member.txt', b'ordinary text')], True)
    nested = {'format': 'zip', 'members': {'member.txt': {}}}
    return [
        ('text', b'ordinary text', {}, True),
        ('private-text', private, {}, False),
        ('zip', good, {'container': nested}, True),
        ('nested-private', archive([('member.txt', private)], True), {'container': nested}, False),
        ('extra-member', archive([('member.txt', b'ordinary text'), ('extra', b'extra')], True),
         {'container': nested}, False),
        ('truncated-zip', good[:-12], {'container': nested}, False),
        ('undeclared-container', good, {}, False),
    ]


class PipelineTests(unittest.TestCase):
    def authorities(self, data, rule):
        """Replace only the source store/policy, never parser or classifier.

        No builder state, Cargo cache or network is supplied to the verifier.
        This deliberately permits hostile *source* bytes, so classification
        cannot accidentally pass a test merely by rejecting provenance first.
        """
        stack = ExitStack()
        stack.enter_context(patch.object(gate, 'registry', return_value={
            'source': ['payload.dat'], 'member_rules': {'source': {'payload.dat': rule}}}))
        stack.enter_context(patch.object(gate, 'tracked_paths', return_value={'payload.dat': 'payload.dat'}))
        stack.enter_context(patch.object(gate, 'checkout_source_bytes', return_value=data))
        stack.enter_context(patch.object(gate.subprocess, 'run', side_effect=AssertionError('external tool forbidden')))
        stack.enter_context(patch.object(gate.subprocess, 'check_output', side_effect=AssertionError('external tool forbidden')))
        return stack

    def test_one_corpus_crosses_classifier_recorder_and_publication_recheck(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'plenora-storage-4.0.0-source.tar.gz'
            for label, data, declaration, accepted in corpus():
                rule = dict(deepcopy(declaration), source='payload.dat', sha256=digest(data))
                path.write_bytes(archive([('plenora-storage-4.0.0/payload.dat', data)]))
                with self.authorities(data, rule):
                    entries = {
                        'classifier': lambda: gate.classify_member(data, 'payload.dat', rule=rule, distribution='source'),
                        'legacy-scan': lambda: gate.scan(data, 'payload.dat', rule=rule, distribution='source'),
                        'entries': lambda: gate.scan_entries([('payload.dat', data)], {'payload.dat': rule}, 0, 'source'),
                        'recorder': lambda: recorder.record_rule(deepcopy(rule), data, 'payload.dat', 'source'),
                        'distribution': lambda: gate.verify_distribution(path),
                        'publication-recheck': lambda: recheck.verify_publication([path]),
                    }
                    for boundary, action in entries.items():
                        with self.subTest(corpus=label, boundary=boundary):
                            if accepted:
                                action()
                            else:
                                with self.assertRaises(zipfile.BadZipFile if label == 'truncated-zip' else ValueError):
                                    action()

    def test_consistent_digest_cannot_promote_foreign_source(self):
        committed, foreign = b'reviewed source', b'foreign but harmless source'
        rule = {'source': 'payload.dat', 'sha256': digest(foreign)}
        with self.authorities(committed, rule):
            for name, action in {
                'classifier': lambda: gate.classify_member(foreign, 'payload.dat', rule=rule, distribution='source'),
                'recorder': lambda: recorder.record_rule(deepcopy(rule), foreign, 'payload.dat', 'source'),
            }.items():
                with self.subTest(boundary=name), self.assertRaises(ValueError):
                    action()

    def test_archive_identity_and_truncation_are_checked_on_consumer_bytes(self):
        data = b'reviewed source'
        rule = {'source': 'payload.dat'}
        good = archive([('plenora-storage-4.0.0/payload.dat', data)])
        variants = [good[:-12], archive([('other/payload.dat', data)]),
                    archive([('plenora-storage-4.0.0/payload.dat', data),
                             ('plenora-storage-4.0.0/hidden', b'hidden')])]
        with tempfile.TemporaryDirectory() as temp, self.authorities(data, rule):
            path = Path(temp) / 'plenora-storage-4.0.0-source.tar.gz'
            path.write_bytes(good)
            recheck.verify_publication([path])
            for variant in variants:
                with self.subTest(digest=digest(variant)):
                    path.write_bytes(variant)
                    with self.assertRaises(ValueError):
                        recheck.verify_publication([path])

    def test_upload_and_publish_verify_exact_bytes_before_external_effect(self):
        # Real entrypoints and control flow; only network/tag and outer inventory
        # are test doubles. A verifier in an unreachable branch is not observed.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            artifact = root / 'asset'
            artifact.write_bytes(b'qualified')
            (root / 'publication-index.json').write_text('{}')
            (root / 'SHA256SUMS').write_text('fixture')
            index = {'files': {'asset': digest(b'qualified')}}
            events = []
            def verifier(paths):
                observed = {p.name: digest(p.read_bytes()) for p in paths}
                events.append(('verify', observed))
                if observed != index['files']:
                    raise ValueError('substitution')
            def transport(argv, **kwargs):
                self.assertTrue(events, 'external effect before verifier')
                self.assertEqual(events[0], ('verify', index['files']))
                events.append(('transport', argv[:3]))
                if argv[2] == 'download':
                    destination = Path(argv[argv.index('--dir') + 1])
                    for path in root.iterdir():
                        if path.is_file():
                            shutil.copyfile(path, destination / path.name)
            with patch.object(publication, 'draft'), patch.object(publication, 'check_files', return_value=index), \
                    patch.object(publication, 'verify_publication', side_effect=verifier), \
                    patch.object(publication.subprocess, 'run', side_effect=transport):
                for action in (publication.upload, publication.publish):
                    events.clear()
                    action(root, 'v4.0.0')
                    self.assertEqual(events[0], ('verify', index['files']))
                    self.assertTrue(any(event[0] == 'transport' for event in events))
                    artifact.write_bytes(b'substituted after inventory check')
                    events.clear()
                    with self.assertRaises(ValueError):
                        action(root, 'v4.0.0')
                    self.assertEqual([e[0] for e in events], ['verify'])
                    artifact.write_bytes(b'qualified')

    def test_prepare_runs_qualification_and_verifies_the_emitted_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'input/qualification/dist/4.0.0'
            source.mkdir(parents=True)
            (root / 'input/qualification/evidence').mkdir()
            (source / 'SHA256SUMS').write_text('fixture')
            platforms = []
            for target in publication.TARGETS:
                folder = source / target
                folder.mkdir()
                (folder / 'SHA256SUMS').write_text('fixture')
                names = [f'plenora-storage-4.0.0-{target}' + ('.zip' if 'windows' in target else '.tar.gz'),
                         f'plenora_storage-{target}.whl', 'plenora-storage-4.0.0-source.tar.gz',
                         *publication.target_documents('4.0.0', target)]
                artifacts = []
                for name in names:
                    data = b'shared-source' if name.endswith('-source.tar.gz') else (target + name).encode()
                    (folder / name).write_bytes(data)
                    artifacts.append({'name': name, 'sha256': digest(data)})
                (folder / 'release-manifest.json').write_text(json.dumps({'artifacts': artifacts}))
                platforms.append({'target': target, 'evidence': []})
            (source / 'release-qualification.json').write_text(json.dumps({
                'schema_version': 2, 'status': 'qualified_for_publication',
                'platforms': platforms, 'additional_gates': []}))
            incoming = root / 'input.tar.gz'
            with tarfile.open(incoming, 'w:gz') as stream:
                stream.add(root / 'input/qualification', arcname='qualification')
            observed = []
            def qualify(argv, **kwargs):
                self.assertEqual(Path(argv[1]).name, 'qualify_release.py')
                self.assertEqual(Path(argv[2]).name, '4.0.0')
                self.assertEqual(argv[3], '--evidence')
                observed.append('qualification')
            def verify(paths):
                self.assertEqual(observed, ['qualification'])
                for path in paths:
                    with tarfile.open(path) as stream:
                        for member in stream.getmembers():
                            self.assertTrue(member.isfile())
                            self.assertEqual(stream.extractfile(member).read(),
                                             (source / member.name.removeprefix('4.0.0/')).read_bytes())
                observed.append('verified-bytes')
            output = root / 'output'
            with patch.object(publication, 'check_tag'), patch.object(publication, 'check_files'), \
                    patch.object(publication.subprocess, 'run', side_effect=qualify), \
                    patch.object(publication, 'verify_publication', side_effect=verify):
                publication.prepare(incoming, output, 'v4.0.0')
            self.assertEqual(observed, ['qualification', 'verified-bytes'])
            index = json.loads((output / 'publication-index.json').read_text())
            self.assertTrue(index['files'])
            for name, sha in index['files'].items():
                self.assertEqual(digest((output / name).read_bytes()), sha)

    def test_downloaded_substitution_cannot_reach_publish(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            version = '4.0.0'
            names = [f'plenora-storage-{version}-source.tar.gz', f'plenora-storage-{version}-qualification.tar.gz']
            platforms = []
            for target in publication.TARGETS:
                names += [f'plenora-storage-{version}-{target}' + ('.zip' if 'windows' in target else '.tar.gz'),
                          f'wheel-{target}.whl', *publication.target_documents(version, target).values()]
                platforms.append({'target': target, 'wheel': f'wheel-{target}.whl', 'wheel_sha256': digest(b'artifact')})
            for name in names:
                (root / name).write_bytes(b'artifact')
            receipt = {'version': version, 'source_revision': 'a' * 40, 'schema_version': 2,
                       'status': 'qualified_for_publication', 'platforms': platforms,
                       'additional_gates': [{'name': 'fixture', 'sha256': 'b' * 64}]}
            (root / 'release-qualification.json').write_text(json.dumps(receipt))
            names.append('release-qualification.json')
            index = {'version': version, 'files': {name: digest((root / name).read_bytes()) for name in names}}
            (root / 'publication-index.json').write_text(json.dumps(index))
            (root / 'SHA256SUMS').write_text('fixture')
            for substitute in (False, True):
                effects = []
                def external(argv, **kwargs):
                    effects.append(argv[2])
                    if argv[2] == 'download':
                        downloaded = Path(argv[argv.index('--dir') + 1])
                        for path in root.iterdir():
                            shutil.copyfile(path, downloaded / path.name)
                        if substitute:
                            (downloaded / names[0]).write_bytes(b'other bytes after verification')
                with self.subTest(substitute=substitute), patch.object(publication, 'draft'), \
                        patch.object(publication, 'verify_publication'), \
                        patch.object(publication.subprocess, 'check_output', return_value='a' * 40), \
                        patch.object(publication.subprocess, 'run', side_effect=external):
                    if substitute:
                        with self.assertRaises(ValueError):
                            publication.publish(root, 'v4.0.0')
                        self.assertEqual(effects, ['download'])
                    else:
                        publication.publish(root, 'v4.0.0')
                        self.assertEqual(effects, ['download', 'edit'])


if __name__ == '__main__':
    unittest.main()
