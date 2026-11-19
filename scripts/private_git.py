"""Git subprocess policy: private creation and no opportunistic index refresh."""
import os
import subprocess


def options(kwargs):
    kwargs = dict(kwargs)
    kwargs['env'] = dict(os.environ if kwargs.get('env') is None else kwargs['env'], GIT_OPTIONAL_LOCKS='0')
    if os.name == 'posix':
        # Child-only umask: never change the multithreaded parent's process mask.
        kwargs['umask'] = 0o077
    return kwargs


def run(*args, **kwargs):
    return subprocess.run(*args, **options(kwargs))


def check_output(*args, **kwargs):
    return subprocess.check_output(*args, **options(kwargs))
