"""Preserve prerequisite policy using the single workflow reader and graph."""
from copy import deepcopy
from pathlib import Path
import re
import sys
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci_plan import workflows, compile_plan, job_plan
from ci_policy import actions, runs

ROOT = Path(__file__).resolve().parents[2]


def jobs(name):
    documents = workflows(ROOT)
    graph = compile_plan(documents)
    return {name_: job_plan(graph, name, name_) for name_, value in documents[name]['jobs'].items()
            if 'steps' in value}


def prerequisites(job):
    steps = job['steps']
    def position(predicate):
        return next((i for i, step in enumerate(steps) if 'if' not in step and predicate(step)), None)
    rust = position(lambda s: re.fullmatch(r'dtolnay/rust-toolchain@[0-9a-f]{40}', s.get('uses', '')))
    fetch = position(lambda s: s.get('run', '').strip() == 'cargo fetch --locked')
    checkout = position(lambda s: re.fullmatch(r'actions/checkout@[0-9a-f]{40}', s.get('uses', '')))
    if any(value is None for value in (rust, fetch, checkout)):
        raise ValueError('missing explicit Git/Rust/fetch prerequisite')
    if not checkout < rust < fetch:
        raise ValueError('prerequisites out of order')
    guard = position(lambda s: re.search(r'python(?:3)? scripts/(?:release_publication|check_release_packages|verify_release|qualify_release|qualify_windows_candidate|build_release)\.py', s.get('run', '')))
    if guard is not None and fetch > guard:
        raise ValueError('fetch must precede release verification')
    return steps[rust]['uses'], steps[rust]['with']['toolchain']


class ReleaseWorkflowTests(unittest.TestCase):
    def test_every_publication_guard_job_has_prerequisites(self):
        guard = (r'scripts/(?:verify_release|qualify_release|check_packages)\.py|'
                 r'scripts/release_publication\.py\s+(?:prepare|upload|publish|bundle)\b')
        checked = set()
        for name, job in jobs('release.yml').items():
            if any(re.search(guard, command) for command in runs(job)):
                with self.subTest(job=name):
                    prerequisites(job)
                checked.add(name)
        self.assertTrue({'qualify', 'publish'} <= checked)

    def test_release_and_clean_runner_use_the_ci_toolchain(self):
        ci, release = jobs('ci.yml'), jobs('release.yml')
        reference = prerequisites(ci['product-quality'])
        channel = tomllib.loads((ROOT / 'rust-toolchain.toml').read_text())['toolchain']['channel']
        self.assertEqual(reference[1], channel)
        for job in (release['qualify'], release['publish'], ci['native-windows'],
                    ci['release-package-build'], ci['release-package-recheck'], ci['release-candidate-recheck'],
                    jobs('release-candidate.yml')['windows']):
            self.assertEqual(prerequisites(job), reference)
        for name in ('qualify', 'publish'):
            checkout = actions(release[name], 'actions/checkout')[0]['with']
            self.assertEqual(checkout['ref'], '${{ inputs.tag }}')
            self.assertEqual(checkout['fetch-depth'], 0)
            self.assertEqual(release[name]['runs-on'], 'ubuntu-24.04')
        self.assertEqual(ci['release-candidate-recheck']['runs-on'], 'ubuntu-24.04')
        setup = actions(ci['release-candidate-recheck'], 'actions/setup-python')[0]
        self.assertEqual(setup['with']['python-version-file'], '.python-version')
        self.assertEqual(actions(release['qualify'], 'actions/setup-python')[0], setup)
        for job in (release['qualify'], ci['release-candidate-recheck']):
            self.assertIn('python -m pip install -r scripts/requirements-quality.txt', runs(job))
        self.assertEqual(ci['release-candidate-recheck']['needs'], ['release-package-build', 'docker-conformance', 'native-windows'])
        self.assertFalse(actions(ci['release-candidate-recheck'], 'actions/cache'))

    def test_missing_or_late_prerequisites_are_rejected(self):
        original = jobs('release.yml')['qualify']
        for mutation in ('rust', 'fetch', 'checkout', 'order'):
            job = deepcopy(original)
            if mutation == 'rust':
                job['steps'] = [s for s in job['steps'] if not s.get('uses', '').startswith('dtolnay/')]
            elif mutation == 'checkout':
                actions(job, 'actions/checkout')[0]['uses'] = 'other/checkout@' + 'a' * 40
            else:
                fetch = next(s for s in job['steps'] if s.get('run') == 'cargo fetch --locked')
                job['steps'].remove(fetch)
                if mutation == 'order':
                    job['steps'].append(fetch)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                prerequisites(job)

    def test_external_actions_are_pinned(self):
        graph = compile_plan(workflows(ROOT))
        for node in graph['nodes'].values():
            action = node['definition'].get('uses')
            if action and not action.startswith('./'):
                with self.subTest(action=action):
                    self.assertRegex(action, r'^[\w./-]+@[0-9a-f]{40}$')

    def test_automatic_consumers_have_independent_producers(self):
        ci = jobs('ci.yml')
        self.assertEqual(ci['release-package-recheck']['needs'], ['release-package-build'])
        self.assertEqual(ci['python-compatibility']['needs'], ['product-quality', 'python-wheel-windows'])
        wheel = actions(ci['python-wheel-windows'], 'actions/upload-artifact')[0]['with']
        self.assertEqual(wheel['name'], 'python-wheel-windows')
        self.assertEqual(wheel['path'], 'target/python-wheels/*.whl')
        self.assertIn('python scripts/build_python.py --debug', runs(ci['python-wheel-windows']))
        download = actions(ci['python-compatibility'], 'actions/download-artifact')[0]['with']
        self.assertEqual(download['name'], "${{ runner.os == 'Windows' && 'python-wheel-windows' || 'python-wheel-linux' }}")
