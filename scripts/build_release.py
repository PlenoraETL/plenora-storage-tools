"""Build and verify immutable local release artifacts; never publishes them."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'contracts/upstream'))
from conformance_checks import adoption_errors

ROOT = Path(__file__).resolve().parents[1]


def run(*command, **kwargs):
    return subprocess.run(command, cwd=ROOT, check=True, **kwargs)


def digest(path):
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(chunk)
    return checksum.hexdigest()


def source_digest():
    checksum = hashlib.sha256()
    files = [ROOT / name for name in ['Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml']]
    for directory in ['crates', 'contracts']:
        files.extend(path for path in (ROOT / directory).rglob('*')
                     if path.is_file() and '__pycache__' not in path.parts)
    for path in sorted(files, key=lambda value: value.relative_to(ROOT).as_posix()):
        checksum.update(path.relative_to(ROOT).as_posix().encode() + b'\0')
        checksum.update(path.read_bytes())
        checksum.update(b'\0')
    return checksum.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-dirty', action='store_true', help='Build a local candidate from an uncommitted checkout')
    args = parser.parse_args()
    metadata = json.loads(run('cargo', 'metadata', '--locked', '--offline', '--format-version', '1', '--no-deps', capture_output=True, text=True).stdout)
    version = metadata['packages'][0]['version']
    target = next(line.split(': ', 1)[1] for line in run('rustc', '-vV', capture_output=True, text=True).stdout.splitlines() if line.startswith('host: '))
    target_dir = Path(metadata['target_directory'])
    output = ROOT / 'dist' / version / target
    output.mkdir(parents=True, exist_ok=True)
    revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True)
    if not args.allow_dirty:
        assert revision.returncode == 0, 'a committed checkout is required'
        status = run('git', 'status', '--porcelain', '--untracked-files=normal', capture_output=True, text=True)
        assert not status.stdout.strip(), 'commit the source before preparing a release'
    run('cargo', 'test', '--workspace', '--all-targets', '--locked', '--offline')
    run('cargo', 'build', '--release', '--locked', '--offline', '-p', 'plenora-storage-cli')
    # Cargo 1.92's temporary workspace registry can fail on Windows with
    # "no hash listed" for unpublished sibling crates. Verify the extracted
    # archives below, including the CLI, without depending on that registry.
    command = ['cargo', 'package', '--workspace', '--locked', '--offline', '--no-verify']
    if args.allow_dirty:
        command.append('--allow-dirty')
    run(*command)

    binary_name = 'plenora-storage.exe' if os.name == 'nt' else 'plenora-storage'
    binary = output / binary_name
    shutil.copy2(target_dir / 'release' / binary_name, binary)
    for command in ('--version', 'capabilities'):
        response = run(str(binary), '--format', 'json', command, capture_output=True, text=True)
        assert not response.stderr and len(response.stdout.splitlines()) == 1
        value = json.loads(response.stdout)
        assert value['status'] == 'ok' and value['component_version'] == version
        (output / (command.lstrip('-') + '.json')).write_text(response.stdout, encoding='utf-8')

    packages = []
    for package in metadata['packages']:
        archive = target_dir / 'package' / f"{package['name']}-{version}.crate"
        shutil.copy2(archive, output / archive.name)
        packages.append(archive)

    # A consumer sees only extracted archives, never the workspace sources.
    with tempfile.TemporaryDirectory(prefix='storage-consumer-') as temporary:
        consumer = Path(temporary)
        for archive in packages:
            with tarfile.open(archive) as stream:
                for member in stream:
                    path = (consumer / member.name).resolve()
                    if consumer not in path.parents or not (member.isfile() or member.isdir()):
                        raise ValueError('unsafe crate archive member')
                    if member.isdir():
                        path.mkdir(parents=True, exist_ok=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with stream.extractfile(member) as source, path.open('wb') as destination:
                            shutil.copyfileobj(source, destination)
        dependency_lines = [f'{p["name"]} = {{ path = {json.dumps(str(consumer / (p["name"] + "-" + version)))}, version = "={version}" }}' for p in metadata['packages'] if p['name'] != 'plenora-storage-cli']
        manifest = '[package]\nname="storage-release-consumer"\nversion="0.0.0"\nedition="2024"\n[dependencies]\n' + '\n'.join(dependency_lines)
        manifest += '\n[patch.crates-io]\n' + '\n'.join(dependency_lines) + '\n'
        (consumer / 'Cargo.toml').write_text(manifest, encoding='utf-8')
        (consumer / 'src').mkdir()
        (consumer / 'src/main.rs').write_text('''use std::sync::Arc;
use plenora_storage_core::{Engine, EngineConfig, EnvironmentCredentialResolver};
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut engine = Engine::new(EngineConfig::default());
    let credentials = Arc::new(EnvironmentCredentialResolver);
    engine.register_provider(Arc::new(plenora_storage_s3::S3Provider::new(credentials.clone())))?;
    engine.register_provider(Arc::new(plenora_storage_sftp::SftpProvider::new(credentials.clone())))?;
    engine.register_provider(Arc::new(plenora_storage_ftp::FtpProvider::new(credentials.clone())))?;
    engine.register_provider(Arc::new(plenora_storage_ftp::FtpProvider::new_ftps(credentials.clone())))?;
    engine.register_provider(Arc::new(plenora_storage_providers::LocalProvider::new(credentials.clone())))?;
    engine.register_provider(Arc::new(plenora_storage_providers::AzureProvider::new(credentials.clone())))?;
    engine.register_provider(Arc::new(plenora_storage_providers::GcsProvider::new(credentials.clone())))?;
    engine.register_provider(Arc::new(plenora_storage_providers::SmbProvider::new(credentials.clone())))?;
    engine.register_provider(Arc::new(plenora_storage_providers::WebDavProvider::new(credentials.clone())))?;
    assert_eq!(engine.capabilities().operations.len(), 7);
    engine.close();
    assert!(engine.is_closed());
    Ok(())
}
''', encoding='utf-8')
        env = dict(os.environ, CARGO_TARGET_DIR=str(target_dir / 'release-consumer'))
        run('cargo', 'run', '--offline', '--manifest-path', str(consumer / 'Cargo.toml'), env=env)
        cli_manifest = consumer / f'plenora-storage-cli-{version}' / 'Cargo.toml'
        with cli_manifest.open('a', encoding='utf-8') as stream:
            stream.write('\n[patch.crates-io]\n' + '\n'.join(dependency_lines) + '\n')
        run('cargo', 'check', '--offline', '--manifest-path', str(cli_manifest), env=env)
        run('cargo', 'test', '--offline', '--manifest-path',
            str(consumer / f'plenora-storage-core-{version}' / 'Cargo.toml'), '--test', 'runtime_binding', env=env)

    archive_path = output / f'plenora-storage-{version}-{target}.tar.gz'
    with tarfile.open(archive_path, 'w:gz') as archive:
        archive.add(binary, arcname=binary_name)
        for name in ['README.md', 'LICENSE-MIT', 'LICENSE-APACHE', 'docs/release.md',
                     'docs/contract-adoption.md', 'docs/release-readiness.md', 'docs/provider-expansion.md',
                     'crates/plenora-smb2/PROVENANCE.md', 'crates/plenora-smb2/LICENSE-MIT', 'crates/plenora-smb2/LICENSE-APACHE']:
            archive.add(ROOT / name, arcname=name)
    contracts = output / f'plenora-storage-contracts-{version}.tar.gz'
    with tarfile.open(contracts, 'w:gz') as archive:
        archive.add(ROOT / 'contracts', arcname='contracts',
                    filter=lambda member: None if '__pycache__' in Path(member.name).parts else member)

    files = [binary, archive_path, contracts] + [output / p.name for p in packages]
    source = json.loads((ROOT / 'contracts/upstream/source.json').read_text())
    adoption = {
        'schema_version': 4, 'component': 'plenora-storage-tools',
        'contracts_source': source, 'profile': 'plenora-storage-tools-profile-v1',
        'artifacts': [
            {'name': binary_name, 'surface': 'cli', 'version': version,
             'digest': 'sha256:' + digest(binary),
             'verification': ['CLI protocol schema tests and released binary discovery', 'docs/contract-adoption.md']},
            *[{'name': p.name, 'surface': 'cli' if 'storage-cli-' in p.name else 'rust',
               'version': version, 'digest': 'sha256:' + digest(p),
               'verification': ['Compiled from extracted Cargo archive by scripts/build_release.py']}
              for p in packages],
            {'name': 'plenora-storage-runtime-binding', 'surface': 'runtime', 'version': version,
             'digest': 'sha256:' + digest(output / f'plenora-storage-core-{version}.crate'),
             'verification': ['Runtime binding packaged in plenora-storage-core',
                              'cargo test --test runtime_binding from the extracted core archive']},
        ],
        'contracts': [
            {'id': contract, 'status': 'conforming', 'verification': checks}
            for contract, checks in [
                ('plenora-public-surfaces-v1', ['core contract fixtures and Rust/runtime operation binding equivalence tests']),
                ('plenora-capabilities-v2', ['CLI capabilities schema validation and per-surface discovery tests']),
                ('plenora-error-v1', ['CLI error schema validation and runtime error-axis tests']),
                ('plenora-public-security-v1', ['provider preflight, credential reference, SSRF and artifact boundary tests']),
                ('plenora-cli-v2', ['CLI protocol tests, version and capabilities on the packaged binary']),
                ('plenora-runtime-binding-v1', ['runtime_binding integration suite executed from extracted core archive']),
            ]
        ],
        'deviations': [],
    }
    assert not adoption_errors(adoption), adoption_errors(adoption)
    adoption_path = output / 'adoption-manifest-v4.json'
    adoption_path.write_text(json.dumps(adoption, indent=2) + '\n', encoding='utf-8')
    run('cargo', 'run', '--locked', '--offline', '-p', 'plenora-storage-core', '--example',
        'validate_adoption', '--', str(ROOT / 'contracts/upstream/adoption-manifest-v4.schema.json'), str(adoption_path))
    files.append(adoption_path)
    manifest = {
        'schema_version': 1, 'component': 'plenora-storage-tools', 'version': version,
        'target': target, 'status': 'built',
        'source_sha256': source_digest(),
        'source_revision': revision.stdout.strip() if revision.returncode == 0 else None,
        'source_committed': not args.allow_dirty,
        'contracts_source': source,
        'verification': ['cargo package --workspace --locked --offline --no-verify', 'external consumer from extracted crate archives', 'CLI compilation from extracted archive', 'released CLI version and capabilities'],
        'artifacts': [{'name': path.name, 'sha256': digest(path), 'size': path.stat().st_size} for path in files],
    }
    (output / 'release-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    (output / 'SHA256SUMS').write_text(''.join(f'{digest(p)}  {p.name}\n' for p in files + [output / 'release-manifest.json']), encoding='utf-8')
    print(f'Release candidate: {output}')


if __name__ == '__main__':
    main()
