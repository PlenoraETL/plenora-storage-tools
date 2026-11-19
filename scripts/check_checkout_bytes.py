"""Refuse tracked working bytes that differ from the committed Git blobs."""
import private_git
import os
from pathlib import Path
import subprocess

from campaign_model import creation_profile


def check(root, revision='HEAD'):
    """No Git clean/smudge filters, index stat cache or EOL normalization."""
    root = Path(root)
    with creation_profile('private_file'):
        tree = private_git.check_output(['git', 'ls-tree', '-rz', '--full-tree', revision], cwd=root)
        records = []
        for entry in tree.split(b'\0'):
            if not entry:
                continue
            metadata, name = entry.split(b'\t', 1)
            mode, kind, oid = metadata.split()
            if kind != b'blob':
                raise ValueError('checkout byte preflight requires tracked blobs')
            records.append((mode, oid, os.fsdecode(name)))
        # Batch reads preserve raw blob bytes, including NUL and CRLF.
        result = private_git.check_output(['git', 'cat-file', '--batch'], cwd=root,
                                         input=b''.join(oid + b'\n' for _, oid, _ in records))
    cursor = 0
    for mode, oid, name in records:
        end = result.index(b'\n', cursor)
        actual_oid, kind, size = result[cursor:end].split()
        size = int(size)
        payload = result[end + 1:end + 1 + size]
        cursor = end + size + 2
        path = root / name
        try:
            if mode == b'120000' and path.is_symlink():
                working = os.fsencode(os.readlink(path))
            elif path.is_file() and not path.is_symlink():
                working = path.read_bytes()
            else:
                raise ValueError('checkout byte preflight: missing or invalid tracked file')
        except OSError:
            raise ValueError('checkout byte preflight: unreadable tracked file') from None
        if actual_oid != oid or kind != b'blob' or working != payload:
            raise ValueError('checkout byte preflight: working bytes differ from committed blob')


if __name__ == '__main__':
    check(Path(__file__).resolve().parents[1])
