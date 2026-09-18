"""Compile isolated provider selections and verify the resulting CLI catalog."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--features', nargs='*')
    args = parser.parse_args()
    manifest = tomllib.loads((ROOT / 'crates/plenora-storage-engine/Cargo.toml').read_text())
    providers = manifest['features']['full']
    for feature in args.features or ['none', *providers, 'full']:
        command = ['cargo', 'clippy', '--locked', '--offline', '-p', 'plenora-storage-cli',
                   '--all-targets', '--no-default-features']
        if feature != 'none':
            command += ['--features', feature]
        subprocess.run(command + ['--', '-D', 'warnings'], cwd=ROOT, check=True)
        command[1] = 'run'
        command.remove('--all-targets')
        result = subprocess.run(command + ['--quiet', '--', '--format', 'json', 'capabilities'],
                                cwd=ROOT, check=True, capture_output=True, text=True)
        catalog = json.loads(result.stdout)['result']
        actual = {provider['provider'] for operation in catalog['operations']
                  for provider in operation['attributes']['providers']}
        expected = set(providers if feature == 'full' else [] if feature == 'none' else [feature])
        assert actual == expected, (feature, actual, expected)
        if feature != 'full':
            absent = next(name for name in providers if name not in actual)
            with tempfile.TemporaryDirectory(prefix='storage-disabled-') as temporary:
                folder = Path(temporary)
                connection = folder / 'connection.json'
                connection.write_text(json.dumps({'provider': absent,
                    'config_contract': f'plenora-storage-{absent}-connection-v1',
                    'config': {}, 'credential_ref': 'vault:must-not-resolve'}))
                output = folder / 'must-not-exist'
                rejected = subprocess.run(command + ['--quiet', '--', '--format', 'json', 'get',
                    '--connection', str(connection), '--key', 'valid', '--output', str(output),
                    '--overwrite', 'false'], cwd=ROOT, capture_output=True, text=True)
                assert rejected.returncode != 0
                error = json.loads(rejected.stdout)['error']
                assert error['category'] == 'unsupported' and error['remote_effect'] == 'none', error
                assert not output.exists() and not list(folder.glob('*.part'))
        print(f'PASS provider selection: {feature}', flush=True)


if __name__ == '__main__':
    main()
