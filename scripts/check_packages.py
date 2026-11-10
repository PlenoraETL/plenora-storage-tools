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


# Single authority for native members, including every supported wheel tag.
# Values: policy inventory, exact member, format, shared library.
NATIVE_DISTRIBUTIONS = {
    'cli-linux': ('cli-linux', 'plenora-storage', 'elf', False),
    'cli-windows': ('cli-windows', 'plenora-storage.exe', 'pe', False),
    'cp310-abi3-win_amd64': ('wheel-windows', 'plenora_storage/_native.pyd', 'pe', True),
    'cp310-abi3-manylinux_2_34_x86_64': ('wheel-linux', 'plenora_storage/_native.abi3.so', 'elf', True),
    'cp310-abi3-manylinux_2_39_x86_64': ('wheel-linux', 'plenora_storage/_native.abi3.so', 'elf', True),
    'qualification-linux': ('qualification', 'x86_64-unknown-linux-gnu/plenora-storage', 'elf', False),
    'qualification-windows': ('qualification', 'x86_64-pc-windows-msvc/plenora-storage.exe', 'pe', False),
    '.crate': (None, None, None, None),
}


def normalize_path(name):
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name
            or any(part in {'', '.', '..'} for part in name.split('/'))
            or any(unicodedata.category(c) == 'Cc' for c in name)):
        raise ValueError('invalid package member path')
    return unicodedata.normalize('NFC', unicodedata.normalize('NFC', name).casefold())


def inventory(names):
    result = set()
    for name in names:
        key = normalize_path(name)
        if key in result:
            raise ValueError('invalid or duplicate package member')
        result.add(key)
    return result


def path_lookup(mapping, name):
    """The only keyed lookup for path inventories, rules and native tables.

    Missing entries and canonical collisions fail closed; defaults belong only
    to construction of the complete inventory, never to verification lookups.
    """
    inventory(mapping)
    canonical = {normalize_path(key): value for key, value in mapping.items()}
    try:
        return canonical[normalize_path(name)]
    except KeyError:
        raise ValueError('undeclared package policy key') from None


def member_rules(policy, kind, version=None, python_version=None):
    """Materialize every declared member, then overlay reviewed supplements."""
    names = (path_lookup(path_lookup(policy, 'crates'), kind[7:])
             if normalize_path(kind).startswith('crates/') else path_lookup(policy, kind))
    inventory(names)
    rules = {name: {} for name in names}
    if 'member_rules' in policy:
        groups = path_lookup(policy, 'member_rules')
        if normalize_path(kind) in inventory(groups):
            supplements = path_lookup(groups, kind)
            inventory(supplements)
            for name, rule in supplements.items():
                # Resolve the actual inventory spelling, retaining rule identity
                # for the separate digest recorder.
                actual = path_lookup({n: n for n in names}, name)
                rules[actual] = rule
    names = [n.replace('{version}', version) if version is not None else n for n in rules]
    names = [n.replace('{python_version}', python_version) if python_version is not None else n for n in names]
    inventory(names)
    return dict(zip(names, rules.values()))


def native_member(distribution, name):
    if not distribution or not name:
        return None
    distribution = normalize_path(distribution)
    if distribution == 'qualification':
        candidates = {item[1]: item for item in NATIVE_DISTRIBUTIONS.values()
                      if item[0] == distribution}
    elif distribution in inventory(NATIVE_DISTRIBUTIONS):
        item = path_lookup(NATIVE_DISTRIBUTIONS, distribution)
        candidates = {item[1]: item} if item[1] else {}
    else:
        return None
    if normalize_path(name) in inventory(candidates):
        return path_lookup(candidates, name)
    return None


def repository_path(distribution, name):
    """Map distribution-relative names into the one repository namespace."""
    name = normalize_path(name)
    distribution = normalize_path(distribution) if distribution else ''
    if distribution.startswith('crates/'):
        if name == 'cargo.toml.orig':
            name = 'Cargo.toml'
        if name in {'license', 'readme.md'} and distribution != 'crates/plenora-smb2':
            return name
        return normalize_path(distribution + '/' + name)
    if distribution in NATIVE_DISTRIBUTIONS and distribution.startswith('cp'):
        if name.startswith('plenora_storage/'):
            return normalize_path('crates/plenora-storage-py/python/' + name)
        if name.endswith('.dist-info/licenses/license'):
            return 'license'
        return None  # Generated wheel metadata.
    if distribution in {'source', 'sdist', 'cli-linux', 'cli-windows', 'contracts'}:
        return name
    return None


def tracked_paths():
    names = subprocess.run(['git', 'ls-files', '--cached', '--with-tree=HEAD', '-z'],
                           cwd=ROOT, capture_output=True, check=True).stdout.decode('utf-8').split('\0')
    names = [n for n in names if n]
    inventory(names)
    return {normalize_path(n): n for n in names}


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


# Closed tar dialect used by Cargo and Python's tar writer. No global PAX,
# GNU longname/longlink/sparse, vendor keys, links or special file types.
TAR_PAX_KEYS = frozenset({'path', 'linkpath', 'size', 'mtime', 'uid', 'gid', 'uname', 'gname'})


class DistributionTarInfo(tarfile.TarInfo):
    def _proc_member(self, archive):
        if self.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE, tarfile.XHDTYPE):
            raise ValueError('non-file archive member or forbidden tar extension')
        if self.type == tarfile.XHDTYPE:
            position = archive.fileobj.tell()
            payload = archive.fileobj.read(self.size)
            archive.fileobj.seek(position)
            offset, seen = 0, set()
            while offset < len(payload):
                match = re.match(rb'([1-9][0-9]*) ([^=\n]+)=', payload[offset:])
                if not match:
                    raise ValueError('invalid PAX record')
                end = offset + int(match[1])
                if end > len(payload) or end <= offset + match.end() or payload[end-1:end] != b'\n':
                    raise ValueError('invalid PAX record')
                key = match[2].decode('ascii', errors='replace')
                value = payload[offset + match.end():end-1]
                if key not in TAR_PAX_KEYS or key in seen or (key == 'linkpath' and value):
                    raise ValueError('forbidden PAX key or value')
                seen.add(key)
                offset = end
        return super()._proc_member(archive)


def members(data):
    """Read regular archive members without extraction; reject links/duplicates."""
    # Inspect the whole expanded stream, including bytes hidden by parsers.
    check_content(data)
    compression = container_format(data)
    if compression in {'gzip', 'bzip2', 'xz'}:
        data = {'gzip': gzip.decompress, 'bzip2': bz2.decompress, 'xz': lzma.decompress}[compression](data)
        check_content(data)
    source = io.BytesIO(data)
    directories = []
    all_names = []
    if container_format(data) == 'zip':
        with zipfile.ZipFile(source) as archive:
            result = []
            for item in archive.infolist():
                all_names.append(item.orig_filename.removesuffix('/') if item.is_dir() else item.orig_filename)
                unix_type = stat.S_IFMT(item.external_attr >> 16)
                dos_directory = bool(item.external_attr & 0x10)
                if unix_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ValueError('non-file archive member')
                if item.is_dir():
                    if (unix_type not in (0, stat.S_IFDIR) or not dos_directory
                            or item.file_size != 0 or archive.read(item) != b''):
                        raise ValueError('invalid archive directory')
                    directories.append(item.orig_filename.removesuffix('/'))
                    continue
                if unix_type == stat.S_IFDIR or dos_directory:
                    raise ValueError('non-file archive member')
                payload = archive.read(item)
                check_content(payload)
                result.append((item.orig_filename, payload))
    else:
        source.seek(0)
        with tarfile.open(fileobj=source, mode='r:', tarinfo=DistributionTarInfo) as archive:
            result = []
            for item in archive:
                all_names.append(item.name.removesuffix('/') if item.isdir() else item.name)
                if item.isdir():
                    if item.size != 0:
                        raise ValueError('nonempty archive directory')
                    directories.append(item.name.removesuffix('/'))
                    continue
                if item.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
                    raise ValueError('non-file archive member')
                result.append((item.name, archive.extractfile(item).read()))
    inventory(all_names)
    parents = {normalize_path(str(parent)) for name, _ in result for parent in PurePosixPath(name).parents if str(parent) != '.'}
    if not inventory(directories) <= parents:
        raise ValueError('unexpected empty archive directory')
    return result


def distribution_policy(name):
    """Resolve only explicitly supported distribution identities."""
    name = normalize_path(name).rsplit('/', 1)[-1]
    policy = registry()
    check_build_paths()
    kind = None
    prefix = ''
    version = ''
    python_version = ''
    if name.endswith('.crate'):
        match = re.fullmatch(r'(plenora-[a-z0-9-]+)-(\d+\.\d+\.\d+(?:-[a-z0-9.]+)?)\.crate', name)
        if not match or normalize_path(match[1]) not in inventory(path_lookup(policy, 'crates')):
            raise ValueError('unknown Cargo archive')
        kind = 'crates/' + match[1]
        prefix = name[:-6] + '/'
    elif name.endswith('.whl'):
        match = re.fullmatch(r'plenora_storage-([^-]+)-(.+)\.whl', name)
        tags = {tag: item[0] for tag, item in NATIVE_DISTRIBUTIONS.items() if tag.startswith('cp')}
        if path_lookup(policy, 'wheel-tags') != tags:
            raise ValueError('wheel tags differ from code table')
        if not match or match[2] not in tags:
            raise ValueError('unknown wheel target or ABI')
        version = match[1]
        kind = path_lookup(tags, match[2])
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
    python_version = parse_version(version).python if kind == 'qualification' else version
    return kind, prefix, member_rules(policy, kind, version, python_version)


def verify_distribution(path, member=None):
    path = Path(path)
    if member is None and path.name.endswith(('.crate', '.whl', '.tar.gz', '.zip')):
        return scan_distribution(path.read_bytes(), path.name)
    policy = registry()
    version = workspace_version().native
    kind = 'qualification' if member is not None else 'public-documents'
    expected = member_rules(policy, kind, version, parse_version(version).python)
    name = member if member is not None else path.name
    if normalize_path(name) not in inventory(expected):
        raise ValueError('undeclared standalone distribution member')
    rule = path_lookup(expected, name)
    classify_member(path.read_bytes(), name, rule=rule, distribution=kind)


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


def scan_entries(entries, expected, depth, distribution=''):
    check_build_paths()
    check_build_paths(expected)
    compare([n for n, _ in entries], expected)
    rules = {normalize_path(n): rule for n, rule in expected.items()}
    tracked = tracked_paths()
    for name, data in entries:
        classify_member(data, name, depth, path_lookup(rules, name), distribution, _tracked=tracked)


def classify_member(data, name='', depth=0, rule=None, distribution='', *, _tracked=None):
    """Single entry for native, source, metadata and container classification."""
    if depth > 8:
        raise ValueError('archive nesting limit exceeded')
    check_build_paths({name or 'payload': rule or {}})
    if rule is None and name.endswith(('.crate', '.whl', '.tar.gz', '.zip')):
        return scan_distribution(data, name, depth)
    rule = rule or {}
    tracked = tracked_paths() if _tracked is None else _tracked
    policy = registry()
    protected = (inventory(path_lookup(policy, 'source')) if 'source' in policy else set()) | set(tracked)
    source = repository_path(distribution, name) if name else None
    native = native_member(distribution, name)
    if native:
        if normalize_path(name) in protected or source in protected:
            raise ValueError('native path conflicts with tracked or inventoried source')
        if rule:
            raise ValueError('native member cannot have a policy rule')
        check_native(data, native)
    else:
        if source in protected and ('source' in rule or not is_text(data)):
            if normalize_path(rule['source'] if 'source' in rule else source) != source:
                raise ValueError('source provenance differs from repository mapping')
            if source in tracked and hashlib.sha256(data).hexdigest() != hashlib.sha256(source_bytes(path_lookup(tracked, source))).hexdigest():
                raise ValueError('binary source digest differs from git')
        _scan_content(data, name, depth, rule)


def check_native(data, native):
    """Only called after matching the code table and distribution context."""
    _, _, format, library = native
    target = 'x86_64-unknown-linux-gnu' if format == 'elf' else 'x86_64-pc-windows-msvc'
    role = 'python-extension' if library else 'cli'
    # STOR-LIMIT-004: native build outputs are checked only for format,
    # not scanned for content (including appended/embedded payloads).
    # CI builds from inventoried, digested/scanned sources mitigate a
    # compromised build. Reproducibility or build attestation closes it.
    if target == 'x86_64-unknown-linux-gnu':
        # ELF64, little-endian, x86-64, current version, executable or DSO.
        if (format != 'elf' or len(data) < 64
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
        if (format != 'pe' or len(data) < 64 or data[:2] != b'MZ'
                or offset < 64 or offset + 26 > len(data)
                or data[offset:offset+4] != bytes([80, 69, 0, 0])
                or int.from_bytes(data[offset+4:offset+6], 'little') != 0x8664
                or int.from_bytes(data[offset+24:offset+26], 'little') != 0x20b
                or not flags & 2 or bool(flags & 0x2000) != (role == 'python-extension')):
            raise ValueError('native PE type or architecture mismatch')


def source_bytes(name):
    """Read a tracked source blob, never an arbitrary build artifact."""
    tracked = tracked_paths()
    if normalize_path(name) not in inventory(tracked):
        raise ValueError('binary source is not tracked in HEAD')
    name = path_lookup(tracked, name)
    result = subprocess.run(['git', 'show', 'HEAD:' + name], cwd=ROOT,
                            capture_output=True)
    if result.returncode:
        raise ValueError('binary source is not tracked in HEAD')
    return result.stdout


def check_build_paths(rules=None):
    """Policy cannot assign any build/native role, even inside containers."""
    def check(group):
        inventory(group)
        for rule in group.values():
            if 'build' in rule:
                raise ValueError('policy build roles are forbidden')
            if 'container' in rule:
                container = rule['container']
                if 'members' in container:
                    check(path_lookup(container, 'members'))
                if 'payload' in container:
                    check({'payload': container['payload']})
    policy = registry()
    for group in (list(path_lookup(policy, 'member_rules').values()) if 'member_rules' in policy else []) + ([rules] if rules is not None else []):
        check(group)
    for kind in ('cli-linux', 'cli-windows', 'wheel-linux', 'wheel-windows'):
        if kind not in policy:
            continue
        allowed = {normalize_path(item[1]) for item in NATIVE_DISTRIBUTIONS.values() if item[0] == kind}
        for name in path_lookup(policy, kind):
            key = normalize_path(name)
            if (key.endswith(('.so', '.pyd', '.exe', '.dll')) or key == 'plenora-storage') and key not in allowed:
                raise ValueError('native name differs from code table')


def check_source_digests():
    check_build_paths()
    policy = registry()
    for rules in (path_lookup(policy, 'member_rules').values() if 'member_rules' in policy else []):
        for rule in rules.values():
            if ('container' in rule and rule['container'].get('distribution') is True):
                continue  # Contents are checked recursively, never opaque.
            if 'sha256' in rule or 'container' in rule:
                if 'source' not in rule:
                    raise ValueError('binary source provenance missing')
                data = source_bytes(rule['source'])
                if hashlib.sha256(data).hexdigest() != rule.get('sha256'):
                    raise ValueError('source digest differs from git')
                classify_member(data, rule=rule)


def relative_entries(entries, prefix):
    """Validate and remove a distribution root using canonical components."""
    if not prefix:
        return entries
    root = normalize_path(prefix.removesuffix('/'))
    count = len(root.split('/'))
    if any(not normalize_path(name).startswith(root + '/') for name, _ in entries):
        raise ValueError('unexpected distribution root')
    return [('/'.join(name.split('/')[count:]), data) for name, data in entries]


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
    identity = normalize_path(name).rsplit('/', 1)[-1]
    context = identity.split('-', 2)[2][:-4] if identity.endswith('.whl') else distribution
    scan_entries(relative_entries(entries, prefix), expected, depth+1, context)


def _scan_content(data, name='', depth=0, rule=None):
    if depth > 8:
        raise ValueError('archive nesting limit exceeded')
    rule = rule or {}
    check_build_paths({name or 'payload': rule})
    kind = container_format(data)
    if kind and kind not in SUPPORTED:
        raise ValueError('unsupported container format')
    if kind and 'container' not in rule:
        raise ValueError('undeclared container')
    recursive = ('container' in rule and rule['container'].get('distribution') is True)
    if not recursive and ('container' in rule or 'sha256' in rule or not is_text(data)):
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
        check_content(expanded)
        return classify_member(expanded, 'payload', depth+1, declaration['payload'])
    if 'container' in rule:
        raise ValueError('declared container missing')
    check_content(data)


# Compatibility name: all callers still enter the same classifier.
scan = classify_member


def check_archive(path, kind, version='', prefix='', platform=''):
    # Legacy entry point: never let caller-supplied kind assign a native role.
    actual, root, _ = distribution_policy(Path(path).name)
    if actual != kind or (prefix and normalize_path(prefix) != normalize_path(root.removesuffix('/'))):
        raise ValueError('distribution identity mismatch')
    return scan_distribution(Path(path).read_bytes(), Path(path).name)


def check_crate(path, name, version, has_vcs=True):
    kind, prefix, expected = distribution_policy(f'{name}-{version}.crate')
    if not has_vcs:
        del expected[path_lookup({n: n for n in expected}, '.cargo_vcs_info.json')]
    entries = members(Path(path).read_bytes())
    scan_entries(relative_entries(entries, prefix), expected, 1, kind)


def cargo(*args):
    return subprocess.check_output(['cargo', *args], cwd=ROOT, text=True, encoding='utf-8')


def check_cargo(build=False):
    check_source_digests()
    metadata = strict_json.loads(cargo('metadata', '--locked', '--offline', '--no-deps', '--format-version', '1'))
    packages = metadata['packages']
    compare([p['name'] for p in packages], path_lookup(registry(), 'crates'))
    vcs = (ROOT / '.git').exists()
    for package in packages:
        name = package['name']
        rules = member_rules(registry(), 'crates/' + name)
        expected = list(rules)
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
            classify_member(source.read_bytes(), item, distribution='crates/' + name, rule=path_lookup(rules, item))
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
