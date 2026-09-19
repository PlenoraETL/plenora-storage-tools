"""Release policy: stable SemVer or alpha/beta/rc.N, with explicit wheel versions."""
from dataclasses import dataclass
from pathlib import Path
import re
import tomllib

_VERSION = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-(alpha|beta|rc)\.([1-9][0-9]*))?')
ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ReleaseVersion:
    native: str
    core: tuple[int, int, int]
    stage: str | None
    serial: int | None

    @property
    def python(self):
        base = '.'.join(map(str, self.core))
        return base if self.stage is None else base + {'alpha': 'a', 'beta': 'b', 'rc': 'rc'}[self.stage] + str(self.serial)

    def requires(self, minimum):
        # A prerelease must pass the same gates as its final version.
        return self.core >= minimum


def parse_version(value):
    match = _VERSION.fullmatch(value)
    if match is None:
        raise ValueError(f'unsupported release version: {value!r}')
    major, minor, patch, stage, serial = match.groups()
    return ReleaseVersion(value, (int(major), int(minor), int(patch)), stage, int(serial) if serial else None)


def workspace_version(root=ROOT):
    return parse_version(tomllib.loads((root / 'Cargo.toml').read_text(encoding='utf-8'))['workspace']['package']['version'])
