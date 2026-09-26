"""Export Clap's compiled command model and exit-code mapping without adding a product command."""
import argparse
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'target/api-current/cli.json')
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['cargo', 'test', '--locked', '--offline', '-p', 'plenora-storage-cli', '--bin',
                    'plenora-storage', 'api_inventory::cli_public_api_matches_baseline', '--', '--exact'],
                   cwd=ROOT, env=dict(os.environ, PLENORA_API_SNAPSHOT_OUTPUT=str(args.output)), check=True)
    if not args.output.is_file():
        raise SystemExit('CLI snapshot was not generated')
    print('CANDIDATE', args.output)


if __name__ == '__main__':
    main()
