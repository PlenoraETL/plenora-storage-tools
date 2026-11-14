"""Pure byte/path classification and closed archive decoders.

No repository access, build tool, process or network is available here.
"""
import base64
import binascii
import bz2
import lzma
import re
import struct
import tarfile
import unicodedata
import zlib
import strict_json

TAR_PAX_KEYS = frozenset({'path', 'linkpath', 'size', 'mtime', 'uid', 'gid', 'uname', 'gname'})
SUPPORTED = {'gzip', 'bzip2', 'xz', 'zip', 'tar'}

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
    # STOR-LIMIT-003 (owner decision 2026-11-10): recodings remain a scanner
    # limit, but every distributed source must now equal its Git checkout bytes.
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


class DistributionTarInfo(tarfile.TarInfo):
    def _proc_member(self, archive):
        if self.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE, tarfile.XHDTYPE):
            raise ValueError('non-file archive member or forbidden tar extension')
        if self.type == tarfile.XHDTYPE:
            position = archive.fileobj.tell()
            payload = archive.fileobj.read(self.size)
            padding = archive.fileobj.read((-self.size) % 512)
            archive.fileobj.seek(position)
            if len(payload) != self.size or any(padding):
                raise ValueError('invalid PAX payload or padding')
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


def expand(data, kind, name=''):
    """Consume one stream; optional gzip names must match the distribution."""
    if kind == 'gzip':
        if len(data) < 18 or data[2] != 8 or data[3] not in (0, 8):
            raise ValueError('undeclared gzip header fields')
        if data[3] == 8:
            end = data.find(b'\0', 10)
            allowed = {name.encode(), name.removesuffix('.gz').encode()} if name else set()
            if end < 0 or data[10:end] not in allowed:
                raise ValueError('undeclared gzip filename')
        decoder = zlib.decompressobj(31)
    else:
        decoder = {'bzip2': bz2.BZ2Decompressor, 'xz': lzma.LZMADecompressor}[kind]()
    expanded = decoder.decompress(data)
    if not decoder.eof or decoder.unused_data:
        raise ValueError('incomplete or trailing compression stream')
    return expanded


def validate_zip(data, archive):
    """Closed ZIP32 layout: every physical record must be accounted for."""
    if len(data) < 22 or data[-22:-18] != b'PK\x05\x06':
        raise ValueError('ZIP must have one final uncommented EOCD')
    disk, cd_disk, count_disk, count, size, start, comment = struct.unpack_from('<4H2IH', data, len(data)-18)
    if disk or cd_disk or comment or count_disk != count or count == 65535 or start + size != len(data)-22:
        raise ValueError('unsupported ZIP directory layout')
    items = archive.infolist()
    if len(items) != count or archive.start_dir != start:
        raise ValueError('ZIP directory mismatch')
    cursor = start
    ranges = []
    payload_ranges = {}
    for item in items:
        if data[cursor:cursor+4] != b'PK\x01\x02' or cursor + 46 > len(data)-22:
            raise ValueError('invalid ZIP central header')
        central = struct.unpack_from('<6H3I5H2I', data, cursor+4)
        _, version, flags, method, _, _, crc, packed, unpacked, names, extras, comments, disk, _, _, offset = central
        end = cursor + 46 + names + extras + comments
        raw_name = data[cursor+46:cursor+46+names]
        if extras or comments or disk or version >= 45 or flags & ~0x808 or method not in (0, 8):
            raise ValueError('undeclared ZIP extension')
        if end > len(data)-22 or offset != item.header_offset or data[offset:offset+4] != b'PK\x03\x04':
            raise ValueError('ZIP local header offset mismatch')
        if offset + 30 > start:
            raise ValueError('truncated ZIP local header')
        lv, lf, lm, _, _, lc, lp, lu, ln, le = struct.unpack_from('<5H3I2H', data, offset+4)
        begin = offset + 30 + ln + le
        if lv != version or lf != flags or lm != method or le or data[offset+30:offset+30+ln] != raw_name:
            raise ValueError('ZIP local and central headers differ')
        if (lc, lp, lu) != (crc, packed, unpacked) and not (flags & 8 and (lc, lp, lu) == (0, 0, 0)):
            raise ValueError('ZIP local sizes or CRC differ')
        stop = begin + packed
        if stop > start:
            raise ValueError('ZIP data overlaps directory')
        payload_ranges[item.orig_filename] = (begin, stop)
        compressed = data[begin:stop]
        if method == 8:
            decoder = zlib.decompressobj(-15)
            payload = decoder.decompress(compressed)
            if not decoder.eof or decoder.unused_data:
                raise ValueError('unaccounted ZIP compressed bytes')
        else:
            payload = compressed
        if len(payload) != unpacked or zlib.crc32(payload) != crc:
            raise ValueError('ZIP size or CRC mismatch')
        if flags & 8:
            if data[stop:stop+4] == b'PK\x07\x08':
                stop += 4
            if data[stop:stop+12] != struct.pack('<3I', crc, packed, unpacked):
                raise ValueError('ZIP data descriptor mismatch')
            stop += 12
        ranges.append((offset, stop))
        cursor = end
    if cursor != start + size:
        raise ValueError('unaccounted ZIP directory bytes')
    cursor = 0
    for begin, end in sorted(ranges):
        if begin != cursor or end <= begin:
            raise ValueError('ZIP gap or overlap')
        cursor = end
    if cursor != start:
        raise ValueError('unaccounted ZIP bytes')
    return payload_ranges


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


def check_native(data, native):
    """Only called after matching the code table and distribution context."""
    _, _, format, library = native
    target = 'x86_64-unknown-linux-gnu' if format == 'elf' else 'x86_64-pc-windows-msvc'
    role = 'python-extension' if library else 'cli'
    # STOR-LIMIT-004: native build outputs are checked only for format,
    # not scanned individually. The enclosing container excludes only their
    # exact data ranges from its expanded-stream scan (STOR-LIMIT-004).
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
