"""Exact distribution inventories and recursive credential/material checks.

No inventory is learned or updated by this gate. Changes require review of
scripts/package_files.json. Diagnostics never include inspected contents.
"""
import argparse
import base64
import binascii
import bz2
import hashlib
import lzma
import unicodedata
import gzip
import io
import strict_json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import zipfile

from versioning import parse_version, workspace_version

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / 'scripts/package_files.json'


def registry():
    return strict_json.loads(REGISTRY.read_text(encoding='utf-8'))


def inventory(names):
    result = set()
    for name in names:
        path = PurePosixPath(name)
        if (not name or str(path) != name or path.is_absolute() or '..' in path.parts
                or '\\' in name or ':' in name or name in result):
            raise ValueError('invalid or duplicate package member')
        result.add(name)
    return result


def compare(names, expected):
    actual, wanted = inventory(names), inventory(expected)
    if actual != wanted:
        # Names are useful to reviewers; never print file contents.
        raise ValueError(f'package inventory mismatch: extra={sorted(actual-wanted)}, missing={sorted(wanted-actual)}')


def tlv(data, offset):
    tag, length = data[offset:offset+2]
    pos = offset + 2
    if length & 128:
        count = length & 127
        if not 1 <= count <= 4:
            raise ValueError('invalid DER length')
        length = int.from_bytes(data[pos:pos+count], 'big')
        pos += count
    end = pos + length
    if end > len(data):
        raise ValueError('truncated DER')
    return tag, pos, end


def private_der(data):
    try:
        tag, pos, end = tlv(data, 0)
        if tag != 48:
            return False
        fields = []
        while pos < end and len(fields) < 10:
            tag, start, stop = tlv(data, pos)
            fields.append((tag, data[start:stop]))
            pos = stop
        tags = [tag for tag, _ in fields]
        # EncryptedPrivateKeyInfo and public DigestInfo have identical outer
        # tags. Require a password-based encryption OID, not a digest OID.
        encryption = False
        if tags == [48, 4]:
            oid_tag, oid_start, oid_end = tlv(fields[0][1], 0)
            oid = fields[0][1][oid_start:oid_end]
            encryption = oid_tag == 6 and (oid.startswith(bytes.fromhex('2a864886f70d0105')) or
                                            oid.startswith(bytes.fromhex('2a864886f70d010c01')))
        # PKCS#1 RSA, SEC1 EC, PKCS#8, encrypted PKCS#8, PKCS#12 PFX.
        return (len(tags) >= 9 and tags[:9] == [2]*9 or
                tags == [2]*6 or
                tags[:2] == [2, 4] or tags[:3] == [2, 48, 4] or
                encryption or
                tags[:2] == [2, 48] and fields[0][1] == bytes([3]) and
                bytes.fromhex('06092a864886f70d0107') in fields[1][1])
    except (ValueError, IndexError):
        return False


def kerberos_container(data):
    """Recognize embedded containers by header and bounded principal fields."""
    try:
        version = data[1]
        if version <= 2:
            length = int.from_bytes(data[2:6], 'big', signed=True)
            count = int.from_bytes(data[6:8], 'big')
            realm_size = int.from_bytes(data[8:10], 'big')
            return 12 <= length <= len(data)-6 and 1 <= count <= 16 and 1 <= realm_size < length-4
        pos = 2
        if version == 4:
            header = int.from_bytes(data[pos:pos+2], 'big')
            pos += 2 + header
        name_type = int.from_bytes(data[pos:pos+4], 'big')
        count = int.from_bytes(data[pos+4:pos+8], 'big')
        size = int.from_bytes(data[pos+8:pos+12], 'big')
        realm = data[pos+12:pos+12+size]
        return (name_type <= 10 and 1 <= count <= 16 and 1 <= size <= 1024
                and len(realm) == size and all(32 <= b < 127 for b in realm))
    except (IndexError, ValueError):
        return False


def check_content(data):
    pem = rb'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----[ \r\n]+[A-Za-z0-9+/=]'
    if re.search(pem, data.replace(b'\\n', b'\n').replace(b'\\r', b'\r')):
        raise ValueError('private PEM material in distribution')
    if data[:2] in (bytes([5, 1]), bytes([5, 2]), bytes([5, 3]), bytes([5, 4])):
        raise ValueError('Kerberos credential container in distribution')
    if private_der(data):
        raise ValueError('private DER or PKCS#12 material in distribution')
    # Also inspect embedded ASN.1 private containers in native objects/data.
    for match in re.finditer(rb'\x30(?:[\x80-\x84].{1,4}|[\x06-\x7f])(?:\x02\x01[\x00-\x03]|\x30.{1,6}\x06)', data):
        if private_der(data[match.start():match.start()+65536]):
            raise ValueError('embedded private DER material in distribution')
    # Embedded ccache/keytab candidates must have a plausible principal/entry,
    # avoiding rejection of random native instructions matching two header bytes.
    for match in re.finditer(bytes([5]) + b'[\\x01-\\x04]', data):
        offset = match.start()
        candidate = data[offset:offset+65536]
        if kerberos_container(candidate):
            raise ValueError('embedded Kerberos credential container in distribution')
    # Credential documents, rather than source code mentioning field names.
    try:
        value = strict_json.loads(data)
    except strict_json.DuplicateJsonMember:
        raise ValueError("duplicate members in distribution document") from None
    except (ValueError, UnicodeDecodeError):
        value = None
    def credentials(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in {'password', 'private_key', 'client_secret', 'secret_access_key', 'aws_secret_access_key', 'account_key', 'sas_token', 'access_token', 'refresh_token'} and isinstance(item, str) and item and item != 'do-not-persist' and not item.startswith(('$', '{{')):
                    raise ValueError('credential value in distribution document')
                if key not in {'enumDescriptions', 'x-enumDescriptions'}:
                    credentials(item)
        elif isinstance(value, list):
            for item in value:
                credentials(item)
    credentials(value)
    # Bare/multiline base64 key blocks, including quoted source literals.
    # Declared limit STOR-LIMIT-003 (owner decision 2026-11-06): textual
    # recodings (hex, prefixed/mixed base64, other encodings) are outside this
    # guard's scope. Exact inventories and PR review still cover those files.
    for match in re.finditer(rb'(?<![A-Za-z0-9+/])(?:[A-Za-z0-9+/]{4}[ \r\n\t]*){4,}(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?', data):
        try:
            decoded = base64.b64decode(re.sub(rb'\s+', b'', match[0]), validate=True)
        except (ValueError, binascii.Error):
            continue
        if private_der(decoded):
            raise ValueError('base64 private key material in distribution')
    for match in re.finditer(rb'printf[ \t]+["\'][^"\']+\\n[^"\']+["\'][ \t]*\|[ \t]*smbpasswd', data):
        # The public testing feature promises this fixed fixture value.
        # This exception does not bypass any private-material checks above.
        if match[0] != b'printf "testpass\\ntestpass\\n" | smbpasswd':
            raise ValueError('fixed Samba password in distribution')


def members(data):
    """Read regular archive members without extraction; reject links/duplicates."""
    source = io.BytesIO(data)
    directories = []
    all_names = []
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            result = []
            for item in archive.infolist():
                all_names.append(item.filename.rstrip('/'))
                if item.is_dir():
                    directories.append(item.filename.rstrip('/'))
                    continue
                if stat.S_IFMT(item.external_attr >> 16) not in (0, stat.S_IFREG):
                    raise ValueError('non-file archive member')
                result.append((item.filename, archive.read(item)))
    else:
        source.seek(0)
        with tarfile.open(fileobj=source, mode='r:*') as archive:
            result = []
            for item in archive:
                all_names.append(item.name.rstrip('/'))
                if item.isdir():
                    directories.append(item.name.rstrip('/'))
                    continue
                if not item.isfile():
                    raise ValueError('non-file archive member')
                result.append((item.name, archive.extractfile(item).read()))
    inventory(all_names)
    parents = {str(parent) for name, _ in result for parent in PurePosixPath(name).parents}
    if not set(directories) <= parents:
        raise ValueError('unexpected empty archive directory')
    return result


def distribution_policy(name):
    """Resolve only explicitly supported distribution identities."""
    name = PurePosixPath(name).name
    policy = registry()
    kind = None
    prefix = ''
    version = ''
    python_version = ''
    if name.endswith('.crate'):
        match = re.fullmatch(r'(plenora-[a-z0-9-]+)-(\d+\.\d+\.\d+(?:-[a-z0-9.]+)?)\.crate', name)
        if not match or match[1] not in policy['crates']:
            raise ValueError('unknown Cargo archive')
        kind = 'crates/' + match[1]
        expected = policy['crates'][match[1]]
        prefix = name[:-6] + '/'
    elif name.endswith('.whl'):
        match = re.fullmatch(r'plenora_storage-([^-]+)-(.+)\.whl', name)
        tags = policy.get('wheel-tags', {})
        if not match or match[2] not in tags:
            raise ValueError('unknown wheel target or ABI')
        version = match[1]
        kind = tags[match[2]]
    elif name.startswith('plenora-storage-') and name.endswith(('.tar.gz', '.zip')):
        match = re.fullmatch(r'plenora-storage-(\d+\.\d+\.\d+(?:-[a-z0-9.]+)?)-(source|qualification|x86_64-pc-windows-msvc|x86_64-unknown-linux-gnu)\.(?:tar.gz|zip)', name)
        if match:
            version, variant = match.groups()
            if name.endswith('.zip') != (variant == 'x86_64-pc-windows-msvc'):
                raise ValueError('unknown distribution archive format')
            kind = {'source': 'source', 'qualification': 'qualification',
                    'x86_64-pc-windows-msvc': 'cli-windows', 'x86_64-unknown-linux-gnu': 'cli-linux'}[variant]
            prefix = f'plenora-storage-{version}/' if variant == 'source' else f'{version}/' if variant == 'qualification' else ''
        else:
            match = re.fullmatch(r'plenora-storage-contracts-(.+)\.tar.gz', name)
            if not match:
                raise ValueError('unknown distribution archive')
            kind = 'contracts'
    elif name.startswith('plenora_storage-') and name.endswith('.tar.gz'):
        raise ValueError('Python sdist distribution is not declared')
    else:
        raise ValueError('unknown distribution archive')
    if not kind.startswith('crates/'):
        python_version = parse_version(version).python if kind == 'qualification' else version
        expected = [n.replace('{version}', version).replace('{python_version}', python_version)
                    for n in policy[kind]]
    rules = policy.get('member_rules', {}).get(kind, {})
    rules = {n.replace('{version}', version).replace('{python_version}', python_version): rule
             for n, rule in rules.items()}
    if not set(rules) <= set(expected):
        raise ValueError('rules outside package inventory')
    return kind, prefix, {n: rules.get(n, {}) for n in expected}


def verify_distribution(path, member=None):
    path = Path(path)
    if member is None and path.name.endswith(('.crate', '.whl', '.tar.gz', '.zip')):
        return scan_distribution(path.read_bytes(), path.name)
    policy = registry()
    version = workspace_version().native
    kind = 'qualification' if member is not None else 'public-documents'
    expected = {n.replace('{version}', version).replace('{python_version}', parse_version(version).python): n
                for n in policy[kind]}
    name = member if member is not None else path.name
    if name not in expected:
        raise ValueError('undeclared standalone distribution member')
    rule = policy.get('member_rules', {}).get(kind, {}).get(expected[name], {})
    scan(path.read_bytes(), name, rule=rule)


def is_tar(data):
    if len(data) < 512:
        return False
    try:
        tarfile.TarInfo.frombuf(data[:512], 'utf-8', 'surrogateescape')
        return True
    except tarfile.HeaderError:
        return False


def is_text(data):
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return False
    return all(c in '\t\r\n' or unicodedata.category(c) != 'Cc' for c in text)


def container_format(data):
    signatures = (
        ('gzip', (b'\x1f\x8b',)), ('bzip2', (b'BZh',)),
        ('xz', (b'\xfd7zXZ\x00',)), ('zstd', (b'\x28\xb5\x2f\xfd',)),
        ('lz4', (b'\x04\x22\x4d\x18', b'\x02\x21\x4c\x18')),
        ('zip', (b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08')),
        ('7z', (b'7z\xbc\xaf\x27\x1c',)), ('rar', (b'Rar!\x1a\x07',)),
        ('ar', (b'!<arch>\n', b'!<thin>\n')),
        ('cpio', (b'070701', b'070702', b'070707', b'\xc7\x71', b'\x71\xc7')),
    )
    for kind, magic in signatures:
        if data.startswith(magic):
            return kind
    # Shared skippable frames for zstd/lz4 are also containers.
    if len(data) >= 4 and 0x184D2A50 <= int.from_bytes(data[:4], 'little') <= 0x184D2A5F:
        return 'zstd'
    if data[257:262] == b'ustar' or is_tar(data):
        return 'tar'
    return None


SUPPORTED = {'gzip', 'bzip2', 'xz', 'zip', 'tar'}


def scan_entries(entries, expected, depth):
    compare([n for n, _ in entries], expected)
    roles = set()
    for rule in expected.values():
        if 'build' in rule:
            build = rule['build']
            identity = (build['target'], build['role'], build.get('abi'))
            if identity in roles:
                raise ValueError('duplicate build role in distribution')
            roles.add(identity)
    for name, data in entries:
        scan(data, name, depth, expected[name])


def check_build(data, rule, name):
    """Validate declared build outputs without predicting their bytes."""
    build = rule['build']
    if 'sha256' in rule or 'source' in rule:
        raise ValueError('build output cannot have a source digest')
    target = build.get('target')
    if target not in {'x86_64-pc-windows-msvc', 'x86_64-unknown-linux-gnu'}:
        raise ValueError('unknown build target')
    role = build.get('role', '')
    if 'container' in rule:
        if rule['container'].get('distribution') is not True:
            raise ValueError('build container requires distribution inventory')
        distribution, _, _ = distribution_policy(name)
        expected_role = ('cargo:' + distribution[7:] if distribution.startswith('crates/')
                         else 'wheel' if distribution.startswith('wheel-')
                         else 'cli-archive' if distribution.startswith('cli-') else distribution)
        if role != expected_role or role not in {'wheel', 'cli-archive', 'source', 'contracts'} and not role.startswith('cargo:'):
            raise ValueError('build container role mismatch')
        if (role == 'wheel' and build.get('abi') != 'cp310-abi3'
                or role != 'wheel' and 'abi' in build):
            raise ValueError('unknown build container ABI')
        if (distribution.endswith('-windows') and target != 'x86_64-pc-windows-msvc'
                or distribution.endswith('-linux') and target != 'x86_64-unknown-linux-gnu'
                or '/' in name and name.split('/', 1)[0] != target):
            raise ValueError('build container target mismatch')
        return
    if role not in {'cli', 'python-extension'}:
        raise ValueError('unknown native build role')
    if (role == 'python-extension' and build.get('abi') != 'cp310-abi3'
            or role == 'cli' and 'abi' in build):
        raise ValueError('unknown native ABI')
    if target == 'x86_64-unknown-linux-gnu':
        # ELF64, little-endian, x86-64, current version, executable or DSO.
        if (build.get('format') != 'elf' or len(data) < 64
                or data[:7] != bytes([127, 69, 76, 70, 2, 1, 1])
                or int.from_bytes(data[18:20], 'little') != 62
                or int.from_bytes(data[20:24], 'little') != 1
                or int.from_bytes(data[52:54], 'little') != 64
                or int.from_bytes(data[16:18], 'little') not in
                   ({3} if role == 'python-extension' else {2, 3})):
            raise ValueError('native ELF type or architecture mismatch')
    else:
        offset = int.from_bytes(data[60:64], 'little')
        flags = int.from_bytes(data[offset+22:offset+24], 'little')
        if (build.get('format') != 'pe' or len(data) < 64 or data[:2] != b'MZ'
                or offset < 64 or offset + 26 > len(data)
                or data[offset:offset+4] != bytes([80, 69, 0, 0])
                or int.from_bytes(data[offset+4:offset+6], 'little') != 0x8664
                or int.from_bytes(data[offset+24:offset+26], 'little') != 0x20b
                or not flags & 2 or bool(flags & 0x2000) != (role == 'python-extension')):
            raise ValueError('native PE type or architecture mismatch')


def source_bytes(name):
    """Read a tracked source blob, never an arbitrary build artifact."""
    inventory([name])
    result = subprocess.run(['git', 'show', 'HEAD:' + name], cwd=ROOT,
                            capture_output=True)
    if result.returncode:
        raise ValueError('binary source is not tracked in HEAD')
    return result.stdout


def check_source_digests():
    for rules in registry().get('member_rules', {}).values():
        for rule in rules.values():
            if 'build' in rule:
                if 'sha256' in rule or 'source' in rule:
                    raise ValueError('build output cannot have a source digest')
            elif 'sha256' in rule or 'container' in rule:
                if 'source' not in rule:
                    raise ValueError('binary source provenance missing')
                data = source_bytes(rule['source'])
                if hashlib.sha256(data).hexdigest() != rule.get('sha256'):
                    raise ValueError('source digest differs from git')
                scan(data, rule=rule)


def scan_distribution(data, name, depth=0):
    if depth > 8:
        raise ValueError('archive nesting limit exceeded')
    if container_format(data) not in SUPPORTED:
        raise ValueError('unsupported distribution container')
    distribution, prefix, expected = distribution_policy(name)
    expected_format = 'zip' if distribution in {'wheel-windows', 'wheel-linux', 'cli-windows'} else 'gzip'
    if container_format(data) != expected_format:
        raise ValueError('distribution container format mismatch')
    entries = members(data)
    if any(not n.startswith(prefix) for n, _ in entries):
        raise ValueError('unexpected distribution root')
    scan_entries([(n[len(prefix):], value) for n, value in entries], expected, depth+1)


def scan(data, name='', depth=0, rule=None):
    if depth > 8:
        raise ValueError('archive nesting limit exceeded')
    # A named top-level distribution has its own explicit inventory. Nested
    # members always receive a rule, so a plausible filename grants nothing.
    if rule is None and name.endswith(('.crate', '.whl', '.tar.gz', '.zip')):
        return scan_distribution(data, name, depth)
    rule = rule or {}
    kind = container_format(data)
    if kind and kind not in SUPPORTED:
        raise ValueError('unsupported container format')
    if kind and 'container' not in rule:
        raise ValueError('undeclared container')
    if 'build' in rule:
        check_build(data, rule, name)
    elif 'container' in rule or 'sha256' in rule or not is_text(data):
        if hashlib.sha256(data).hexdigest() != rule.get('sha256'):
            raise ValueError('binary digest missing or mismatched')
    if kind:
        declaration = rule['container']
        if declaration.get('format') != kind:
            raise ValueError('container format mismatch')
        if declaration.get('distribution') is True:
            return scan_distribution(data, name, depth+1)
        if kind in {'zip', 'tar'}:
            return scan_entries(members(data), declaration['members'], depth+1)
        expanded = {'gzip': gzip.decompress, 'bzip2': bz2.decompress, 'xz': lzma.decompress}[kind](data)
        return scan(expanded, 'payload', depth+1, declaration['payload'])
    if 'container' in rule:
        raise ValueError('declared container missing')
    check_content(data)


def check_archive(path, kind, version='', prefix='', platform=''):
    policy = registry()
    expected = {n.replace('{version}', version).replace('{native}', '_native.pyd' if platform == 'windows' else '_native.abi3.so'):
                policy.get('member_rules', {}).get(kind, {}).get(n, {}) for n in policy[kind]}
    entries = members(Path(path).read_bytes())
    prefix = prefix + '/' if prefix else ''
    if any(not n.startswith(prefix) for n, _ in entries):
        raise ValueError('unexpected archive root')
    scan_entries([(n[len(prefix):], data) for n, data in entries], expected, 1)


def check_crate(path, name, version, has_vcs=True):
    _, prefix, expected = distribution_policy(f'{name}-{version}.crate')
    if not has_vcs:
        expected.pop('.cargo_vcs_info.json', None)
    entries = members(Path(path).read_bytes())
    if any(not n.startswith(prefix) for n, _ in entries):
        raise ValueError('unexpected crate root')
    scan_entries([(n[len(prefix):], data) for n, data in entries], expected, 1)


def cargo(*args):
    return subprocess.check_output(['cargo', *args], cwd=ROOT, text=True, encoding='utf-8')


def check_cargo(build=False):
    check_source_digests()
    metadata = strict_json.loads(cargo('metadata', '--locked', '--offline', '--no-deps', '--format-version', '1'))
    packages = metadata['packages']
    compare([p['name'] for p in packages], registry()['crates'])
    vcs = (ROOT / '.git').exists()
    for package in packages:
        name = package['name']
        expected = registry()['crates'][name]
        if not vcs:
            expected = [f for f in expected if f != '.cargo_vcs_info.json']
        listing = cargo('package', '--list', '--locked', '--offline', '--allow-dirty', '-p', name)
        compare(listing.splitlines(), expected)
        directory = Path(package['manifest_path']).parent
        for item in expected:
            if item in {'.cargo_vcs_info.json', 'Cargo.lock'}:
                continue
            source = directory / ('Cargo.toml' if item == 'Cargo.toml.orig' else item)
            if not source.exists() and package.get('license_file'):
                source = directory / package['license_file']
            scan(source.read_bytes(), item, rule=registry().get('member_rules', {}).get('crates/' + name, {}).get(item, {}))
        print(f'{name}: exact inventory and content PASS ({len(expected)} files)')
    if build:
        cargo('package', '--workspace', '--exclude', 'plenora-storage-py', '--no-verify', '--locked', '--offline', '--allow-dirty')
        for package in packages:
            if package['name'] != 'plenora-storage-py':
                name, version = package['name'], package['version']
                check_crate(Path(metadata['target_directory']) / 'package' / f'{name}-{version}.crate', name, version, vcs)
                print(f'{name}: built archive PASS')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', action='store_true')
    args = parser.parse_args()
    check_cargo(args.build)


if __name__ == '__main__':
    main()
