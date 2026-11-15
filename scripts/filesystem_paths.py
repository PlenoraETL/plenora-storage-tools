"""Canonical identities for local filesystem paths, never archive member names."""
from pathlib import Path


def canonical_path(path):
    """Resolve aliases (including Windows 8.3) before comparing local paths."""
    return Path(path).resolve()
