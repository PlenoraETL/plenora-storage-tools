"""Keep the clean-runner regression coupled to publication prerequisites."""
from pathlib import Path
import re
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[2]


def jobs(name):
    text = (ROOT / '.github/workflows' / name).read_text()
    return dict(re.findall(r'^  ([\w-]+):\n(.*?)(?=^  [\w-]+:|\Z)',
                           text.split('jobs:\n', 1)[1], re.M | re.S))


def prerequisites(job):
    # Deliberately require unconditional standalone steps, before any guard.
    rust = re.search(r'^      - uses: (dtolnay/rust-toolchain@[0-9a-f]{40})\n'
                     r'        with:\n          toolchain: \'([^\']+)\'\n', job, re.M)
    fetch = re.search(r'^      - run: cargo fetch --locked\n', job, re.M)
    checkout = re.search(r'^      - uses: actions/checkout@[0-9a-f]{40}', job, re.M)
    if not rust or not fetch or not checkout:
        raise ValueError('missing explicit Git/Rust/fetch prerequisite')
    if not checkout.start() < rust.start() < fetch.start():
        raise ValueError('prerequisites out of order')
    guard = re.search(r'python scripts/(?:release_publication|check_release_packages|verify_release|qualify_release)\.py', job)
    if guard and fetch.end() > guard.start():
        raise ValueError('fetch must precede release verification')
    return rust.groups()


class ReleaseWorkflowTests(unittest.TestCase):
    def test_every_publication_guard_job_has_prerequisites(self):
        guard = (r'scripts/(?:verify_release|qualify_release|check_packages)\.py|'
                 r'scripts/release_publication\.py\s+(?:prepare|upload|publish|bundle)\b')
        checked = set()
        for name, job in jobs('release.yml').items():
            if re.search(guard, job):
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
                    ci['release-package-build'], ci['release-package-recheck'],
                    jobs('release-candidate.yml')['windows']):
            self.assertEqual(prerequisites(job), reference)
        for name in ('qualify', 'publish'):
            self.assertIn('ref: ${{ inputs.tag }}', release[name])
            self.assertIn('fetch-depth: 0', release[name])
            self.assertIn('runs-on: ubuntu-24.04', release[name])
        self.assertIn('runs-on: ubuntu-24.04', ci['release-package-recheck'])
        self.assertIn("python-version: '3.11'", ci['release-package-recheck'])
        python_setup = r'uses: (actions/setup-python@[0-9a-f]{40})[^\n]*\n        with:\n          python-version: (.+)'
        self.assertEqual(re.search(python_setup, release['qualify']).groups(),
                         re.search(python_setup, ci['release-package-recheck']).groups())
        for job in (release['qualify'], ci['release-package-recheck']):
            self.assertIn('python -m pip install -r scripts/requirements-quality.txt', job)
        self.assertIn('needs: release-package-build', ci['release-package-recheck'])
        self.assertNotIn('actions/cache@', ci['release-package-recheck'])

    def test_missing_or_late_prerequisites_are_rejected(self):
        job = jobs('release.yml')['qualify']
        mutations = [re.sub(r'^      - uses: dtolnay/.*?\n', '', job, flags=re.M),
                     job.replace('      - run: cargo fetch --locked\n', ''),
                     job.replace('      - uses: actions/checkout@', '      - uses: other/checkout@'),
                     job.replace('      - run: cargo fetch --locked\n', '')
                     + '      - run: cargo fetch --locked\n']
        for mutated in mutations:
            with self.subTest(job=mutated), self.assertRaises(ValueError):
                prerequisites(mutated)

    def test_external_actions_are_pinned(self):
        for path in (ROOT / '.github/workflows').glob('*.yml'):
            for action in re.findall(r'\buses: ([^\s#]+)', path.read_text()):
                if not action.startswith('./'):
                    with self.subTest(workflow=path.name, action=action):
                        self.assertRegex(action, r'^[\w./-]+@[0-9a-f]{40}$')
