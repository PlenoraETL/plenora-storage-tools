"""Prove the Rust API gate rejects real compiler-resolved breaking changes."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
from check_rust_api import ROOT, TOOL, differences


BASE = '''
pub struct Client;
impl Client { pub fn close(&self) {} }
pub enum Outcome { Ready }
pub fn consume(value: u32) -> u32 { value }
'''


def main():
    tool_target = Path(os.environ.get('CARGO_TARGET_DIR', TOOL.parent / 'target')).resolve()
    tool = tool_target / 'debug' / ('storage-api-inventory.exe' if os.name == 'nt' else 'storage-api-inventory')
    cases = {
        'removed_method': BASE.replace('pub fn close(&self) {}', ''),
        'changed_parameter_type': BASE.replace('value: u32', 'value: u16').replace('{ value }', '{ value.into() }'),
        'added_exhaustive_enum_variant': BASE.replace('Ready }', 'Ready, Failed }'),
        'lost_send_sync': BASE.replace('pub struct Client;', 'pub struct Client(std::rc::Rc<()>);'),
    }
    with tempfile.TemporaryDirectory(prefix='storage-api-gate-') as temporary:
        folder = Path(temporary)

        def inventory(source):
            path = folder / 'lib.rs'
            path.write_text(source)
            subprocess.run(['rustdoc', '--edition', '2024', '--crate-name', 'api_gate_fixture',
                            '-Z', 'unstable-options', '--output-format', 'json', '-o', str(folder), str(path)],
                           env=dict(os.environ, RUSTC_BOOTSTRAP='1'), check=True, capture_output=True)
            return subprocess.run([str(tool), str(folder / 'api_gate_fixture.json')], check=True,
                                  capture_output=True, text=True).stdout

        baseline = inventory(BASE)
        assert not differences(baseline, inventory(BASE.replace('{ value }', '{ value.saturating_add(1) }'))), 'private body affected signature gate'
        for name, source in cases.items():
            assert differences(baseline, inventory(source)), f'gate missed {name}'
            print('PASS negative Rust API case:', name)
    output = ROOT / 'target/api-current/negative-tests.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({'status': 'PASS', 'rejected': list(cases), 'body_only_change_accepted': True}, indent=2) + '\n')


if __name__ == '__main__':
    main()
