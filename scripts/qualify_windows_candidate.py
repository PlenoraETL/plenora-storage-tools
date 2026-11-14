"""Run the Windows candidate's localhost checks and final artifact verification."""
import argparse
from pathlib import Path
import os
import shutil
import subprocess
import sys
from proof_collection import Collection

ROOT = Path(__file__).resolve().parents[1]


def check(directory):
    directory = directory.resolve()
    folder = directory / 'x86_64-pc-windows-msvc'
    binary = folder / 'plenora-storage.exe'
    if sys.platform != 'win32' or not binary.is_file():
        raise ValueError('Windows candidate verification requires its built Windows executable')
    env = dict(os.environ, PLENORA_CLI_BIN=str(binary))
    collection = Collection()
    for script, arguments, source, destination in [
        ('audit_release_readiness.py', [], 'results.json', 'cli-regressions.json'),
        ('qualify_extended_faults.py', [], 'extended-regressions.json', 'extended-regressions.json'),
        ('qualify_extended_faults.py', ['--spool-uploads', '--output', str(folder / 'spooled-regressions.json')], None, None),
    ]:
        with collection.case(destination or 'spooled-regressions.json'):
            (folder / (destination or 'spooled-regressions.json')).unlink(missing_ok=True)
            path = ROOT / 'target/release-readiness' / source if source else None
            if path:
                path.unlink(missing_ok=True)
            subprocess.run([sys.executable, str(ROOT / 'scripts' / script), *arguments],
                           cwd=ROOT, env=env, check=True)
            if path:
                shutil.copyfile(path, folder / destination)
    with collection.case('verify-release'):
        subprocess.run([sys.executable, str(ROOT / 'scripts/verify_release.py'), str(directory)],
                       cwd=ROOT, check=True)
    collection.finish(ROOT / 'target/release-readiness' / (folder.name + '-collection.json'))



if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    check(parser.parse_args().directory)
