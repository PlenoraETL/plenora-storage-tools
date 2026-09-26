"""Build a GitHub source bundle and compile an external Rust consumer from it."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import os

from versioning import workspace_version

ROOT = Path(__file__).resolve().parents[1]
CONSUMER = '''use std::sync::Arc;
use plenora_storage_core::{EngineConfig, EnvironmentCredentialResolver};
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let engine = plenora_storage_engine::build_engine(
        EngineConfig::default(), Arc::new(EnvironmentCredentialResolver))?;
    assert_eq!(engine.capabilities().operations.len(), 7);
    engine.close();
    assert!(engine.is_closed());
    Ok(())
}
'''


def extract(archive, destination, prefix):
    destination = Path(destination).resolve()
    seen = set()
    with tarfile.open(archive) as stream:
        for member in stream:
            parts = PurePosixPath(member.name).parts
            if (not parts or parts[0] != prefix or '..' in parts or
                    '\\' in member.name or ':' in member.name or
                    not (member.isfile() or member.isdir()) or member.name in seen):
                raise ValueError('unsafe or duplicated source archive member')
            seen.add(member.name)
            path = destination.joinpath(*parts).resolve()
            if destination not in path.parents:
                raise ValueError('source archive member escapes destination')
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with stream.extractfile(member) as source, path.open('wb') as output:
                    shutil.copyfileobj(source, output)
    return destination / prefix


def build(output, target_dir):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    status = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=normal'], cwd=ROOT)
    if status.strip():
        raise ValueError('source bundle requires a clean committed checkout')
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    version = workspace_version().native
    prefix = f'plenora-storage-{version}'
    archive = output / f'{prefix}-source.tar.gz'
    # Git supplies committed bytes and stable timestamps on both platforms.
    # Suppress gzip filename/time fields so the two source assets can agree.
    with tempfile.TemporaryDirectory(prefix='storage-source-build-') as temporary:
        work = Path(temporary)
        raw = work / 'source.tar'
        subprocess.run(['git', '-c', 'core.autocrlf=false', '-c', 'core.eol=lf',
                        'archive', '--format=tar', f'--prefix={prefix}/',
                        f'--output={raw}', revision], cwd=ROOT, check=True)
        with raw.open('rb') as source, archive.open('wb') as output_file:
            with gzip.GzipFile(filename='', fileobj=output_file, mode='wb', mtime=0) as compressed:
                shutil.copyfileobj(source, compressed)
        source = extract(archive, work / 'extracted', prefix)
        for name in ['Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml', 'LICENSE-MIT', 'LICENSE-APACHE',
                     'contracts/upstream/source.json', 'crates/plenora-smb2/PROVENANCE.md']:
            if not (source / name).is_file():
                raise ValueError('source bundle is incomplete')
        consumer = work / 'consumer'
        (consumer / 'src').mkdir(parents=True)
        dependencies = '\n'.join(f'{name} = {{ path = {json.dumps(str(source / "crates" / name))} }}'
                                 for name in ['plenora-storage-core', 'plenora-storage-engine'])
        (consumer / 'Cargo.toml').write_text(
            '[package]\nname="storage-source-consumer"\nversion="0.0.0"\nedition="2024"\n'
            '[dependencies]\n' + dependencies + '\n', encoding='utf-8')
        (consumer / 'src/main.rs').write_text(CONSUMER, encoding='utf-8')
        log = output / 'source-consumer.log'
        toolchain = tomllib.loads((source / 'rust-toolchain.toml').read_text())['toolchain']['channel']
        with log.open('wb') as stream:
            result = subprocess.run(['cargo', 'run', '--offline', '--manifest-path', str(consumer / 'Cargo.toml')],
                                    cwd=consumer, env=dict(os.environ, RUSTUP_TOOLCHAIN=toolchain,
                                                          CARGO_TARGET_DIR=str(Path(target_dir).resolve())),
                                    stdout=stream, stderr=subprocess.STDOUT)
        if result.returncode:
            raise RuntimeError(f'extracted source consumer failed: {log}')
    report = output / 'source-consumer.json'
    report.write_text(json.dumps({'status': 'PASS', 'source_revision': revision,
                                 'archive': archive.name,
                                 'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                                 'log_sha256': hashlib.sha256(log.read_bytes()).hexdigest(),
                                 'scope': 'external path consumer of extracted workspace; no registry patches'},
                                indent=2) + '\n', encoding='utf-8')
    return [archive, report, log]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'target/source-bundle')
    args = parser.parse_args()
    for path in build(args.output, ROOT / 'target'):
        print(path)
