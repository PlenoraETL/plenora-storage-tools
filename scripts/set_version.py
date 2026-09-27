"""Set a release version consistently; refresh the lockfile and generated inventory."""
import argparse
import re
import subprocess
import sys

from versioning import ROOT, parse_version, workspace_version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version')
    args = parser.parse_args()
    version = parse_version(args.version)
    previous = workspace_version()
    # Only workspace identity and internal path dependencies change. Third-party
    # dependency versions and historical evidence must remain untouched.
    manifests = [ROOT / 'Cargo.toml', *sorted((ROOT / 'crates').glob('*/Cargo.toml'))]
    for path in manifests:
        text = path.read_text(encoding='utf-8')
        lines = text.splitlines(keepends=True)
        section = ''
        for index, line in enumerate(lines):
            if line.startswith('['):
                section = line.strip()
            if (path == ROOT / 'Cargo.toml' and section == '[workspace.package]' and line.startswith('version =')) or ('path =' in line and 'version =' in line):
                lines[index] = re.sub(r'version = "' + re.escape(previous.native) + '"',
                                      f'version = "{version.native}"', line)
        path.write_text(''.join(lines), encoding='utf-8', newline='\n')
    path = ROOT / 'crates/plenora-storage-py/pyproject.toml'
    path.write_text(path.read_text(encoding='utf-8').replace(
        f'version = "{previous.python}"', f'version = "{version.python}"'), encoding='utf-8', newline='\n')
    subprocess.run(['cargo', 'metadata', '--offline', '--format-version', '1'],
                   cwd=ROOT, stdout=subprocess.DEVNULL, check=True)
    subprocess.run(['cargo', 'metadata', '--manifest-path', 'fuzz/Cargo.toml', '--offline',
                    '--format-version', '1'],
                   cwd=ROOT, stdout=subprocess.DEVNULL, check=True)
    subprocess.run([sys.executable, str(ROOT / 'scripts/render_state.py')], cwd=ROOT, check=True)


if __name__ == '__main__':
    main()
