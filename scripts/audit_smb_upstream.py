"""Audit the vendored SMB package under its original RustSec package identity."""
import json
from pathlib import Path
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_VERSION = '0.22.1'
UPSTREAM_CHECKSUM = 'c04c8f7cb2f27fbbd4d8d3839f5f1e197423613a94921bf83ec059147d6e6f16'


def main():
    source = (ROOT / 'Cargo.lock').read_text()
    package = next(p for p in tomllib.loads(source)['package'] if p['name'] == 'plenora-smb2')
    marker = f'name = "plenora-smb2"\nversion = "{package["version"]}"'
    replacement = f'name = "smb2"\nversion = "{UPSTREAM_VERSION}"\nsource = "registry+https://github.com/rust-lang/crates.io-index"\nchecksum = "{UPSTREAM_CHECKSUM}"'
    assert source.count(marker) == 1
    source = source.replace(marker, replacement).replace(' "plenora-smb2",', ' "smb2",')
    output = ROOT / 'target/release-readiness'
    output.mkdir(parents=True, exist_ok=True)
    lockfile = output / 'smb-upstream.lock'
    lockfile.write_text(source)
    result = subprocess.run(['cargo', 'audit', '--file', str(lockfile), '--json'], capture_output=True, text=True, cwd=ROOT)
    report = json.loads(result.stdout)
    (output / 'smb-upstream-audit.json').write_text(json.dumps(report, indent=2) + '\n')
    assert result.returncode == 0 and report['vulnerabilities']['count'] == 0 and not report.get('warnings'), result.stdout + result.stderr
    print('Vendored smb2 upstream audit: PASS')


if __name__ == '__main__':
    main()
