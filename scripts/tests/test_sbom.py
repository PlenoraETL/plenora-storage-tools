import copy
import importlib.util
from pathlib import Path
import unittest
import json
import jsonschema
import tempfile
import os
import shutil
import subprocess
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('render_sbom', Path(__file__).resolve().parents[1] / 'render_sbom.py')
sbom = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sbom)


class SBOMTests(unittest.TestCase):
    def test_sboms_match_across_git_checkout_line_endings(self):
        # Exercise Git's real checkout conversion, including on Linux CI.
        # Parsing requirements is insensitive to CRLF, but their byte digests
        # must match when both platform inventories are sealed together.
        inputs = ['.gitattributes', 'Cargo.toml', 'Cargo.lock',
                  'fuzz/Cargo.lock', 'tools/api-inventory/Cargo.lock',
                  'crates/plenora-smb2/PROVENANCE.md']
        inputs.extend(path.relative_to(sbom.ROOT).as_posix()
                      for path in (sbom.ROOT / 'scripts').glob('requirements-*.txt'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / 'repository'
            repository.mkdir()
            env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)

            def git(*arguments):
                subprocess.run(['git', *arguments], cwd=repository, env=env,
                               check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            git('init', '--quiet')
            for name in inputs:
                destination = repository / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(sbom.ROOT / name, destination)
            git('-c', 'core.autocrlf=false', 'add', '--', *inputs)
            inventories = []
            for autocrlf in ('false', 'true'):
                checkout = root / autocrlf
                checkout.mkdir()
                git('-c', 'core.eol=lf', '-c', 'core.autocrlf=' + autocrlf, 'checkout-index', '--all',
                    '--prefix=' + checkout.as_posix() + '/')
                with patch.object(sbom, 'ROOT', checkout):
                    inventories.append((sbom.render(), sbom.render_qualification()))
            self.assertEqual(inventories[0], inventories[1])

    def test_changed_artifact_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / 'artifact.bin'
            artifact.write_bytes(b'qualified')
            document = sbom.render([artifact])
            sbom.verify(document, [artifact])
            artifact.write_bytes(b'changed')
            with self.assertRaises(ValueError):
                sbom.verify(document, [artifact])

    def test_sboms_carry_the_fields_sbom_attestation_requires(self):
        # actions/attest accepts a CycloneDX document only with bomFormat,
        # serialNumber and specVersion; without serialNumber it refuses it as
        # "Unsupported SBOM format" and the release candidate fails.
        import re
        pattern = re.compile(r'urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-8[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}')
        for document in (sbom.render(), sbom.render_qualification()):
            self.assertTrue(document['bomFormat'] and document['specVersion'] and document['serialNumber'])
            self.assertRegex(document['serialNumber'], pattern)
        self.assertNotEqual(sbom.render()['serialNumber'], sbom.render_qualification()['serialNumber'])

    def test_serial_number_is_deterministic_and_follows_the_content(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / 'artifact.bin'
            artifact.write_bytes(b'qualified')
            first = sbom.render([artifact])
            self.assertEqual(first['serialNumber'], sbom.render([artifact])['serialNumber'])
            artifact.write_bytes(b'changed')
            self.assertNotEqual(first['serialNumber'], sbom.render([artifact])['serialNumber'])
            forged = copy.deepcopy(first)
            forged['serialNumber'] = 'urn:uuid:00000000-0000-8000-8000-000000000000'
            artifact.write_bytes(b'qualified')
            with self.assertRaises(ValueError):
                sbom.verify(forged, [artifact])

    def test_official_cyclonedx_schema(self):
        schema = json.loads((sbom.ROOT / 'scripts/schemas/bom-1.6.schema.json').read_text())
        jsonschema.Draft7Validator(schema).validate(sbom.render())
        jsonschema.Draft7Validator(schema).validate(sbom.render_qualification())

    def test_qualification_graphs_stay_separate_and_declarations_are_explicit(self):
        document = sbom.render_qualification()
        refs = {component['bom-ref'] for component in document['components']}
        self.assertEqual(len(refs), len(document['components']))
        for edge in document['dependencies']:
            self.assertTrue(set(edge['dependsOn']).issubset(refs))
        roots = [c for c in document['components'] if c['type'] == 'application']
        self.assertEqual({c['name'] for c in roots}, {'fuzz', 'tools/api-inventory'})
        python = [c for c in document['components'] if c.get('purl', '').startswith('pkg:pypi/')]
        self.assertTrue(any(c['name'] == 'maturin' for c in python))
        self.assertTrue(any(c['name'] == 'mypy' for c in python))
        self.assertTrue(all('declared qualification dependency' in str(c['properties']) for c in python))
        # A declaration must not masquerade as a fully resolved dependency leaf.
        self.assertFalse({c['bom-ref'] for c in python} & {d['ref'] for d in document['dependencies']})
        sbom.verify_qualification(document)
        document['components'].pop()
        with self.assertRaises(ValueError):
            sbom.verify_qualification(document)

    def test_every_dependency_edge_is_resolved(self):
        document = sbom.render()
        refs = {component['bom-ref'] for component in document['components']}
        for edge in document['dependencies']:
            self.assertTrue(set(edge['dependsOn']).issubset(refs))
        sbom.verify(document)

    def test_removed_component_and_changed_dependency_are_rejected(self):
        original = sbom.render()
        changed = copy.deepcopy(original)
        changed['components'].pop()
        with self.assertRaises(ValueError):
            sbom.verify(changed)
        changed = copy.deepcopy(original)
        changed['dependencies'][0]['dependsOn'].append('fake')
        with self.assertRaises(ValueError):
            sbom.verify(changed)


if __name__ == '__main__':
    unittest.main()
