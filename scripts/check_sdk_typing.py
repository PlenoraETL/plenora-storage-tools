"""Check a consumer outside the checkout against the installed SDK wheel."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def check(wheel, output):
    output.mkdir(parents=True, exist_ok=True)
    (output / 'sdk-typing.json').write_text('{"status": "RUNNING"}\n', encoding='utf-8')
    import plenora_storage
    installed = Path(plenora_storage.__file__).resolve().parent
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.startswith('plenora_storage/') and not name.endswith('/'):
                if (installed / name.removeprefix('plenora_storage/')).read_bytes() != archive.read(name):
                    raise ValueError('typing must check exactly the installed candidate wheel')
    version = importlib.metadata.version('mypy')
    if version != '2.3.1':
        raise ValueError('install scripts/requirements-sdk-typing.txt')
    consumer = ROOT / 'crates/plenora-storage-py/typing/consumer.py'
    with tempfile.TemporaryDirectory(prefix='storage-sdk-typing-') as temporary:
        path = Path(temporary) / 'consumer.py'
        path.write_bytes(consumer.read_bytes())
        config = Path(temporary) / 'mypy.ini'
        config.write_text('[mypy]\nstrict = True\n', encoding='utf-8')
        environment = {k: v for k, v in os.environ.items() if k not in {'MYPYPATH', 'PYTHONPATH', 'PYTHONHOME'}}
        process = subprocess.run([sys.executable, '-I', '-m', 'mypy', '--strict', '--no-incremental',
                                  '--config-file', str(config),
                                  '--python-version', '.'.join(platform.python_version_tuple()[:2]),
                                  '--follow-imports=normal', str(path)],
                                 cwd=temporary, env=environment, text=True, capture_output=True)
    output.mkdir(parents=True, exist_ok=True)
    log = output / 'sdk-typing.log'
    log.write_text(process.stdout + process.stderr, encoding='utf-8')
    report = dict(status='PASS' if process.returncode == 0 else 'FAIL', python=platform.python_version(),
                  platform=sys.platform, mypy=version, wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
                  consumer_sha256=hashlib.sha256(consumer.read_bytes()).hexdigest(),
                  log_sha256=hashlib.sha256(log.read_bytes()).hexdigest())
    (output / 'sdk-typing.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    if process.returncode:
        raise ValueError('installed SDK typing failed; inspect sdk-typing.log')
    return report


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheel', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    arguments = parser.parse_args()
    check(arguments.wheel.resolve(), arguments.output.resolve())
