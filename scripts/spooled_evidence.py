"""Validate separate private-file CLI and installed-SDK qualification evidence."""
import json

from qualify_extended_faults import EXPECTED_TESTS

EXTENDED = {'local', 'ftps', 'azure', 'gcs', 'smb', 'webdav'}
PROVIDERS = EXTENDED | {'s3', 'sftp', 'ftp'}


def validate_rows(report, field, expected):
    """Reject incomplete, duplicate or failed cases, including default-mode reports."""
    if report.get('spool_uploads') is not True:
        raise ValueError('private-file evidence must explicitly enable spooled uploads')
    rows = report['results']
    if len(rows) != len(expected) or {row[field] for row in rows} != expected:
        raise ValueError('private-file evidence has missing or duplicate cases')
    if any(row.get('status') != 'PASS' for row in rows):
        raise ValueError('private-file evidence contains unsuccessful cases')
    return rows


def validate_reports(folder, version, binary_sha256, wheel, wheel_sha256):
    """Bind all three reports to the exact platform artifacts before sealing."""
    paths = [folder / name for name in (
        'spooled-qualification.json', 'spooled-regressions.json',
        'spooled-python-qualification.json')]
    cli, faults, sdk = [json.loads(path.read_text(encoding='utf-8')) for path in paths]
    for report in (cli, faults):
        if report.get('binary_sha256') != binary_sha256:
            raise ValueError('private-file evidence refers to another CLI')
    for report, providers in ((cli, EXTENDED), (sdk, PROVIDERS)):
        rows = validate_rows(report, 'provider', providers)
        if any(row.get('operations') != 7 for row in rows):
            raise ValueError('private-file provider operations are incomplete')
    validate_rows(faults, 'name', EXPECTED_TESTS)
    if any(row.get('async_stat') != 'PASS' for row in sdk['results']):
        raise ValueError('private-file SDK async qualification is incomplete')
    if (sdk.get('version'), sdk.get('wheel'), sdk.get('wheel_sha256')) != (version, wheel, wheel_sha256):
        raise ValueError('private-file evidence refers to another SDK artifact')
    return paths
