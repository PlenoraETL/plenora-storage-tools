"""Print the workspace release version without requiring a compiled binary."""
import argparse
from versioning import workspace_version

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', action='store_true', help='Print the normalized wheel version')
    args = parser.parse_args()
    version = workspace_version()
    print(version.python if args.python else version.native)
