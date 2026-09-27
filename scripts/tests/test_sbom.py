import copy
import importlib.util
from pathlib import Path
import unittest
import json
import jsonschema
import tempfile

spec = importlib.util.spec_from_file_location('render_sbom', Path(__file__).resolve().parents[1] / 'render_sbom.py')
sbom = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sbom)


class SBOMTests(unittest.TestCase):
    def test_changed_artifact_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / 'artifact.bin'
            artifact.write_bytes(b'qualified')
            document = sbom.render([artifact])
            sbom.verify(document, [artifact])
            artifact.write_bytes(b'changed')
            with self.assertRaises(ValueError):
                sbom.verify(document, [artifact])

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
