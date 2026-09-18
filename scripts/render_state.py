"""Generate the current product inventory from manifests and executable discovery."""
import argparse
import json
from pathlib import Path
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def render():
    workspace = tomllib.loads((ROOT / 'Cargo.toml').read_text())['workspace']
    version = workspace['package']['version']
    result = subprocess.run(['cargo', 'run', '--quiet', '--locked', '--offline', '-p',
                             'plenora-storage-cli', '--features', 'full', '--', '--format', 'json',
                             'capabilities'], cwd=ROOT, check=True, capture_output=True, text=True)
    catalog = json.loads(result.stdout)['result']
    assert catalog['component_version'] == version
    providers = {}
    for operation in catalog['operations']:
        for provider in operation['attributes']['providers']:
            providers[provider['provider']] = provider
    lines = ['# Stato del prodotto', '', '<!-- Generato da scripts/render_state.py. Non modificare a mano. -->', '',
             f'Versione sorgente: `{version}`. Rust minimo: `{workspace["package"]["rust-version"]}`.', '',
             'Questo inventario descrive il codice compilato. Non certifica una release:',
             'la qualifica richiede le evidenze vincolate al commit e ai digest degli artefatti.', '',
             '## Crate del workspace', '', '| Crate | Versione |', '| --- | --- |']
    for member in workspace['members']:
        package = tomllib.loads((ROOT / member / 'Cargo.toml').read_text())['package']
        own_version = package['version'] if isinstance(package['version'], str) else version
        lines.append(f'| `{package["name"]}` | `{own_version}` |')
    lines += ['', '## Provider compilati nella distribuzione completa', '',
              '| Feature | Contratto di connessione | Operazioni |', '| --- | --- | --- |']
    for name, provider in sorted(providers.items()):
        lines.append(f'| `{name}` | `{provider["config_contract"]}` | {len(provider["operations"])} |')
    lines += ['', 'Le feature sono condivise da engine, CLI e binding Python. `default = full`;',
              '`--no-default-features --features local,s3` compila solo i provider richiesti.', '',
              '## Operazioni', '', '| Operazione | Input | Output |', '| --- | --- | --- |']
    for op in catalog['operations']:
        lines.append(f'| `{op["id"]}` | `{op["input"]["contract"]}` | `{op["output"]["contract"]}` |')
    source = json.loads((ROOT / 'contracts/upstream/source.json').read_text())
    lines += ['', '## Contratti bloccati', '', '```json', json.dumps(source, indent=2, sort_keys=True), '```', '',
              '## Superfici e limiti', '',
              '- Rust: core neutrale e factory applicativa `plenora_storage_engine::build_engine`.',
              '- CLI: protocollo JSON e trasferimenti su file.',
              '- Runtime Binding: nel core, con resolver posseduti dal consumer.',
              '- Python: wheel PyO3, API sincrona e asyncio, trasferimenti su file e tipi PEP 561.',
              '- Il catalogo esposto da Python descrive i provider Rust della wheel;',
              '  non dichiara automaticamente conformità al profilo Python upstream.',
              '- Limiti, sistemi qualificati e prove richieste: [allineamento](database-alignment.md),',
              '  [provider](provider-expansion.md) e [release](release.md).', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    path = ROOT / 'docs/STATO.md'
    expected = render()
    if args.check:
        if not path.exists() or path.read_text(encoding='utf-8') != expected:
            raise SystemExit('docs/STATO.md differs: run python scripts/render_state.py')
    else:
        path.write_text(expected, encoding='utf-8')


if __name__ == '__main__':
    main()
