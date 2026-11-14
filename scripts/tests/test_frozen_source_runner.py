"""Run the source packaging/consumer pipeline with an observed tool boundary."""
from contextlib import ExitStack
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import package_source as source
from frozen_proofs import detail


class SourceRunnerTests(unittest.TestCase):
    def test_consumer_uses_extracted_reviewed_bytes_and_receipt_rejects_tool_failure(self):
        committed = {'Cargo.toml': b'[workspace]\n', 'Cargo.lock': b'# fixture\n',
                     'rust-toolchain.toml': b'[toolchain]\nchannel="1.98.1"\n', 'LICENSE': b'fixture',
                     'contracts/upstream/source.json': b'{}', 'crates/plenora-smb2/PROVENANCE.md': b'fixture',
                     'crates/plenora-storage-core/Cargo.toml': b'# core fixture',
                     'crates/plenora-storage-engine/Cargo.toml': b'# engine fixture'}
        observations = []
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
                root = Path(temp)
                events = []
                def tool(argv, **kwargs):
                    if argv[0] == 'git':
                        raw = Path(next(arg.split('=', 1)[1] for arg in argv if arg.startswith('--output=')))
                        prefix = next(arg.split('=', 1)[1] for arg in argv if arg.startswith('--prefix='))
                        with tarfile.open(raw, 'w') as stream:
                            for name in [*committed, 'untracked-extra']:
                                info = tarfile.TarInfo(prefix + name)
                                data = b'untrusted producer bytes'
                                info.size = len(data)
                                stream.addfile(info, io.BytesIO(data))
                        events.append('git-input')
                    else:
                        self.assertEqual(argv[:4], ['cargo', 'run', '--offline', '--manifest-path'])
                        consumer = Path(kwargs['cwd'])
                        self.assertEqual(Path(argv[4]), consumer / 'Cargo.toml')
                        self.assertNotEqual(consumer, source.ROOT)
                        extracted = consumer.parent / 'extracted/plenora-storage-4.0.0'
                        self.assertEqual({p.relative_to(extracted).as_posix(): p.read_bytes()
                                          for p in extracted.rglob('*') if p.is_file()}, committed)
                        manifest = (consumer / 'Cargo.toml').read_text()
                        self.assertNotIn('[patch.', manifest)
                        self.assertIn(str(extracted / 'crates/plenora-storage-core').replace('\\', '\\\\'), manifest)
                        self.assertEqual(kwargs['env']['RUSTUP_TOOLCHAIN'], '1.98.1')
                        self.assertEqual(kwargs['env']['CARGO_TERM_COLOR'], 'never')
                        kwargs['stdout'].write(b'observed fake tool\n')
                        events.append('consumer')
                    return subprocess.CompletedProcess(argv, 5 if failure and argv[0] == 'cargo' else 0)
                def verify(path):
                    with tarfile.open(path) as stream:
                        self.assertEqual({m.name.removeprefix('plenora-storage-4.0.0/'): stream.extractfile(m).read()
                                          for m in stream.getmembers()}, committed)
                    events.append('verify')
                stack.enter_context(patch.object(source, 'registry', return_value={'source': list(committed)}))
                stack.enter_context(patch.object(source, 'checkout_source_bytes', side_effect=committed.__getitem__))
                stack.enter_context(patch.object(source, 'verify_distribution', side_effect=verify))
                stack.enter_context(patch.object(source.subprocess, 'check_output',
                                                 side_effect=lambda argv, **kw: 'a' * 40 if argv[1] == 'rev-parse' else b''))
                stack.enter_context(patch.object(source.subprocess, 'run', side_effect=tool))
                output = root / 'release'
                if failure:
                    with self.assertRaises(RuntimeError):
                        source.build(output, root / 'isolated-target')
                    self.assertFalse((output / 'source-consumer.json').exists())
                else:
                    archive, report, log = source.build(output, root / 'isolated-target')
                    receipt = json.loads(report.read_text())
                    self.assertEqual(receipt['source_revision'], 'a' * 40)
                    self.assertEqual(receipt['archive_sha256'], hashlib.sha256(archive.read_bytes()).hexdigest())
                    self.assertEqual(receipt['log_sha256'], hashlib.sha256(log.read_bytes()).hexdigest())
                    observations.append({'scope': 'fake-cargo-real-source-pipeline', 'receipt': receipt})
                self.assertEqual(events, ['git-input', 'verify', 'consumer'])
        detail('source-runner-receipt.json', observations)


if __name__ == '__main__':
    unittest.main()
