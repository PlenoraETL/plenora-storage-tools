"""Run an argv command, preserving UTF-8 tool output and its actual exit code."""
from pathlib import Path
import subprocess
import sys


def main():
    if len(sys.argv) < 3:
        raise SystemExit('usage: run_logged.py LOG COMMAND [ARG ...]')
    path = Path(sys.argv[1])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('wb') as output:
        result = subprocess.run(sys.argv[2:], stdout=output, stderr=subprocess.STDOUT)
    print(f'exit={result.returncode}; log={path}', flush=True)
    if result.returncode:
        # A bounded tail makes CI failures actionable without dumping full logs.
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - 16_384))
            print(stream.read().decode('utf-8', errors='replace'))
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
