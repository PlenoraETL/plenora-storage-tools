"""Fail closed when a release guard invocation loses its PR CI counterpart."""
import ast
from functools import lru_cache
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def jobs(name):
    text = (ROOT / '.github/workflows' / name).read_text(encoding='utf-8')
    return dict(re.findall(r'^  ([\w-]+):\n(.*?)(?=^  [\w-]+:|\Z)',
                           text.split('jobs:\n', 1)[1], re.M | re.S))


@lru_cache(maxsize=1)
def guard_scripts():
    # Discover wrappers from imports and literal subprocess script paths. A new
    # wrapper is included without extending a hand-maintained name regex.
    dependencies = {}
    for path in (ROOT / 'scripts').glob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split('.')[0])
            elif isinstance(node, ast.Import):
                names.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if re.fullmatch(r'(?:scripts/)?[a-z_][a-z0-9_]*\.py', node.value):
                    names.add(Path(node.value).stem)
        dependencies[path.stem] = names
    guards = {'check_packages'}
    while True:
        expanded = guards | {name for name, deps in dependencies.items() if deps & guards}
        if expanded == guards:
            return guards
        guards = expanded


def invocations(job):
    found = re.findall(r'scripts/([a-z_][a-z0-9_]*)\.py(?:[ \t]+([\w-]+))?', job)
    return [(script, arg if script == 'release_publication' else '') for script, arg in found
            if script in guard_scripts() and
            (script != 'release_publication' or arg not in {'draft', 'smoke', 'smoke-cli'})]


def check(candidate=None, release=None, ci=None):
    candidate = jobs('release-candidate.yml') if candidate is None else candidate
    release = jobs('release.yml') if release is None else release
    ci = jobs('ci.yml') if ci is None else ci
    expected = {
        ('release-candidate.yml', 'python-compatibility'): [('check_installed_sdk', '')],
        ('release-candidate.yml', 'linux'): [('build_release', ''), ('qualify_target', '')],
        ('release-candidate.yml', 'windows'): [('build_release', ''), ('qualify_windows_candidate', '')],
        ('release.yml', 'qualify'): [('release_publication', 'prepare'), ('release_publication', 'upload')],
        ('release.yml', 'publish'): [('release_publication', 'publish')],
    }
    actual = {(workflow, name): invocations(job)
              for workflow, collection in [('release-candidate.yml', candidate), ('release.yml', release)]
              for name, job in collection.items() if invocations(job)}
    if actual != expected:
        raise ValueError('release guard invocation changed; extend the PR regression mapping')
    for collection in (candidate, ci):
        sdk = collection['python-compatibility']
        if (invocations(sdk) != [('check_installed_sdk', '')]
                or 'os: [ubuntu-24.04, windows-latest]' not in sdk
                or 'python-version-file: .python-version' not in sdk
                or 'downloaded-wheel --coverage --typing' not in sdk):
            raise ValueError('installed wheel guard missing its two-platform CI counterpart')
    pairs = [('linux', 'docker-conformance'), ('windows', 'native-windows')]
    for source, destination in pairs:
        runner = re.search(r'^    runs-on: (.+)$', candidate[source], re.M).group(1)
        if '    runs-on: ' + runner + '\n' not in ci[destination]:
            raise ValueError('candidate and CI guard operating systems differ')
        for invocation in invocations(candidate[source]):
            if invocation not in invocations(ci[destination]):
                raise ValueError('candidate guard invocation missing from PR CI')
        if source == 'linux':
            for job in (candidate[source], ci[destination]):
                if 'docker compose -f docker-compose.yml -f compose.extended.yml run --rm --no-deps storage-rust bash -c' not in job:
                    raise ValueError('Linux candidate guard must run in the same Bookworm service')
        if 'name: guard-candidate-' + source not in ci[destination] or 'path: dist/' not in ci[destination]:
            raise ValueError('complete candidate outputs must reach the recheck runner')
    recheck = ci['release-package-recheck']
    for token in ('runs-on: ubuntu-24.04', 'pattern: guard-candidate-*', 'merge-multiple: true',
                  'needs: [release-package-build, docker-conformance, native-windows]',
                  'python scripts/check_release_packages.py "target/candidate-recheck/$version" --release'):
        if token not in recheck:
            raise ValueError('publication guard regression missing its complete built inputs')
    for name in ('qualify', 'publish'):
        if 'runs-on: ubuntu-24.04' not in release[name]:
            raise ValueError('publication and PR guard operating systems differ')
    # Publication and PR invoke the same verifier; do not silently bypass it or
    # add a new direct classifier call outside the coverage mapping.
    tree = ast.parse((ROOT / 'scripts/release_publication.py').read_text(encoding='utf-8'))
    for name in ('prepare', 'upload', 'publish'):
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        calls = [n.func.id for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        if calls.count('verify_publication') != 1 or 'verify_distribution' in calls:
            raise ValueError('publication guard must use the CI-exercised verification script')
    regression = (ROOT / 'scripts/check_release_packages.py').read_text(encoding='utf-8')
    if "with_name('verify_release.py')" not in regression or 'verify_publication([*public, bundle, receipt])' not in regression:
        raise ValueError('PR regression no longer exercises target and publication entry points')
    for (workflow, name), calls in actual.items():
        print(f'{workflow}:{name}: {calls} -> PR CI covered')


if __name__ == '__main__':
    check()
