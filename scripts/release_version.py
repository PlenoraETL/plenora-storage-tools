"""Print the workspace release version without requiring a compiled binary."""
from pathlib import Path
import tomllib

if __name__ == '__main__':
    print(tomllib.loads((Path(__file__).resolve().parents[1] / 'Cargo.toml').read_text())['workspace']['package']['version'])
