"""Closed generated-text exceptions; additions require code review.

Cargo and core wheel metadata have strict Git-derived validators below.
Campaign/publication evidence and the wheel SBOM retain STOR-LIMIT-003.
Every entry is still inventoried, must be UTF-8 text and is scanned for private
material/credentials. Containers and natives have separate closed authorities.
No suffix, directory or policy rule can grant a generated-text exemption.
"""

GENERATED_MEMBERS = {
    'crates': (
        '.cargo_vcs_info.json',
        'Cargo.lock',
        'Cargo.toml',
    ),
    'wheel': (
        'plenora_storage-{version}.dist-info/METADATA',
        'plenora_storage-{version}.dist-info/RECORD',
        'plenora_storage-{version}.dist-info/WHEEL',
        'plenora_storage-{version}.dist-info/sboms/plenora-storage-py.cyclonedx.json',
    ),
    'qualification': (
        'SHA256SUMS',
        'evidence/audit.json',
        'evidence/deny.log',
        'evidence/gates/api/x86_64-pc-windows-msvc/plenora-smb2.txt',
        'evidence/gates/api/x86_64-pc-windows-msvc/plenora-storage-core.txt',
        'evidence/gates/api/x86_64-pc-windows-msvc/plenora-storage-engine.txt',
        'evidence/gates/api/x86_64-pc-windows-msvc/plenora-storage-ftp.txt',
        'evidence/gates/api/x86_64-pc-windows-msvc/plenora-storage-providers.txt',
        'evidence/gates/api/x86_64-pc-windows-msvc/plenora-storage-s3.txt',
        'evidence/gates/api/x86_64-pc-windows-msvc/plenora-storage-sftp.txt',
        'evidence/gates/api/x86_64-pc-windows-msvc/report.json',
        'evidence/gates/api/x86_64-unknown-linux-gnu/plenora-smb2.txt',
        'evidence/gates/api/x86_64-unknown-linux-gnu/plenora-storage-core.txt',
        'evidence/gates/api/x86_64-unknown-linux-gnu/plenora-storage-engine.txt',
        'evidence/gates/api/x86_64-unknown-linux-gnu/plenora-storage-ftp.txt',
        'evidence/gates/api/x86_64-unknown-linux-gnu/plenora-storage-providers.txt',
        'evidence/gates/api/x86_64-unknown-linux-gnu/plenora-storage-s3.txt',
        'evidence/gates/api/x86_64-unknown-linux-gnu/plenora-storage-sftp.txt',
        'evidence/gates/api/x86_64-unknown-linux-gnu/report.json',
        'evidence/gates/coverage/coverage-summary.json',
        'evidence/gates/coverage/rust-coverage.json',
        'evidence/gates/disk-pressure/report.json',
        'evidence/gates/fuzz/azure_xml/run.log',
        'evidence/gates/fuzz/ftp_listing/run.log',
        'evidence/gates/fuzz/kerberos_messages/run.log',
        'evidence/gates/fuzz/ntlm_challenge/run.log',
        'evidence/gates/fuzz/report.json',
        'evidence/gates/fuzz/s3_xml/run.log',
        'evidence/gates/fuzz/smb2_messages/run.log',
        'evidence/gates/fuzz/spnego_der/run.log',
        'evidence/gates/fuzz/webdav_xml/run.log',
        'evidence/gates/native-components/x86_64-pc-windows-msvc/cli.cdx.json',
        'evidence/gates/native-components/x86_64-pc-windows-msvc/cli.syft.json',
        'evidence/gates/native-components/x86_64-pc-windows-msvc/report.json',
        'evidence/gates/native-components/x86_64-pc-windows-msvc/wheel.cdx.json',
        'evidence/gates/native-components/x86_64-pc-windows-msvc/wheel.syft.json',
        'evidence/gates/native-components/x86_64-unknown-linux-gnu/cli.cdx.json',
        'evidence/gates/native-components/x86_64-unknown-linux-gnu/cli.syft.json',
        'evidence/gates/native-components/x86_64-unknown-linux-gnu/report.json',
        'evidence/gates/native-components/x86_64-unknown-linux-gnu/wheel.cdx.json',
        'evidence/gates/native-components/x86_64-unknown-linux-gnu/wheel.syft.json',
        'evidence/gates/performance/baseline.json',
        'evidence/gates/performance/candidate.json',
        'evidence/gates/performance/report.json',
        'evidence/gates/sdk/x86_64-pc-windows-msvc/3.10/coverage.json',
        'evidence/gates/sdk/x86_64-pc-windows-msvc/3.10/sdk-tests.json',
        'evidence/gates/sdk/x86_64-pc-windows-msvc/3.10/sdk-tests.log',
        'evidence/gates/sdk/x86_64-pc-windows-msvc/3.10/sdk-typing.json',
        'evidence/gates/sdk/x86_64-pc-windows-msvc/3.10/sdk-typing.log',
        'evidence/gates/sdk/x86_64-pc-windows-msvc/3.11/coverage.json',
        'evidence/gates/sdk/x86_64-pc-windows-msvc/3.11/sdk-tests.json',
        'evidence/gates/sdk/x86_64-pc-windows-msvc/3.11/sdk-tests.log',
        'evidence/gates/sdk/x86_64-pc-windows-msvc/3.11/sdk-typing.json',
        'evidence/gates/sdk/x86_64-pc-windows-msvc/3.11/sdk-typing.log',
        'evidence/gates/sdk/x86_64-pc-windows-msvc/3.12/coverage.json',
        'evidence/gates/sdk/x86_64-pc-windows-msvc/3.12/sdk-tests.json',
        'evidence/gates/sdk/x86_64-pc-windows-msvc/3.12/sdk-tests.log',
        'evidence/gates/sdk/x86_64-pc-windows-msvc/3.12/sdk-typing.json',
        'evidence/gates/sdk/x86_64-pc-windows-msvc/3.12/sdk-typing.log',
        'evidence/gates/sdk/x86_64-pc-windows-msvc/3.13/coverage.json',
        'evidence/gates/sdk/x86_64-pc-windows-msvc/3.13/sdk-tests.json',
        'evidence/gates/sdk/x86_64-pc-windows-msvc/3.13/sdk-tests.log',
        'evidence/gates/sdk/x86_64-pc-windows-msvc/3.13/sdk-typing.json',
        'evidence/gates/sdk/x86_64-pc-windows-msvc/3.13/sdk-typing.log',
        'evidence/gates/sdk/x86_64-pc-windows-msvc/3.14/coverage.json',
        'evidence/gates/sdk/x86_64-pc-windows-msvc/3.14/sdk-tests.json',
        'evidence/gates/sdk/x86_64-pc-windows-msvc/3.14/sdk-tests.log',
        'evidence/gates/sdk/x86_64-pc-windows-msvc/3.14/sdk-typing.json',
        'evidence/gates/sdk/x86_64-pc-windows-msvc/3.14/sdk-typing.log',
        'evidence/gates/sdk/x86_64-unknown-linux-gnu/3.10/coverage.json',
        'evidence/gates/sdk/x86_64-unknown-linux-gnu/3.10/sdk-tests.json',
        'evidence/gates/sdk/x86_64-unknown-linux-gnu/3.10/sdk-tests.log',
        'evidence/gates/sdk/x86_64-unknown-linux-gnu/3.10/sdk-typing.json',
        'evidence/gates/sdk/x86_64-unknown-linux-gnu/3.10/sdk-typing.log',
        'evidence/gates/sdk/x86_64-unknown-linux-gnu/3.11/coverage.json',
        'evidence/gates/sdk/x86_64-unknown-linux-gnu/3.11/sdk-tests.json',
        'evidence/gates/sdk/x86_64-unknown-linux-gnu/3.11/sdk-tests.log',
        'evidence/gates/sdk/x86_64-unknown-linux-gnu/3.11/sdk-typing.json',
        'evidence/gates/sdk/x86_64-unknown-linux-gnu/3.11/sdk-typing.log',
        'evidence/gates/sdk/x86_64-unknown-linux-gnu/3.12/coverage.json',
        'evidence/gates/sdk/x86_64-unknown-linux-gnu/3.12/sdk-tests.json',
        'evidence/gates/sdk/x86_64-unknown-linux-gnu/3.12/sdk-tests.log',
        'evidence/gates/sdk/x86_64-unknown-linux-gnu/3.12/sdk-typing.json',
        'evidence/gates/sdk/x86_64-unknown-linux-gnu/3.12/sdk-typing.log',
        'evidence/gates/sdk/x86_64-unknown-linux-gnu/3.13/coverage.json',
        'evidence/gates/sdk/x86_64-unknown-linux-gnu/3.13/sdk-tests.json',
        'evidence/gates/sdk/x86_64-unknown-linux-gnu/3.13/sdk-tests.log',
        'evidence/gates/sdk/x86_64-unknown-linux-gnu/3.13/sdk-typing.json',
        'evidence/gates/sdk/x86_64-unknown-linux-gnu/3.13/sdk-typing.log',
        'evidence/gates/sdk/x86_64-unknown-linux-gnu/3.14/coverage.json',
        'evidence/gates/sdk/x86_64-unknown-linux-gnu/3.14/sdk-tests.json',
        'evidence/gates/sdk/x86_64-unknown-linux-gnu/3.14/sdk-tests.log',
        'evidence/gates/sdk/x86_64-unknown-linux-gnu/3.14/sdk-typing.json',
        'evidence/gates/sdk/x86_64-unknown-linux-gnu/3.14/sdk-typing.log',
        'evidence/gates/soak/report.json',
        'evidence/gates/transfers-spooled/large.json',
        'evidence/gates/transfers-spooled/workers16.json',
        'evidence/gates/transfers-spooled/workers4.json',
        'evidence/gates/transfers/large.json',
        'evidence/gates/transfers/workers16.json',
        'evidence/gates/transfers/workers4.json',
        'evidence/smb-upstream-audit.json',
        'evidence/x86_64-pc-windows-msvc-tests.log',
        'evidence/x86_64-unknown-linux-gnu-tests.log',
        'release-qualification.json',
        'x86_64-pc-windows-msvc/SHA256SUMS',
        'x86_64-pc-windows-msvc/adoption-manifest-v4.json',
        'x86_64-pc-windows-msvc/cli-regressions.json',
        'x86_64-pc-windows-msvc/extended-qualification.json',
        'x86_64-pc-windows-msvc/extended-regressions.json',
        'x86_64-pc-windows-msvc/python-qualification.json',
        'x86_64-pc-windows-msvc/python-tests.json',
        'x86_64-pc-windows-msvc/python-tests.log',
        'x86_64-pc-windows-msvc/qualification-sbom.cdx.json',
        'x86_64-pc-windows-msvc/qualification.json',
        'x86_64-pc-windows-msvc/release-manifest.json',
        'x86_64-pc-windows-msvc/source-consumer.json',
        'x86_64-pc-windows-msvc/source-consumer.log',
        'x86_64-pc-windows-msvc/spooled-python-qualification.json',
        'x86_64-pc-windows-msvc/spooled-qualification.json',
        'x86_64-pc-windows-msvc/spooled-regressions.json',
        'x86_64-pc-windows-msvc/storage-sbom.cdx.json',
        'x86_64-pc-windows-msvc/webdav-fixture.json',
        'x86_64-unknown-linux-gnu/SHA256SUMS',
        'x86_64-unknown-linux-gnu/adoption-manifest-v4.json',
        'x86_64-unknown-linux-gnu/cli-regressions.json',
        'x86_64-unknown-linux-gnu/commit-faults.json',
        'x86_64-unknown-linux-gnu/extended-qualification.json',
        'x86_64-unknown-linux-gnu/extended-regressions.json',
        'x86_64-unknown-linux-gnu/local-faults.json',
        'x86_64-unknown-linux-gnu/python-qualification.json',
        'x86_64-unknown-linux-gnu/python-tests.json',
        'x86_64-unknown-linux-gnu/python-tests.log',
        'x86_64-unknown-linux-gnu/qualification-sbom.cdx.json',
        'x86_64-unknown-linux-gnu/qualification.json',
        'x86_64-unknown-linux-gnu/release-manifest.json',
        'x86_64-unknown-linux-gnu/source-consumer.json',
        'x86_64-unknown-linux-gnu/source-consumer.log',
        'x86_64-unknown-linux-gnu/spooled-local-faults.json',
        'x86_64-unknown-linux-gnu/spooled-python-qualification.json',
        'x86_64-unknown-linux-gnu/spooled-qualification.json',
        'x86_64-unknown-linux-gnu/spooled-regressions.json',
        'x86_64-unknown-linux-gnu/storage-sbom.cdx.json',
        'x86_64-unknown-linux-gnu/webdav-fixture.json',
    ),
    'public-documents': (
        'release-qualification.json',
        'plenora-storage-{version}-x86_64-pc-windows-msvc.sbom.cdx.json',
        'plenora-storage-{version}-x86_64-pc-windows-msvc.adoption-manifest-v4.json',
        'plenora-storage-{version}-x86_64-unknown-linux-gnu.sbom.cdx.json',
        'plenora-storage-{version}-x86_64-unknown-linux-gnu.adoption-manifest-v4.json',
    ),
}

# Generated archives are allowed only at these exact distribution members.
GENERATED_CONTAINERS = {
    'qualification': (
        'x86_64-pc-windows-msvc/plenora-smb2-{version}.crate',
        'x86_64-pc-windows-msvc/plenora-storage-cli-{version}.crate',
        'x86_64-pc-windows-msvc/plenora-storage-contracts-{version}.tar.gz',
        'x86_64-pc-windows-msvc/plenora-storage-core-{version}.crate',
        'x86_64-pc-windows-msvc/plenora-storage-engine-{version}.crate',
        'x86_64-pc-windows-msvc/plenora-storage-ftp-{version}.crate',
        'x86_64-pc-windows-msvc/plenora-storage-providers-{version}.crate',
        'x86_64-pc-windows-msvc/plenora-storage-s3-{version}.crate',
        'x86_64-pc-windows-msvc/plenora-storage-sftp-{version}.crate',
        'x86_64-pc-windows-msvc/plenora-storage-{version}-source.tar.gz',
        'x86_64-pc-windows-msvc/plenora-storage-{version}-x86_64-pc-windows-msvc.zip',
        'x86_64-pc-windows-msvc/plenora_storage-{python_version}-cp310-abi3-win_amd64.whl',
        'x86_64-unknown-linux-gnu/plenora-smb2-{version}.crate',
        'x86_64-unknown-linux-gnu/plenora-storage-cli-{version}.crate',
        'x86_64-unknown-linux-gnu/plenora-storage-contracts-{version}.tar.gz',
        'x86_64-unknown-linux-gnu/plenora-storage-core-{version}.crate',
        'x86_64-unknown-linux-gnu/plenora-storage-engine-{version}.crate',
        'x86_64-unknown-linux-gnu/plenora-storage-ftp-{version}.crate',
        'x86_64-unknown-linux-gnu/plenora-storage-providers-{version}.crate',
        'x86_64-unknown-linux-gnu/plenora-storage-s3-{version}.crate',
        'x86_64-unknown-linux-gnu/plenora-storage-sftp-{version}.crate',
        'x86_64-unknown-linux-gnu/plenora-storage-{version}-source.tar.gz',
        'x86_64-unknown-linux-gnu/plenora-storage-{version}-x86_64-unknown-linux-gnu.tar.gz',
        'x86_64-unknown-linux-gnu/plenora_storage-{python_version}-cp310-abi3-manylinux_2_34_x86_64.whl',
    ),
}


def cargo_reference():
    """Let the pinned Cargo normalize the release tree, entirely offline."""
    import check_packages as gate
    import os
    import subprocess
    import tarfile
    import tomllib
    with gate.release_checkout() as work:
        env = dict(os.environ, CARGO_TARGET_DIR=str(work.parent / 'cargo-target'))
        result = subprocess.run(['cargo', 'package', '--workspace', '--exclude',
                                 'plenora-storage-py', '--no-verify', '--locked',
                                 '--offline', '--allow-dirty'], cwd=work, env=env,
                                capture_output=True)
        if result.returncode:
            raise ValueError('cannot reproduce Cargo metadata offline from Git')
        output = {}
        for path in (work.parent / 'cargo-target/package').glob('*.crate'):
            with tarfile.open(path) as archive:
                manifest = next(m for m in archive if m.name.endswith('/Cargo.toml'))
                data = archive.extractfile(manifest).read()
                name = tomllib.loads(data.decode())['package']['name']
                root = manifest.name.rsplit('/', 1)[0]
                output[name] = {key: archive.extractfile(root + '/' + key).read()
                                for key in ('Cargo.toml', 'Cargo.lock')}
        return output


def wheel_metadata(distribution):
    import check_packages as gate
    import tomllib
    base = 'crates/plenora-storage-py/'
    config = tomllib.loads(gate.source_bytes(base + 'pyproject.toml').decode())
    project = config['project']
    # Closed supported PEP 621 surface: changes require a deliberate gate update.
    if set(project) != {'name', 'version', 'description', 'requires-python', 'license',
                        'readme', 'authors', 'urls'}:
        raise ValueError('unsupported Python project metadata')
    # Producer order is part of the byte contract, not an unordered header set.
    fields = [('Metadata-Version', '2.4'), ('Name', project['name']),
              ('Version', project['version']), ('License-File', 'LICENSE'),
              ('Summary', project['description']),
              ('Author', ', '.join(a['name'] for a in project['authors'])),
              ('License', project['license']['text']),
              ('Requires-Python', project['requires-python']),
              ('Description-Content-Type', 'text/markdown; charset=UTF-8; variant=GFM')]
    fields += [('Project-URL', key + ', ' + value) for key, value in project['urls'].items()]
    readme = gate.checkout_source_bytes(base + project['readme']).decode().replace('\r\n', '\n') + '\n'
    return fields, readme, config['build-system']['requires'][0].split('==')[1]


def validate_generated(data, name, distribution, entries=None):
    import check_packages as gate
    import json
    distribution = gate.normalize_path(distribution)
    if distribution.startswith('crates/'):
        crate = distribution[7:]
        if name.lower() == '.cargo_vcs_info.json':
            expected = json.dumps({'git': {'sha1': gate.release_revision()},
                                   'path_in_vcs': 'crates/' + crate}, indent=2).encode('utf-8')
            if data != expected:
                raise ValueError('generated Cargo VCS metadata differs from Git')
        else:
            expected = gate.git_input(('cargo-generated',), cargo_reference)
            if data != gate.path_lookup(gate.path_lookup(expected, crate), name):
                raise ValueError('generated Cargo metadata differs from Git')
    elif distribution.startswith('cp'):
        leaf = name.rsplit('/', 1)[-1].upper()
        if leaf not in {'METADATA', 'WHEEL', 'RECORD'}:
            return  # Generated SBOM remains within STOR-LIMIT-003.
        if leaf == 'RECORD':
            if entries is None or (name, data) not in entries:
                raise ValueError('wheel RECORD requires complete wheel context')
            validate_record(entries, distribution)
            return
        fields, body, maturin = wheel_metadata(distribution)
        if leaf == 'WHEEL':
            expected = ('Wheel-Version: 1.0\nGenerator: maturin (' + maturin + ')\n'
                        'Root-Is-Purelib: false\nTag: ' + distribution + '\n')
            if data != expected.encode('utf-8'):
                raise ValueError('unexpected wheel WHEEL fields')
        else:
            expected = ('\n'.join(key + ': ' + value for key, value in fields)
                        + '\n\n' + body).encode('utf-8')
            if data != expected:
                raise ValueError('wheel METADATA differs from pyproject/README')


def validate_record(entries, distribution):
    if not distribution.lower().startswith('cp'):
        return
    import base64
    import csv
    import hashlib
    import io
    records = [(name, data) for name, data in entries if name.lower().endswith('.dist-info/record')]
    if not records:
        return  # Single-member classifier tests; complete inventory requires RECORD.
    if len(records) != 1:
        raise ValueError('invalid wheel RECORD count')
    record, data = records[0]
    expected = [[name, '' if name == record else 'sha256=' + base64.urlsafe_b64encode(
        hashlib.sha256(payload).digest()).rstrip(b'=').decode(),
        '' if name == record else str(len(payload))] for name, payload in entries]
    canonical = io.StringIO(newline='')
    csv.writer(canonical, lineterminator='\n').writerows(expected)
    if data != canonical.getvalue().encode('utf-8'):
        raise ValueError('wheel RECORD members, hashes or sizes differ')
