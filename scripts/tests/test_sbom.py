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
