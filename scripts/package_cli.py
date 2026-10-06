"""Package the CLI with its licenses and verify the bytes users will extract."""
import hashlib
from pathlib import Path
import stat
import subprocess
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = [
    'README.md', 'LICENSE',
    'crates/plenora-storage-py/README.md',
    'crates/plenora-storage-py/examples/local_roundtrip.py',
    'crates/plenora-smb2/PROVENANCE.md', 'crates/plenora-smb2/LICENSE-MIT',
    'crates/plenora-smb2/LICENSE-APACHE',
]
# Ship the complete committed documentation tree, including the release reports
# linked by its index. Untracked working notes must never enter a distribution.
DOCUMENTS += sorted(subprocess.check_output(['git', 'ls-files', '--', 'docs'], cwd=ROOT, text=True).splitlines())


def verify_archive(archive, binary):
    archive, binary = Path(archive), Path(binary)
    expected = hashlib.sha256(binary.read_bytes()).hexdigest()
    if archive.suffix == '.zip':
        with zipfile.ZipFile(archive) as stream:
            if any(stat.S_IFMT(member.external_attr >> 16) not in (0, stat.S_IFREG)
                   for member in stream.infolist()):
                raise ValueError('CLI archive contains a non-file member')
            names = stream.namelist()
            actual = hashlib.sha256(stream.read(binary.name)).hexdigest()
    else:
        with tarfile.open(archive) as stream:
            members = stream.getmembers()
            if any(not member.isfile() for member in members):
                raise ValueError('CLI archive contains a non-file member')
            names = [member.name for member in members]
            actual = hashlib.sha256(stream.extractfile(binary.name).read()).hexdigest()
    if len(names) != len(set(names)) or set(names) != {binary.name, *DOCUMENTS}:
        raise ValueError('CLI archive has unexpected or missing members')
    if actual != expected:
        raise ValueError('archived CLI differs from qualified binary')


def build(output, binary, version, target):
    suffix = '.zip' if 'windows' in target else '.tar.gz'
    archive = Path(output) / f'plenora-storage-{version}-{target}{suffix}'
    files = [(Path(binary), Path(binary).name)] + [(ROOT / name, name) for name in DOCUMENTS]
    if suffix == '.zip':
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as stream:
            for source, name in files:
                stream.write(source, name)
    else:
        with tarfile.open(archive, 'w:gz') as stream:
            for source, name in files:
                stream.add(source, arcname=name)
    verify_archive(archive, binary)
    return archive
