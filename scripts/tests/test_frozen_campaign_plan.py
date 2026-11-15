"""Execute campaign control flow with observation-only external executors."""
from contextlib import ExitStack, nullcontext
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import campaign_state as state
import run_vm_campaign as vm
import release_campaign as controller
from frozen_proofs import detail
from filesystem_paths import canonical_path

# Reviewed phase contract; shared by expectations and receipt export. Actual
# phases come from executing the functions, not from their AST inventories.
VM_PHASES = ('qualify-linux', 'install-sdk',
             'transfers-large', 'transfers-workers4', 'transfers-workers16',
             'spooled-large', 'spooled-workers4', 'spooled-workers16', 'calibrate-resources', 'soak')
CONTROLLER_PHASES = ('workflows', 'download', 'assemble', 'prepare-vm',
                     'qualify-windows', 'qualify-vm', 'seal')


class CampaignPlanTests(unittest.TestCase):
    def test_vm_executes_each_phase_with_bound_argv_and_ledger_receipt(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            root = Path(temp)
            candidate, private, output, fixtures = (root / n for n in ('candidate', 'private', 'output', 'fixtures'))
            for folder in (candidate, private, fixtures):
                folder.mkdir()
            binary, wheel = candidate / 'plenora-storage', candidate / 'fixture.whl'
            for path in (binary, wheel):
                path.write_bytes(path.name.encode())
            subject = {'version': '4.0.0', 'source_revision': 'a' * 40,
                       'artifacts': {p.name: state.digest(p) for p in (binary, wheel)},
                       'policy_id': 'storage-release-cycles-v1'}
            (candidate / 'release-manifest.json').write_text(json.dumps(subject))
            (fixtures / 'fixture-state.json').write_text(json.dumps({'kind': 'reset', 'nonce': 'this-reset'}))
            observed = []
            def external(argv, folder, **kwargs):
                argv = list(map(str, argv))
                observed.append((folder.parent.name, argv, kwargs))
                receipt = {'scope': 'fake-tools-real-control-flow', 'argv': argv,
                           'binary_sha256': state.digest(binary), 'wheel_sha256': state.digest(wheel)}
                state.write_json(folder / (kwargs.get('name', 'command.log') + '.json'), receipt)
                for flag in ('--output', '--baseline-output'):
                    if flag in argv:
                        state.write_json(Path(argv[argv.index(flag) + 1]), receipt)
            stack.enter_context(patch.object(vm.sys, 'platform', 'linux'))
            stack.enter_context(patch.object(vm, 'identity', return_value=subject))
            stack.enter_context(patch.object(vm, 'ROOT', root))
            stack.enter_context(patch.object(vm, 'logged', side_effect=external))
            stack.enter_context(patch.object(vm, 'qualification_copy', return_value=candidate))
            stack.enter_context(patch.object(vm.venv.EnvBuilder, 'create', side_effect=lambda p: p.mkdir()))
            stack.enter_context(patch.object(vm, 'inspect', return_value={'status': 'PASS'}))
            stack.enter_context(patch.object(vm, 'available_memory', return_value=10**15))
            stack.enter_context(patch.object(vm, 'inspect_memory', return_value={'status': 'PASS'}))
            fence = Mock()
            vm.measure(candidate, private, output, fixtures, [], None, [], 'this-reset',
                       fence, {'candidate': state.digest(binary)}, 'epoch-1')
            ledger = json.loads((output / 'campaign.json').read_text())
            self.assertEqual(tuple(ledger['phases']), VM_PHASES)
            self.assertGreaterEqual(fence.call_count, 4 * len(VM_PHASES))
            self.assertEqual(len(observed), len(VM_PHASES))
            for phase, attempts in ledger['phases'].items():
                self.assertEqual(len(attempts), 1)
                self.assertEqual(attempts[0]['status'], 'PASS')
                self.assertEqual(attempts[0]['files'], state.Campaign.inventory(output / phase / '1'))
            for phase, argv, kwargs in observed:
                if 'qualify_transfers.py' in Path(argv[1]).name:
                    self.assertEqual(canonical_path(kwargs['env']['PLENORA_CLI_BIN']), canonical_path(binary))
                    limit = float(argv[argv.index('--phase-timeout') + 1])
                    self.assertEqual(kwargs['timeout'], limit + 60)
                    self.assertEqual('--spool-uploads' in argv, phase.startswith('spooled-'))
            selected = json.loads((output / 'selected/report.json').read_text())
            self.assertEqual(selected['epoch'], 'epoch-1')
            self.assertEqual(selected['fixture_nonce'], 'this-reset')
            self.assertEqual(selected['identity'], ledger['identity'])
            self.assertEqual(ledger['selected']['report_sha256'], state.digest(output / 'selected/report.json'))
            for name, sha in selected['files'].items():
                self.assertEqual(state.digest(output / 'selected' / name), sha)
            detail('vm-ledger.json', ledger)
            detail('vm-selected.json', selected)
            detail('vm-argv.json', [{'phase': phase, 'argv': argv, 'timeout': kwargs['timeout']}
                                    for phase, argv, kwargs in observed])

    def test_controller_dispatch_has_one_bound_receipt_for_every_phase(self):
        # Executes the controller's phase dispatch, admission lifetime and
        # collection. External phase bodies are deliberately not qualification.
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            root = Path(temp)
            config_path = root / 'config.json'
            config_path.write_text('{}')
            config = {'vm_root': '/fixture', 'host': 'fixture.invalid'}
            phases = []
            class ObservedCampaign(state.Campaign):
                def phase(self, name, action, **kwargs):
                    phases.append(name)
                    return super().phase(name, lambda folder: state.write_json(folder / 'dispatch.json',
                                         {'phase': name, 'identity': self.state['identity'],
                                          'scope': 'dispatch-only'}), **kwargs)
            remote, session = Mock(), Mock()
            stack.enter_context(patch.object(controller.sys, 'platform', 'win32'))
            stack.enter_context(patch.object(controller, 'configuration', return_value=config))
            stack.enter_context(patch.object(controller, 'Campaign', ObservedCampaign))
            stack.enter_context(patch.object(controller, 'Remote', return_value=remote))
            stack.enter_context(patch.object(controller, 'check_private_root'))
            stack.enter_context(patch.object(controller, 'admission', return_value=nullcontext(session)))
            stack.enter_context(patch.object(controller.subprocess, 'check_output',
                                             side_effect=lambda argv, **kw: 'a' * 40 if argv[1] == 'rev-parse' else ''))
            output = root / 'output'
            controller.run(config_path, output, [], None)
            self.assertEqual(tuple(phases), CONTROLLER_PHASES)
            ledger = json.loads((output / 'campaign.json').read_text())
            for phase in phases:
                receipt = json.loads((output / phase / '1/dispatch.json').read_text())
                self.assertEqual(receipt['identity'], ledger['identity'])
                self.assertEqual(ledger['phases'][phase][0]['files'], state.Campaign.inventory(output / phase / '1'))
            remote.client.close.assert_called_once()
            detail('controller-ledger.json', ledger)


if __name__ == '__main__':
    unittest.main()
