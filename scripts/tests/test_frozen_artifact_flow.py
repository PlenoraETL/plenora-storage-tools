"""Execute the CI artifact handoff contract in disjoint local workspaces."""
import fnmatch
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from frozen_plan import workflows
from frozen_proofs import detail

ROOT = Path(__file__).resolve().parents[2]


class ArtifactFlowTests(unittest.TestCase):
    def test_recheck_consumes_both_candidates_and_crates_on_a_clean_runner(self):
        jobs = workflows(ROOT)['ci.yml']['jobs']
        recheck = jobs['release-package-recheck']
        fixtures = {'docker-conformance': ('dist/4.0.0/x86_64-unknown-linux-gnu/subject.bin', b'linux'),
                    'native-windows': ('dist/4.0.0/x86_64-pc-windows-msvc/subject.bin', b'windows'),
                    'release-package-build': ('target/package/fixture.crate', b'crate')}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store, producer_receipts = {}, []
            for job, (relative, data) in fixtures.items():
                producer = root / job
                source = producer / relative
                source.parent.mkdir(parents=True)
                source.write_bytes(data)
                # Deliberately not shared: neither checkout nor producer cache
                # is part of the upload action's declared path.
                (producer / 'cargo-cache').mkdir()
                (producer / 'private-checkout').write_bytes(b'not an artifact')
                matches = []
                for number, step in enumerate(jobs[job]['steps']):
                    if step.get('uses', '').split('@')[0] != 'actions/upload-artifact':
                        continue
                    options = step['with']
                    selected = []
                    for expression in options['path'].splitlines():
                        path = producer / expression
                        if path.is_dir():
                            selected.extend((p.relative_to(path).as_posix(), p.read_bytes())
                                            for p in path.rglob('*') if p.is_file())
                        else:
                            selected.extend((p.name, p.read_bytes()) for p in producer.glob(expression) if p.is_file())
                    if selected:
                        self.assertEqual(len(selected), 1)
                        store[options['name']] = {'producer': job, 'files': dict(selected)}
                        matches.append(options['name'])
                        producer_receipts.append({'job': job, 'step': number, 'name': options['name'],
                                                  'sha256': hashlib.sha256(data).hexdigest()})
                self.assertTrue(matches)
            consumer = root / 'independent-consumer'
            consumer.mkdir()
            observed = []
            for number, step in enumerate(recheck['steps']):
                if step.get('uses', '').split('@')[0] != 'actions/download-artifact':
                    continue
                options = step['with']
                matched = {name: asset for name, asset in store.items()
                           if name == options.get('name') or fnmatch.fnmatchcase(name, options.get('pattern', ''))}
                self.assertTrue(matched)
                for name, asset in matched.items():
                    self.assertIn(asset['producer'], recheck['needs'])
                    destination = consumer / options['path']
                    if 'pattern' in options and not options.get('merge-multiple', False):
                        destination /= name
                    for relative, data in asset['files'].items():
                        target = destination / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(data)
                        observed.append({'job': 'release-package-recheck', 'step': number,
                                         'producer': asset['producer'], 'name': name,
                                         'path': target.relative_to(consumer).as_posix(),
                                         'sha256': hashlib.sha256(target.read_bytes()).hexdigest()})
            expected = {'target/candidate-recheck/' + fixtures[job][0].removeprefix('dist/'): data
                        for job, (_, data) in fixtures.items() if job != 'release-package-build'}
            expected['target/release-package-regression/fixture.crate'] = b'crate'
            self.assertEqual({p.relative_to(consumer).as_posix(): p.read_bytes()
                              for p in consumer.rglob('*') if p.is_file()}, expected)
            self.assertEqual({row['producer'] for row in observed}, set(fixtures))
            self.assertEqual({row['sha256'] for row in observed},
                             {row['sha256'] for row in producer_receipts})
            detail('ci-artifact-handoff.json', {'scope': 'local-action-contract',
                                               'uploads': producer_receipts, 'downloads': observed})


if __name__ == '__main__':
    unittest.main()
