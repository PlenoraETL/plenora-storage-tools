"""Build a wheel, install it in an isolated environment and test the installed SDK."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import venv
from versioning import workspace_version

ROOT = Path(__file__).resolve().parents[1]


def build(output, release=True, environment=None):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    command = ['maturin', 'build', '--locked', '--offline', '--manifest-path',
               str(ROOT / 'crates/plenora-storage-py/Cargo.toml'), '--out', str(output),
               '--interpreter', sys.executable]
    if release:
        command.append('--release')
    subprocess.run(command, cwd=ROOT, check=True)
    version = workspace_version()
    wheels = sorted(output.glob(f'plenora_storage-{version.python}-*.whl'))
    if len(wheels) != 1:
        raise ValueError('expected exactly one storage wheel in the output directory')
    wheel = wheels[0]
    environment = Path(environment).resolve() if environment is not None else ROOT / 'target/python-sdk-test'
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    subprocess.run([str(python), '-m', 'pip', 'install', '--no-index', '--force-reinstall', str(wheel)], check=True)
    with tempfile.TemporaryDirectory(prefix='storage-wheel-tests-') as temporary:
        result = subprocess.run([str(python), '-I', '-m', 'unittest', 'discover', '-s',
                                 str(ROOT / 'crates/plenora-storage-py/python/tests'), '-v'],
                                cwd=temporary, capture_output=True, text=True)
    log = output / 'python-tests.log'
    log.write_text(result.stdout + result.stderr, encoding='utf-8')
    if result.returncode:
        raise RuntimeError(f'installed Python SDK tests failed: {log}')
    report = {'status': 'PASS', 'wheel': wheel.name,
              'wheel_sha256': hashlib.sha256(wheel.read_bytes()).hexdigest(),
              'tests_log_sha256': hashlib.sha256(log.read_bytes()).hexdigest()}
    report_path = output / 'python-tests.json'
    report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return [wheel, log, report_path]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'target/python-wheels')
    parser.add_argument('--debug', action='store_true')
    parser.add_argument('--environment', type=Path, help='Isolated test environment, independent of concurrent campaigns')
    args = parser.parse_args()
    for artifact in build(args.output, release=not args.debug, environment=args.environment):
        print(artifact)
