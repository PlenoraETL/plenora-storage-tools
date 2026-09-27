"""Qualify a downloaded ABI3 wheel on the selected Python interpreter (>=3.10)."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def test_count(log, returncode):
    counts = re.findall(r'^Ran (\d+) tests? in ', log, re.MULTILINE)
    if (returncode or len(counts) != 1 or int(counts[0]) == 0 or 'skipped=' in log
            or not re.search(r'^OK\s*$', log, re.MULTILINE)
            or re.search(r'^(FAILED|ERROR:|FAIL:)', log, re.MULTILINE)):
        raise ValueError('installed SDK tests failed, were skipped or did not run')
    return int(counts[0])


def measured_coverage(document, wheel):
    if not document['meta']['branch_coverage'] or document['meta']['version'] != '7.16.1':
        raise ValueError('expected the pinned branch coverage tool')
    with zipfile.ZipFile(wheel) as archive:
        expected = {name: archive.read(name) for name in archive.namelist()
                    if name.startswith('plenora_storage/') and name.endswith('.py')}
    actual = {}
    for name, entry in document['files'].items():
        source = Path(name)
        public = 'plenora_storage/' + source.as_posix().rsplit('/plenora_storage/', 1)[-1]
        if public in actual or public not in expected or source.read_bytes() != expected[public]:
            raise ValueError('coverage must measure exactly the Python files installed from this wheel')
        summary = entry['summary']
        lines, covered = summary['num_statements'], summary['covered_lines']
        branches, reached = summary['num_branches'], summary['covered_branches']
        if lines <= 0 or not 0 <= covered <= lines or not 0 <= reached <= branches:
            raise ValueError('invalid Python coverage counts')
        actual[public] = {'lines': lines, 'covered_lines': covered, 'branches': branches,
                          'covered_branches': reached, 'line_percent': covered * 100 / lines,
                          'branch_percent': reached * 100 / branches if branches else 100}
    if actual.keys() != expected.keys() or not actual:
        raise ValueError('Python coverage omitted installed source files')
    return actual


def enforce_coverage(files, policy):
    for counts in files.values():
        if counts['line_percent'] < policy['line_percent'] or counts['branch_percent'] < policy['branch_percent']:
            raise ValueError('Python coverage is below the per-module release floor')


def qualify(wheel, output, measure, typing=False):
    subprocess.run([sys.executable, '-I', '-m', 'pip', 'install', '--no-index', '--force-reinstall', str(wheel)], check=True)
    with tempfile.TemporaryDirectory(prefix='storage-sdk-compatibility-') as temporary:
        prefix = [sys.executable, '-I', '-m']
        if measure:
            prefix += ['coverage', 'run', '--branch', '--source=plenora_storage',
                       '--data-file', str(output / '.coverage'), '-m']
        result = subprocess.run([*prefix, 'unittest', 'discover', '-s',
                                 str(ROOT / 'crates/plenora-storage-py/python/tests'), '-v'],
                                cwd=temporary, capture_output=True, text=True)
        log = output / 'sdk-tests.log'
        log.write_text(result.stdout + result.stderr, encoding='utf-8')
        count = test_count(log.read_text(encoding='utf-8'), result.returncode)
        subprocess.run([sys.executable, '-I', str(ROOT / 'crates/plenora-storage-py/examples/local_roundtrip.py')],
                       cwd=temporary, check=True)
        if measure:
            subprocess.run([sys.executable, '-I', '-m', 'coverage', 'json',
                            '--data-file', str(output / '.coverage'),
                            '-o', str(output / 'coverage.json')], cwd=temporary, check=True)
    report = {'status': 'PASS', 'python': platform.python_version(), 'tests_passed': count,
              'platform': sys.platform, 'wheel': wheel.name, 'wheel_sha256': hashlib.sha256(wheel.read_bytes()).hexdigest(),
              'tests_log_sha256': hashlib.sha256(log.read_bytes()).hexdigest()}
    if measure:
        path = output / 'coverage.json'
        document = json.loads(path.read_text())
        files = measured_coverage(document, wheel)
        policy = json.loads((ROOT / 'scripts/coverage-policy.json').read_text())['python']
        report['coverage'] = {'tool_version': document['meta']['version'], 'branch': True,
                              'files': files, 'policy': policy, 'report_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        enforce_coverage(files, policy)
    if typing:
        from check_sdk_typing import check
        report['typing'] = check(wheel, output)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('artifacts', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--coverage', action='store_true', help='Measure installed Python lines and branches')
    parser.add_argument('--typing', action='store_true', help='Check the installed public typing contract')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {'status': 'RUNNING'}
    report_path = output / 'sdk-tests.json'
    report_path.write_text(json.dumps(report) + '\n', encoding='utf-8')
    try:
        wheels = [args.artifacts] if args.artifacts.is_file() else list(args.artifacts.rglob('plenora_storage-*.whl'))
        if len(wheels) != 1:
            raise ValueError('expected one downloaded wheel for this platform')
        report = qualify(wheels[0].resolve(), output, args.coverage, args.typing)
    except BaseException as error:
        report.update(status='FAIL', failure_type=type(error).__name__)
        raise
    finally:
        report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
