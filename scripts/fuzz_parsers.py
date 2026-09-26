"""Run the real XML/FTP parsers with libFuzzer and preserve reproducible evidence."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
TOOLCHAIN = 'nightly-2026-09-20'
TARGETS = ('s3_xml', 'azure_xml', 'webdav_xml', 'ftp_listing')
TRIPLE = 'x86_64-unknown-linux-gnu'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_lock_alignment(product, fuzz):
    def identity(package):
        return tuple(package.get(key) for key in ('name', 'version', 'source', 'checksum'))
    available = {identity(package) for package in product['package']}
    tools = {'plenora-storage-fuzz', 'libfuzzer-sys', 'arbitrary'}
    for package in fuzz['package']:
        if package['name'] not in tools and identity(package) not in available:
            raise ValueError('fuzz dependencies differ from the product lockfile')


def boundary_seeds(target, corpus):
    if target == 'ftp_listing':
        for size in (32768, 32769):
            (corpus / f'boundary-{size}').write_bytes(b'x' * size)
        (corpus / 'invalid-utf8').write_bytes(b'type=file;size=0; \xff\n')
    else:
        root = {'s3_xml': b'ListBucketResult', 'azure_xml': b'EnumerationResults',
                'webdav_xml': b'multistatus'}[target]
        (corpus / 'deep-unknown-elements').write_bytes(
            b'<' + root + b'>' + b'<unknown>' * 512 + b'</unknown>' * 512 + b'</' + root + b'>')


def stats(log, returncode):
    units = re.findall(r'stat::number_of_executed_units:\s*(\d+)', log)
    coverage = re.findall(r'cov:\s*(\d+)', log)
    # A successful exit without a completed, instrumented campaign is not proof.
    passed = returncode == 0 and bool(units) and bool(coverage)
    passed = passed and int(units[-1]) > 0 and int(coverage[-1]) > 0
    return {'status': 'PASS' if passed else 'FAIL', 'returncode': returncode,
            'executions': int(units[-1]) if units else None,
            'coverage_edges': int(coverage[-1]) if coverage else None}


def capture(command):
    return subprocess.check_output(command, cwd=ROOT, text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=60, help='Budget per parser, excluding compilation')
    parser.add_argument('--seed', type=int, default=7319)
    parser.add_argument('--output', type=Path, default=ROOT / 'target/parser-fuzz')
    args = parser.parse_args()
    if not 10 <= args.seconds <= 3600 or not 1 <= args.seed <= 2**31 - 1:
        parser.error('seconds must be 10..3600 and seed 1..2147483647')
    output = args.output.resolve()
    # Never mix a previous PASS or corpus with a new campaign.
    output.mkdir(parents=True, exist_ok=False)
    report = {'schema_version': 1, 'status': 'RUNNING', 'targets': {},
              'toolchain': TOOLCHAIN, 'seconds_per_target': args.seconds, 'seed': args.seed,
              'sanitizer': 'address', 'max_input_bytes': 65536,
              'timeout_seconds_per_input': 10, 'rss_limit_mb': 1024}

    def save():
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')

    save()
    try:
        report['source_commit'] = capture(['git', 'rev-parse', 'HEAD'])
        report['dirty'] = bool(capture(['git', 'status', '--porcelain', '--untracked-files=normal']))
        report['rustc'] = capture(['rustc', f'+{TOOLCHAIN}', '-Vv'])
        report['cargo_fuzz'] = capture(['cargo', f'+{TOOLCHAIN}', 'fuzz', '--version'])
        if report['cargo_fuzz'] != 'cargo-fuzz 0.13.2':
            raise RuntimeError('cargo-fuzz version does not match the pinned tool')
        lock = ROOT / 'fuzz/Cargo.lock'
        check_lock_alignment(tomllib.loads((ROOT / 'Cargo.lock').read_text()),
                             tomllib.loads(lock.read_text()))
        report['lock_sha256'] = digest(lock)
        subprocess.run(['cargo', f'+{TOOLCHAIN}', 'metadata', '--manifest-path', 'fuzz/Cargo.toml',
                        '--locked', '--offline', '--format-version', '1'], cwd=ROOT,
                       stdout=subprocess.DEVNULL, check=True)
        env = dict(os.environ, CARGO_NET_OFFLINE='true')
        command = ['cargo', f'+{TOOLCHAIN}', 'fuzz', 'build', '--sanitizer', 'address',
                   '--target', TRIPLE, '--target-dir', str(ROOT / 'fuzz/target'), '--codegen-units', '16']
        report['build_command'] = command
        with (output / 'build.log').open('w') as log:
            subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        if digest(lock) != report['lock_sha256']:
            raise RuntimeError('fuzz build changed the locked dependencies')
        for target in TARGETS:
            directory = output / target
            directory.mkdir()
            corpus = directory / 'corpus'
            shutil.copytree(ROOT / 'fuzz/seeds' / target, corpus)
            boundary_seeds(target, corpus)
            seeds = {p.name: digest(p) for p in sorted(corpus.iterdir())}
            artifacts = directory / 'artifacts'
            artifacts.mkdir()
            binary = ROOT / 'fuzz/target' / TRIPLE / 'release' / target
            command = [str(binary), str(corpus), f'-seed={args.seed}',
                       f'-max_total_time={args.seconds}', '-max_len=65536',
                       '-timeout=10', '-rss_limit_mb=1024', '-print_final_stats=1',
                       f'-artifact_prefix={artifacts}{os.sep}']
            started = time.monotonic()
            with (directory / 'run.log').open('w') as log:
                try:
                    result = subprocess.run(command, cwd=ROOT, env=env, stdout=log,
                                            stderr=subprocess.STDOUT, timeout=args.seconds + 60)
                    code = result.returncode
                except subprocess.TimeoutExpired:
                    code = -1
            entry = stats((directory / 'run.log').read_text(errors='replace'), code)
            entry.update(binary_sha256=digest(binary), seeds=seeds, command=command,
                         elapsed_seconds=round(time.monotonic() - started, 3))
            report['targets'][target] = entry
            save()
            print(f'{entry["status"]} {target}: {entry["executions"]} executions', flush=True)
        report['status'] = 'PASS' if all(v['status'] == 'PASS' for v in report['targets'].values()) else 'FAIL'
    except BaseException as error:
        report['status'] = 'FAIL'
        report['failure_type'] = type(error).__name__
        raise
    finally:
        save()
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
