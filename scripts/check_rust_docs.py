"""Build owned public documentation and run doctests with warnings rejected."""
import os
from pathlib import Path
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def main():
    members = tomllib.loads((ROOT / 'Cargo.toml').read_text())['workspace']['members']
    packages = [Path(member).name for member in members
                if Path(member).name.startswith('plenora-storage-')]
    selection = [part for package in packages for part in ('-p', package)]
    environment = dict(os.environ, RUSTDOCFLAGS='-D warnings')
    subprocess.run(['cargo', 'doc', '--locked', '--offline', '--all-features', '--no-deps',
                    *selection], cwd=ROOT, env=environment, check=True)
    subprocess.run(['cargo', 'test', '--locked', '--offline', '--all-features', '--doc',
                    *selection], cwd=ROOT, env=environment, check=True)
    print('PASS owned public rustdoc and doctests')


if __name__ == '__main__':
    main()
