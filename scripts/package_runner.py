"""Cargo artifact executor; classification is supplied by the verification port."""
from pathlib import Path
import os
import subprocess
import strict_json


def cargo(api, *args):
    return api.subprocess.check_output(['cargo', *args], cwd=api.ROOT, text=True, encoding='utf-8')


def check_cargo(api, build=False):
    api.check_source_digests()
    metadata = strict_json.loads(api.cargo('metadata', '--locked', '--offline', '--no-deps', '--format-version', '1'))
    packages = metadata['packages']
    api.compare([p['name'] for p in packages], api.path_lookup(api.registry(), 'crates'))
    vcs = (api.ROOT / '.git').exists()
    for package in packages:
        name = package['name']
        rules = api.member_rules(api.registry(), 'crates/' + name)
        expected = list(rules)
        if not vcs:
            expected = [f for f in expected if f != '.cargo_vcs_info.json']
        listing = api.cargo('package', '--list', '--locked', '--offline', '--allow-dirty', '-p', name)
        api.compare(listing.splitlines(), expected)
        directory = Path(package['manifest_path']).parent
        for item in expected:
            if item in {'.cargo_vcs_info.json', 'Cargo.lock', 'Cargo.toml'}:
                continue
            source = directory / ('Cargo.toml' if item == 'Cargo.toml.orig' else item)
            if not source.exists():
                field = 'readme' if api.normalize_path(item) == 'readme.md' else 'license_file'
                if package.get(field):
                    source = directory / package[field]
            api.classify_member(source.read_bytes(), item, distribution='crates/' + name, rule=api.path_lookup(rules, item))
        print(f'{name}: exact inventory and content PASS ({len(expected)} files)')
    if build:
        api.cargo('package', '--workspace', '--exclude', 'plenora-storage-py', '--no-verify', '--locked', '--offline', '--allow-dirty')
        for package in packages:
            if package['name'] != 'plenora-storage-py':
                name, version = package['name'], package['version']
                api.check_crate(Path(metadata['target_directory']) / 'package' / f'{name}-{version}.crate', name, version, vcs)
                print(f'{name}: built archive PASS')


def run_consumer(consumer, toolchain, target_dir, log):
    # Cargo's CI action forces ANSI color even for redirected output. Evidence
    # is scanned UTF-8 text: prevent terminal controls rather than stripping
    # bytes after capture or relaxing the generated-member classifier.
    with log.open('wb') as stream:
        return subprocess.run(['cargo', 'run', '--offline', '--manifest-path', str(consumer / 'Cargo.toml')],
                              cwd=consumer, env=dict(os.environ, RUSTUP_TOOLCHAIN=toolchain,
                                                    CARGO_TARGET_DIR=str(Path(target_dir).resolve()),
                                                    CARGO_TERM_COLOR='never'),
                              stdout=stream, stderr=subprocess.STDOUT)

