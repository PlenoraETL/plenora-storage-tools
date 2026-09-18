"""Seal a local release only after both platform and dependency gates passed."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

from build_release import ROOT, digest, source_digest
from qualify_extended_faults import EXPECTED_TESTS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--evidence', required=True, type=Path)
    args = parser.parse_args()
    output = args.directory.resolve()
    subprocess.run([sys.executable, str(ROOT / 'scripts/verify_release.py'), str(output)], check=True)
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    status = subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True)
    assert not status.strip(), 'qualification requires the final clean commit'
    audit_path = args.evidence / 'audit.json'
    audit = json.loads(audit_path.read_text(encoding='utf-8'))
    assert audit['vulnerabilities']['count'] == 0 and not audit.get('warnings'), 'dependency audit failed'
    deny_path = args.evidence / 'deny.log'
    assert 'advisories ok, bans ok, licenses ok, sources ok' in deny_path.read_text(encoding='utf-8'), 'dependency policy failed'
    archive_evidence = output / 'evidence'
    archive_evidence.mkdir(exist_ok=True)
    targets = ['x86_64-unknown-linux-gnu', 'x86_64-pc-windows-msvc']
    records = []
    for target in targets:
        folder = output / target
        manifest = json.loads((folder / 'release-manifest.json').read_text())
        assert manifest['source_committed'] and manifest['source_revision'] == revision
        assert manifest['source_sha256'] == source_digest(), 'source snapshot changed'
        qualification_path = folder / 'qualification.json'
        qualification = json.loads(qualification_path.read_text())
        assert {r['provider'] for r in qualification['results']} == {'s3', 'sftp', 'ftp'}
        assert all(r['status'] == 'PASS' and r['operations'] == 7 for r in qualification['results'])
        binary = folder / ('plenora-storage.exe' if 'windows' in target else 'plenora-storage')
        assert qualification['binary_sha256'] == digest(binary)
        regression_path = folder / 'cli-regressions.json'
        regressions = json.loads(regression_path.read_text())
        assert regressions['binary_sha256'] == digest(binary)
        assert set(regressions['results']) == {'cli_pagination', 'cli_download_publication', 'connection_file_bound', 'ftp_parent_deadline'}
        assert all(r['status'] == 'PASS' for r in regressions['results'].values())
        suite_path = args.evidence / f'{target}-tests.log'
        suite = suite_path.read_text(encoding='utf-8', errors='replace')
        counts = re.findall(r'test result: ok\. (\d+) passed; (\d+) failed; (\d+) ignored', suite)
        assert counts and not any(int(failed) for _, failed, _ in counts)
        assert 'test result: FAILED' not in suite
        assert 'runtime_uuids_are_canonical_and_causation_is_preserved ... ok' in suite
        assert 'interrupted_atomic_commit_preserves_final_object_and_reports_provable_effects ... ok' in suite
        if 'linux' in target:
            assert not any(int(ignored) for _, _, ignored in counts), 'Linux provider fixtures were skipped'
            faults_path = folder / 'commit-faults.json'
            faults = json.loads(faults_path.read_text())
            assert faults['binary_sha256'] == digest(binary)
            assert {(r['mode'], r['interruption']) for r in faults['results']} == {
                (mode, control) for mode in ['before_commit', 'after_commit'] for control in ['deadline', 'sigterm']}
            assert all(r['status'] == 'PASS' and r['reported_effect'] == 'unknown' for r in faults['results'])
        evidence = [qualification_path, regression_path, suite_path, audit_path, deny_path,
                    folder / 'adoption-manifest-v4.json', folder / 'release-manifest.json']
        if tuple(map(int, manifest['version'].split('.'))) >= (0, 2, 0):
            upstream_audit_path = args.evidence / 'smb-upstream-audit.json'
            upstream_audit = json.loads(upstream_audit_path.read_text())
            assert upstream_audit['vulnerabilities']['count'] == 0 and not upstream_audit.get('warnings')
            shutil.copyfile(upstream_audit_path, archive_evidence / upstream_audit_path.name)
            evidence.append(upstream_audit_path)
            extended_path = folder / 'extended-qualification.json'
            extended = json.loads(extended_path.read_text())
            assert extended['binary_sha256'] == digest(binary)
            assert {r['provider'] for r in extended['results']} == {'local', 'ftps', 'azure', 'gcs', 'smb', 'webdav'}
            assert all(r['status'] == 'PASS' and r['operations'] == 7 for r in extended['results'])
            faults_extended_path = folder / 'extended-regressions.json'
            extended_faults = json.loads(faults_extended_path.read_text())
            assert extended_faults['binary_sha256'] == digest(binary)
            assert {r['name'] for r in extended_faults['results']} == EXPECTED_TESTS
            assert all(r['status'] == 'PASS' for r in extended_faults['results'])
            qualification['results'].extend(extended['results'])
            evidence.extend([extended_path, faults_extended_path])
        if 'linux' in target:
            evidence.append(faults_path)
        for source in [suite_path, audit_path, deny_path]:
            shutil.copyfile(source, archive_evidence / source.name)
        records.append({'target': target, 'binary_sha256': digest(binary),
                        'tests_passed': sum(int(passed) for passed, _, _ in counts),
                        'tests_ignored': sum(int(ignored) for _, _, ignored in counts),
                        'providers': qualification['results'],
                        'evidence': [{'name': p.name, 'sha256': digest(p)} for p in evidence]})
    receipt = {'schema_version': 1, 'component': 'plenora-storage-tools', 'version': output.name,
               'status': 'qualified_for_publication', 'source_revision': revision,
               'source_sha256': source_digest(), 'advisory_database': audit['database'],
               'platforms': records}
    path = output / 'release-qualification.json'
    path.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    (output / 'SHA256SUMS').write_text(f'{digest(path)}  {path.name}\n' + ''.join(
        f'{digest(output / target / "SHA256SUMS")}  {target}/SHA256SUMS\n' for target in targets), encoding='utf-8')
    print(f'Qualified release: {path}')


if __name__ == '__main__':
    main()
