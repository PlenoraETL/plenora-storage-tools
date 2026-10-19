import ast
import io
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import strict_json
import campaign_state
import check_features
import release_campaign
import release_publication
import render_state

BAD = ('{"PRIVATE_KEY":"PRIVATE_VALUE","PRIVATE_KEY":false}',
       '{"outer":[{"PRIVATE_KEY":1,"PRIVATE_KEY":"PRIVATE_VALUE"}]}',
       '{"key":1,"k\\u0065y":2}')
CLEAN = '{"outer":[{"key":1},{"key":2}],"null":null,"number":1.5}'


class UniqueJsonTests(unittest.TestCase):
    def test_recursive_parser_and_stream(self):
        for loader in (strict_json.loads, lambda text: strict_json.load(io.StringIO(text))):
            for document in BAD:
                with self.subTest(document=document), self.assertRaisesRegex(
                        strict_json.DuplicateJsonMember, '^JSON document contains duplicate members$'):
                    loader(document)
            self.assertEqual(loader(CLEAN), json.loads(CLEAN))

    def test_every_script_parse_site_uses_the_strict_parser(self):
        # Exercise the parser selected at every source ingress, including deferred
        # argparse callbacks, without running publication or remote commands.
        count = 0
        for path in Path(__file__).resolve().parents[1].glob('*.py'):
            if path.name == 'strict_json.py':
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute) or node.attr not in ('loads', 'load', 'JSONDecoder'):
                    continue
                if not isinstance(node.value, ast.Name) or node.value.id not in ('json', 'strict_json'):
                    continue
                with self.subTest(path=path.name, line=node.lineno):
                    self.assertEqual(node.value.id, 'strict_json')
                    loader = eval(compile(ast.Expression(node), str(path), 'eval'), {'strict_json': strict_json})
                    self.assertEqual(loader(CLEAN), json.loads(CLEAN))
                    for document in BAD:
                        with self.assertRaises(strict_json.DuplicateJsonMember):
                            loader(document)
                    count += 1
        self.assertGreater(count, 100)

    def test_discovery_rejects_before_validation(self):
        for document in BAD:
            with self.subTest(document=document):
                with patch.object(render_state.subprocess, 'run') as run:
                    run.return_value.stdout = document
                    with self.assertRaises(strict_json.DuplicateJsonMember):
                        render_state.render()
                with patch.object(check_features.subprocess, 'run') as run, patch.object(
                        sys, 'argv', ['check_features', '--features', 'full']):
                    run.return_value.stdout = document
                    with self.assertRaises(strict_json.DuplicateJsonMember):
                        check_features.main()
                identity = json.dumps({'status': 'ok', 'command': 'version', 'protocol_version': 2,
                                      'component_version': '3', 'result': {
                                          'component_version': '3', 'cli_protocol_version': 2}})
                for outputs in ([document], [identity, document]):
                    with patch.object(release_publication.subprocess, 'check_output', side_effect=outputs):
                        with self.assertRaises(strict_json.DuplicateJsonMember):
                            release_publication.smoke_cli(Path('unused'), '3')

    def test_campaign_state_rejects_before_identity_or_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            for document in BAD:
                (folder / 'campaign.json').write_text(document)
                with self.assertRaises(strict_json.DuplicateJsonMember):
                    campaign_state.Campaign(folder, {})
            (folder / 'campaign.json').write_text('{"identity":{},"selected":{}}')
            self.assertEqual(campaign_state.Campaign(folder, {}).state['identity'], {})

    def test_campaign_configuration_rejects_before_options_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'configuration.json'
            for document in BAD:
                path.write_text(document)
                with self.assertRaises(strict_json.DuplicateJsonMember):
                    release_campaign.configuration(path)
            clean = {'candidate_run': '1', 'ci_run': '2', 'vm_root': '/srv/storage/qualification'}
            path.write_text(json.dumps(clean))
            self.assertEqual(release_campaign.configuration(path), clean)

    def test_clean_discovery_reaches_each_consumer(self):
        root = Path(__file__).resolve().parents[2]
        version = tomllib.loads((root / 'Cargo.toml').read_text())['workspace']['package']['version']
        providers = tomllib.loads((root / 'crates/plenora-storage-engine/Cargo.toml').read_text())['features']['full']
        rows = [{'provider': name, 'config_contract': name, 'operations': []} for name in providers]
        catalog = {'status': 'ok', 'result': {'component_version': version, 'operations': [
            {'id': f'operation{index}', 'input': {'contract': 'input'}, 'output': {'contract': 'output'},
             'attributes': {'providers': rows}} for index in range(7)]}}
        document = json.dumps(catalog)
        with patch.object(render_state.subprocess, 'run') as run:
            run.return_value.stdout = document
            self.assertIn(version, render_state.render())
        with patch.object(check_features.subprocess, 'run') as run, patch.object(
                sys, 'argv', ['check_features', '--features', 'full']):
            run.return_value.stdout = document
            check_features.main()
        identity = json.dumps({'status': 'ok', 'command': 'version', 'protocol_version': 2,
                               'component_version': version, 'result': {
                                   'component_version': version, 'cli_protocol_version': 2}})
        with patch.object(release_publication.subprocess, 'check_output', side_effect=[identity, document]), patch.object(
                release_publication, 'digest', return_value='digest'):
            self.assertEqual(release_publication.smoke_cli(Path('unused'), version)['status'], 'PASS')
