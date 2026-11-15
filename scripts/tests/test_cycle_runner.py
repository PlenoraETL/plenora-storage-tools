"""Execute SDK cycle orchestration with a synthetic SDK; not provider evidence."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release_evidence import validate_soak


class RunnerTests(unittest.TestCase):
    def test_runner_emits_the_exact_report_accepted_by_the_gate(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            installed = root / '__init__.py'
            installed.write_bytes(b'synthetic SDK')
            wheel = root / 'fixture.whl'
            with zipfile.ZipFile(wheel, 'w') as archive:
                archive.writestr('plenora_storage/__init__.py', installed.read_bytes())
            class Error(Exception):
                category = 'cancelled'
            class Token:
                cancelled = False
                def cancel(self): self.cancelled = True
            objects = {}
            class Engine:
                def __init__(self, *args, **kwargs): self.resolver = kwargs.get('credential_resolver')
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def put(self, connection, key, source, *, overwrite, publication_policy, **options):
                    if self.resolver and connection.provider != 'local': self.resolver(connection.credential_ref)
                    if options.get('cancellation') and options['cancellation'].cancelled: raise Error()
                    objects[connection.provider, key] = source.read_bytes()
                def copy(self, connection, source, destination, **options):
                    objects[connection.provider, destination] = objects[connection.provider, source]
                def get(self, connection, key, destination, **options):
                    data = objects[connection.provider, key]
                    destination.write_bytes(data)
                    return {'checksum': {'value': hashlib.sha256(data).hexdigest()}}
                def delete(self, connection, key, **options): objects.pop((connection.provider,key), None)
            sdk = SimpleNamespace(__file__=str(installed), version=lambda:'4.0.0', Engine=Engine,
                                  Connection=lambda **kw:SimpleNamespace(**kw), EngineConfig=lambda **kw:kw,
                                  StorageError=Error, CancellationToken=Token)
            spec = importlib.util.spec_from_file_location('cycle_runner', Path(__file__).parents[1]/'stress_python.py')
            module = importlib.util.module_from_spec(spec)
            output = root/'report.json'
            with patch.dict(sys.modules, plenora_storage=sdk): spec.loader.exec_module(module)
            with patch.object(module.sys, 'platform', 'linux'), patch.object(module.sys,'argv',
                    ['stress_python.py','--wheel',str(wheel),'--output',str(output),'--both-upload-modes']), \
                    patch.object(module, 'fixture', side_effect=lambda p, local:({'provider':p},{})), \
                    patch.object(module, 'resources', return_value=dict(rss_bytes=1000, threads=4, file_descriptors=10)), \
                    patch.dict(module.os.environ), patch('builtins.print'):
                module.main()
            report = json.loads(output.read_text())
            validate_soak(report, hashlib.sha256(wheel.read_bytes()).hexdigest(), '4.0.0', both_upload_modes=True)
            self.assertEqual(report['completed_cycles'],40)
            self.assertEqual(objects,{})

if __name__ == '__main__': unittest.main()
