"""Package the CLI with its licenses and verify the bytes users will extract."""
import hashlib
from pathlib import Path
import stat
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = [
    'README.md', 'LICENSE-MIT', 'LICENSE-APACHE', 'docs/release.md',
    'docs/contract-adoption.md', 'docs/release-readiness.md', 'docs/provider-expansion.md',
    'docs/STATO.md', 'docs/database-alignment.md', 'docs/architecture.md',
    'docs/README.md', 'docs/database-reference-review.md', 'docs/release-evidence-bundle.md',
    'docs/quality-alignment-progress.md',
    'docs/migration-1.0.md', 'docs/reliability.md', 'docs/compatibility-1.0.md',
    'crates/plenora-storage-py/README.md',
    'crates/plenora-storage-py/examples/local_roundtrip.py',
    'crates/plenora-smb2/PROVENANCE.md', 'crates/plenora-smb2/LICENSE-MIT',
    'crates/plenora-smb2/LICENSE-APACHE',
]


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
