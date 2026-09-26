"""Copy public test reports from the Docker target volume to the checkout mount."""
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def main():
    destination = ROOT / '.fixtures/evidence'
    destination.mkdir(parents=True, exist_ok=True)
    for pattern in ('release-readiness/*.json', 'python-wheels/python-tests.*'):
        for source in (ROOT / 'target').glob(pattern):
            if source.is_file() and not source.is_symlink():
                shutil.copyfile(source, destination / source.name)


if __name__ == '__main__':
    main()
