"""Exact distribution inventories and recursive credential/material checks.

No inventory is learned or updated by this gate. Changes require review of
scripts/package_files.json. Diagnostics never include inspected contents.
This facade composes pure classification, Git provenance and tool execution.
The cross-check between member identity and repository bytes stays at this
boundary; compatibility entrypoints all delegate to the same three owners.
"""
import argparse
import sys
import package_provenance
import package_runner
from package_classifier import (
    normalize_path, inventory, path_lookup, tlv, private_der, kerberos_container,
    check_content, DistributionTarInfo, expand, validate_zip, is_tar, is_text,
    container_format, check_native, TAR_PAX_KEYS, SUPPORTED)
import hashlib
import io
import strict_json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import zipfile
from contextvars import ContextVar
from functools import wraps
from package_generated import GENERATED_MEMBERS, GENERATED_CONTAINERS

from versioning import parse_version, workspace_version

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / 'scripts/package_files.json'
_GIT_CACHE = ContextVar('package_git_snapshot', default=None)


def repository_snapshot(function):
    """Reuse immutable Git inputs only during one verification operation."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        if _GIT_CACHE.get() is not None:
            return function(*args, **kwargs)
        token = _GIT_CACHE.set({})
        try:
            return function(*args, **kwargs)
        finally:
            _GIT_CACHE.reset(token)
    return wrapped


def git_input(key, read):
    cache = _GIT_CACHE.get()
    if cache is None:
        return read()
    key = (str(ROOT), *key)
    if key not in cache:
        cache[key] = read()
    return cache[key]


def git_env():
    return package_provenance.git_env(sys.modules[__name__])


def git_command(*args, work=None):
    return package_provenance.git_command(sys.modules[__name__], *args, work=work)


def release_revision():
    git_input(('git-version',), check_git_version)
    return git_input(('revision',), lambda: subprocess.check_output(
        git_command('rev-parse', 'HEAD'), cwd=ROOT).decode('ascii').strip())


def registry():
    return strict_json.loads(REGISTRY.read_text(encoding='utf-8'))


# Single authority for native members, including every supported wheel tag.
# Values: policy inventory, exact member, format, shared library.
NATIVE_DISTRIBUTIONS = {
    'cli-linux': ('cli-linux', 'plenora-storage', 'elf', False),
    'cli-windows': ('cli-windows', 'plenora-storage.exe', 'pe', False),
    'cp314-abi3-win_amd64': ('wheel-windows', 'plenora_storage/_native.pyd', 'pe', True),
    'cp314-abi3-manylinux_2_34_x86_64': ('wheel-linux', 'plenora_storage/_native.abi3.so', 'elf', True),
    'cp314-abi3-manylinux_2_39_x86_64': ('wheel-linux', 'plenora_storage/_native.abi3.so', 'elf', True),
    'qualification-linux': ('qualification', 'x86_64-unknown-linux-gnu/plenora-storage', 'elf', False),
    'qualification-windows': ('qualification', 'x86_64-pc-windows-msvc/plenora-storage.exe', 'pe', False),
    '.crate': (None, None, None, None),
}


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
        if name in {'license', 'readme.md'} and distribution != 'crates/plenora-smb2' and not (
                name == 'readme.md' and distribution == 'crates/plenora-storage-py'):
            return name
        return normalize_path(distribution + '/' + name)
    if distribution in NATIVE_DISTRIBUTIONS and distribution.startswith('cp'):
        if name.startswith('plenora_storage/'):
            return normalize_path('crates/plenora-storage-py/python/' + name)
        if name.endswith('.dist-info/licenses/license'):
            return 'license'
        return None  # Generated wheel metadata.
    if distribution in {'source', 'sdist', 'cli-linux', 'cli-windows', 'contracts',
                        'qualification', 'public-documents'}:
        return name
    return None


def generated_member(distribution, name, table=GENERATED_MEMBERS):
    """Only exact code-declared templates may differ from repository bytes."""
    distribution = normalize_path(distribution) if distribution else ''
    kind = ('crates' if distribution.startswith('crates/') else
            'wheel' if distribution in NATIVE_DISTRIBUTIONS and distribution.startswith('cp')
            else distribution)
    for template in table.get(kind, ()):
        pattern = re.escape(normalize_path(template))
        for token in ('version', 'python_version'):
            pattern = pattern.replace(re.escape('{' + token + '}'), r'[0-9]+\.[0-9]+\.[0-9]+(?:[a-z0-9.-]+)?')
        if re.fullmatch(pattern, normalize_path(name)):
            return True
    return False


def tracked_paths():
    return git_input(('tracked',), _tracked_paths)


def _tracked_paths():
    return package_provenance._tracked_paths(sys.modules[__name__])


def compare(names, expected):
    actual, wanted = inventory(names), inventory(expected)
    if actual != wanted:
        # Names are useful to reviewers; never print file contents.
        raise ValueError(f'package inventory mismatch: extra={sorted(actual-wanted)}, missing={sorted(wanted-actual)}')


# Closed tar dialect used by Cargo and Python's tar writer. No global PAX,
# GNU longname/longlink/sparse, vendor keys, links or special file types.


def members(data, name=''):
    """Read regular archive members without extraction; reject links/duplicates."""
    # Only a resolved distribution identity can exempt code-table natives.
    context, prefix = '', ''
    if name:
        distribution, prefix, _ = distribution_policy(name)
        identity = normalize_path(name).rsplit('/', 1)[-1]
        context = identity.split('-', 2)[2][:-4] if identity.endswith('.whl') else distribution

    def is_native(member):
        relative = relative_entries([(member, b'')], prefix)[0][0]
        return native_member(context, relative)

    excluded = []
    compression = container_format(data)
    if compression in {'gzip', 'bzip2', 'xz'}:
        data = expand(data, compression, name)
    source = io.BytesIO(data)
    directories = []
    all_names = []
    if container_format(data) == 'zip':
        with zipfile.ZipFile(source) as archive:
            payload_ranges = validate_zip(data, archive)
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
                if is_native(item.orig_filename):
                    excluded.append(payload_ranges[item.orig_filename])
                else:
                    check_content(payload)
                result.append((item.orig_filename, payload))
    else:
        source.seek(0)
        with tarfile.open(fileobj=source, mode='r:', tarinfo=DistributionTarInfo) as archive:
            result = []
            cursor = 0
            for item in archive:
                if item.offset != cursor:
                    raise ValueError('unaccounted tar bytes')
                cursor = archive.offset
                if cursor > len(data) or any(data[item.offset_data+item.size:cursor]):
                    raise ValueError('invalid tar data padding')
                all_names.append(item.name.removesuffix('/') if item.isdir() else item.name)
                if item.isdir():
                    if item.size != 0:
                        raise ValueError('nonempty archive directory')
                    directories.append(item.name.removesuffix('/'))
                    continue
                if item.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
                    raise ValueError('non-file archive member')
                if is_native(item.name):
                    excluded.append((item.offset_data, item.offset_data + item.size))
                result.append((item.name, archive.extractfile(item).read()))
            if len(data) % 512 or len(data) < cursor + 1024 or any(data[cursor:]):
                raise ValueError('invalid tar terminator or trailing bytes')
    # Scan all accounted bytes except exact native payload ranges. Keep headers,
    # descriptors, central directory and tar padding outside the exemption.
    cursor = 0
    for begin, end in sorted(excluded):
        check_content(data[cursor:begin])
        cursor = end
    check_content(data[cursor:])
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


@repository_snapshot
def scan_entries(entries, expected, depth, distribution=''):
    check_build_paths()
    check_build_paths(expected)
    compare([n for n, _ in entries], expected)
    rules = {normalize_path(n): rule for n, rule in expected.items()}
    tracked = tracked_paths()
    for name, data in entries:
        classify_member(data, name, depth, path_lookup(rules, name), distribution, _tracked=tracked, _entries=entries)


def classify_member(data, name='', depth=0, rule=None, distribution='', *, _tracked=None, _entries=None):
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
        recursive = rule.get('container', {}).get('distribution') is True
        if distribution and recursive and not generated_member(distribution, name, GENERATED_CONTAINERS):
            raise ValueError('undeclared generated distribution container')
        generated = generated_member(distribution, name) if name else False
        if generated:
            if rule or not is_text(data) or container_format(data):
                raise ValueError('generated member must be scanned text without source rules')
        elif distribution and not recursive:
            if source is None or source not in tracked:
                raise ValueError('package source is not tracked in HEAD')
            if 'source' in rule and normalize_path(rule['source']) != source:
                raise ValueError('source provenance differs from repository mapping')
            if data != checkout_source_bytes(path_lookup(tracked, source)):
                raise ValueError('source digest differs from git checkout')
        _scan_content(data, name, depth, rule)
        if generated:
            from package_generated import validate_generated
            validate_generated(data, name, distribution, _entries)


def source_bytes(name):
    """Read a tracked source blob, never an arbitrary build artifact."""
    tracked = tracked_paths()
    if normalize_path(name) not in inventory(tracked):
        raise ValueError('binary source is not tracked in HEAD')
    name = path_lookup(tracked, name)
    return git_input(('blob', name), lambda: _source_bytes(name))


def _source_bytes(name):
    return package_provenance._source_bytes(sys.modules[__name__], name)


def check_git_version():
    return package_provenance.check_git_version(sys.modules[__name__])


def release_checkout():
    return package_provenance.release_checkout(sys.modules[__name__])


def _checkout_sources():
    return package_provenance._checkout_sources(sys.modules[__name__])


@repository_snapshot
def checkout_source_bytes(name):
    source_bytes(name)
    name = path_lookup(tracked_paths(), name)
    return git_input(('checkout-tree',), _checkout_sources)[name]


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


@repository_snapshot
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
                data = checkout_source_bytes(rule['source'])
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
    entries = members(data, normalize_path(name).rsplit('/', 1)[-1])
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
        expanded = expand(data, kind)
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
    entries = members(Path(path).read_bytes(), Path(path).name)
    scan_entries(relative_entries(entries, prefix), expected, 1, kind)


def cargo(*args):
    return package_runner.cargo(sys.modules[__name__], *args)


@repository_snapshot
def check_cargo(build=False):
    return package_runner.check_cargo(sys.modules[__name__], build)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', action='store_true')
    args = parser.parse_args()
    check_cargo(args.build)


if __name__ == '__main__':
    main()
