"""Reject panic primitives in library code of every workspace crate.

The single definition of the anti-panic gate, run by CI on Linux and Windows
and by `verify.sh`, so the three cannot drift apart. Tests, examples and
benches are not library code and keep their assertions; feature-gated test
support (`plenora-smb2`'s `testing`/`fuzzing` features) is not compiled here.
"""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
DENIED = (
    'unsafe-code',
    'clippy::unwrap_used',
    'clippy::expect_used',
    'clippy::panic',
    'clippy::unreachable',
    'clippy::todo',
    'clippy::unimplemented',
)


def command():
    flags = [part for lint in DENIED for part in ('-D', lint)]
    return ['cargo', 'clippy', '--workspace', '--lib', '--locked', '--', *flags]


def main():
    subprocess.run(command(), cwd=ROOT, check=True)
    print('PASS anti-panic gate on workspace libraries')


if __name__ == '__main__':
    main()
