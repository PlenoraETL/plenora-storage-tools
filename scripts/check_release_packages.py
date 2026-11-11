"""Exercise the release package guard on CI-produced archives, without fixtures."""
import argparse
from pathlib import Path

from check_packages import registry, repository_snapshot, verify_distribution
from versioning import workspace_version


@repository_snapshot
def check(directory):
    version = workspace_version().native
    expected = {f'{name}-{version}.crate' for name in registry()['crates']
                if name != 'plenora-storage-py'}
    actual = {path.name for path in directory.iterdir()}
    if actual != expected:
        raise ValueError('release regression requires the complete Cargo archive set')
    for name in sorted(expected):
        # Same entry point as verify_release and publication prepare/upload/publish;
        # no mocked Git inputs, metadata or policy, and no rebuild of input bytes.
        verify_distribution(directory / name)
    print(f'PASS release package guard ({len(expected)} built archives)')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    check(parser.parse_args().directory)
