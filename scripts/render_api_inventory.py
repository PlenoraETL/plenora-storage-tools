"""Render the reviewed, compiler/runtime-derived API inventory index."""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def render():
    metadata = json.loads((ROOT / 'api/metadata.json').read_text())
    sdk = json.loads((ROOT / 'api/python.json').read_text())
    cli = json.loads((ROOT / 'api/cli.json').read_text())
    lines = ['# Inventario delle API pubbliche', '',
             '<!-- Generato da scripts/render_api_inventory.py. Non modificare a mano. -->', '',
             'Baseline di sviluppo della serie 1.0: [regole e riproduzione](../api/README.md).',
             'Le righe Rust includono implementazioni derivate e blanket impl; il loro',
             'numero non misura copertura o maturità. I file contengono le firme complete.', '',
             '## Rust', '', '| Target | Crate | Righe API |', '| --- | --- | --- |']
    for target in sorted((ROOT / 'api/rust').iterdir()):
        if not target.is_dir():
            continue
        for path in sorted(target.glob('*.txt')):
            lines.append(f'| `{target.name}` | [{path.stem}](../{path.relative_to(ROOT).as_posix()}) | {len(path.read_text().splitlines())} |')
    lines += ['', '## Python', '', 'La [baseline della wheel](../api/python.json) comprende:', '']
    for name, value in sorted(sdk['exports'].items()):
        lines.append(f'- `{name}`: {value["kind"]}.')
    lines += ['', 'Gli alias usati nelle annotazioni e gli attributi di errore sono inclusi',
              'nello stesso snapshot; la versione della wheel è verificata separatamente.', '',
              '## CLI e contratti', '',
              f'Protocollo CLI: **{cli["protocol_version"]}**. [Modello compilato](../api/cli.json)',
              'con parametri, default, obbligatorietà, valori ammessi ed exit code.', '',
              'Comandi: ' + ', '.join(f'`{command["name"]}`' for command in cli['command']['subcommands']) + '.', '',
              f'Requisiti: Rust **{metadata["rust_version"]}**, Python **{metadata["python_requires"]}**.',
              f'[Metadati](../api/metadata.json): {len(metadata["contracts"])} schemi JSON, feature dei crate e riferimento upstream.', '',
              '## Gate', '',
              '[Workflow Rust sui due target](../.github/workflows/api-compatibility.yml),',
              'test CLI e test della wheel nella [CI completa](../.github/workflows/ci.yml).',
              'Il workflow di release richiede gli stessi gate prima del packaging.', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    path = ROOT / 'docs/API-INVENTORY.md'
    actual = render()
    if args.check:
        if path.read_text(encoding='utf-8') != actual:
            raise SystemExit('API inventory index differs; run scripts/render_api_inventory.py')
    else:
        path.write_text(actual, encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
