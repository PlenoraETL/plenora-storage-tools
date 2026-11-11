"""Run the Windows candidate's localhost checks and final artifact verification."""
import argparse
from pathlib import Path
import os
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def check(directory):
    directory = directory.resolve()
    folder = directory / 'x86_64-pc-windows-msvc'
    binary = folder / 'plenora-storage.exe'
    if sys.platform != 'win32' or not binary.is_file():
        raise ValueError('Windows candidate verification requires its built Windows executable')
    env = dict(os.environ, PLENORA_CLI_BIN=str(binary))
    for script, arguments in [
        ('audit_release_readiness.py', []),
        ('qualify_extended_faults.py', []),
        ('qualify_extended_faults.py', ['--spool-uploads', '--output', str(folder / 'spooled-regressions.json')]),
    ]:
        subprocess.run([sys.executable, str(ROOT / 'scripts' / script), *arguments],
                       cwd=ROOT, env=env, check=True)
    for source, destination in [('extended-regressions.json', 'extended-regressions.json'),
                                ('results.json', 'cli-regressions.json')]:
        shutil.copyfile(ROOT / 'target/release-readiness' / source, folder / destination)
    subprocess.run([sys.executable, str(ROOT / 'scripts/verify_release.py'), str(directory)],
                   cwd=ROOT, check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    check(parser.parse_args().directory)
