"""Qualify a downloaded ABI3 wheel on the selected Python interpreter (>=3.10)."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('artifacts', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    wheels = list(args.artifacts.rglob('plenora_storage-*.whl'))
    if len(wheels) != 1:
        raise ValueError('expected one downloaded wheel for this platform')
    wheel = wheels[0].resolve()
    subprocess.run([sys.executable, '-I', '-m', 'pip', 'install', '--no-index', '--force-reinstall', str(wheel)], check=True)
    with tempfile.TemporaryDirectory(prefix='storage-sdk-compatibility-') as temporary:
        result = subprocess.run([sys.executable, '-I', '-m', 'unittest', 'discover', '-s',
                                 str(ROOT / 'crates/plenora-storage-py/python/tests'), '-v'],
                                cwd=temporary, capture_output=True, text=True)
    args.output.mkdir(parents=True, exist_ok=True)
    log = args.output / 'sdk-tests.log'
    log.write_text(result.stdout + result.stderr, encoding='utf-8')
    report = {'status': 'PASS' if result.returncode == 0 else 'FAIL', 'python': platform.python_version(),
              'platform': sys.platform, 'wheel': wheel.name, 'wheel_sha256': hashlib.sha256(wheel.read_bytes()).hexdigest(),
              'tests_log_sha256': hashlib.sha256(log.read_bytes()).hexdigest()}
    (args.output / 'sdk-tests.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    if result.returncode:
        print(result.stdout + result.stderr)
        raise SystemExit(result.returncode)
    print(json.dumps(report))


if __name__ == '__main__':
    main()
