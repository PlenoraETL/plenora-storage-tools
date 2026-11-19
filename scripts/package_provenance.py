"""Git source provenance; the injected port supplies repository identity and I/O."""
import os
from campaign_model import creation_profile
import re
import tempfile
from pathlib import Path
from contextlib import contextmanager


def git_env(api):
    """Environment for the guard's own git calls.

    Git 2.39 honours safe.directory for a local clone only from protected
    (system/global) configuration, not from -c: the guard points
    GIT_CONFIG_GLOBAL at a private file that trusts the checkout, its .git
    and the guard's own temporary clones under target/, and nothing else.
    """
    target = api.ROOT / 'target'
    target.mkdir(exist_ok=True)
    trusted = [api.ROOT.resolve(), api.ROOT.resolve() / '.git']
    for clone in sorted(target.glob('package-reference-*')):
        trusted += [clone.resolve() / 'work', clone.resolve() / 'work' / '.git']
    config = target / 'package-guard-gitconfig'
    lines = ['[safe]'] + ['\tdirectory = ' + path.as_posix() for path in trusted]
    config.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return {**os.environ, 'GIT_CONFIG_GLOBAL': str(config), 'GIT_OPTIONAL_LOCKS': '0'}


def git_command(api, *args, work=None):
    """Trust only the source checkout and, when used, its isolated clone."""
    # A local clone checks ownership of the source's .git directory, not of
    # the working tree: trust both paths, for the checkout and its clone.
    command = ['git']
    for base in (api.ROOT, work) if work is not None else (api.ROOT,):
        for path in (base.resolve(), base.resolve() / '.git'):
            command += ['-c', 'safe.directory=' + path.as_posix()]
    return [*command, *args]


def _tracked_paths(api):
    names = api.subprocess.run(env=api.git_env(), args=api.git_command('ls-files', '--cached', '--with-tree=' + api.release_revision(), '-z'),
                           cwd=api.ROOT, capture_output=True, check=True).stdout.decode('utf-8').split('\0')
    names = [n for n in names if n]
    api.inventory(names)
    return {api.normalize_path(n): n for n in names}


def _source_bytes(api, name):
    result = api.subprocess.run(env=api.git_env(), args=api.git_command('show', api.release_revision() + ':' + name), cwd=api.ROOT,
                            capture_output=True)
    if result.returncode:
        raise ValueError('binary source is not tracked in HEAD')
    return result.stdout


def check_git_version(api):
    output = api.subprocess.check_output(env=api.git_env(), args=api.git_command('--version'), cwd=api.ROOT).decode('ascii')
    match = re.match(r'git version (\d+)\.(\d+)', output)
    if not match or tuple(map(int, match.groups())) < (2, 39):
        raise ValueError('package guard requires Git >= 2.39')


@contextmanager
def release_checkout(api):
    """Isolated clone/worktree with release attributes and repository EOL config."""
    api.check_git_version()
    (api.ROOT / 'target').mkdir(exist_ok=True)
    with creation_profile('private_file'), tempfile.TemporaryDirectory(dir=api.ROOT / 'target', prefix='package-reference-') as tmp:
        directory = Path(tmp)
        work = directory / 'work'
        result = api.subprocess.run(env=api.git_env(), args=api.git_command('clone', '--shared', '--no-checkout', '--no-reject-shallow',
                                            '--quiet', str(api.ROOT.resolve()), str(work), work=work),
                                capture_output=True)
        if result.returncode:
            raise ValueError('cannot create isolated release checkout: ' +
                             result.stderr.decode('utf-8', errors='replace').strip())
        for key in ('core.autocrlf', 'core.eol'):
            value = api.subprocess.run(env=api.git_env(), args=api.git_command('config', '--get', key), cwd=api.ROOT, capture_output=True)
            if value.returncode == 0:
                api.subprocess.run(env=api.git_env(), args=api.git_command('config', key, value.stdout.decode().strip(), work=work),
                               cwd=work, check=True, capture_output=True)
        result = api.subprocess.run(env=api.git_env(), args=api.git_command('checkout', '--quiet', '--detach', api.release_revision(), work=work),
                                cwd=work, capture_output=True)
        if result.returncode:
            raise ValueError('cannot produce git checkout source bytes: ' +
                             result.stderr.decode('utf-8', errors='replace').strip())
        yield work


def _checkout_sources(api):
    with api.release_checkout() as work:
        return {name: (work / name).read_bytes() for name in api.tracked_paths().values()}
