"""Qualify exact Linux distributions with resumable, separately recorded attempts."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

from campaign_fence import held
from campaign_state import Campaign, digest, exclusive, logged, write_json
from check_disk_space import GIB, inspect
from check_memory import available as available_memory, inspect as inspect_memory
from soak_policy import SOAK_DURATION_SECONDS
from performance_order import SCHEME
from versioning import parse_version

ROOT = Path(__file__).resolve().parents[1]
TARGET = 'x86_64-unknown-linux-gnu'
PERFORMANCE_ORDER = SCHEME
# A multiple of four, so ABBA gives every provider the same number of first
# runs as baseline and as candidate.
PERFORMANCE_ROUNDS = 32
SPOOLED_PHASES = ('spooled-large', 'spooled-workers4', 'spooled-workers16')
PHASES = ('qualify-linux', 'install-sdk', 'performance-ab', 'performance-compare', 'transfers-large',
          'transfers-workers4', 'transfers-workers16', *SPOOLED_PHASES, 'soak')


def phases_for(version):
    """The phases this runner executes for `version`: spooled transfers exist from 2.1.0."""
    spooled = parse_version(version).requires((2, 1, 0))
    return tuple(name for name in PHASES if spooled or name not in SPOOLED_PHASES)



def identity(folder, baseline):
    manifest = json.loads((folder / 'release-manifest.json').read_text())
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    if (manifest['target'] != TARGET or manifest['source_revision'] != revision or not manifest['source_committed']
            or subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip()):
        raise ValueError('campaign requires matching committed Linux artifacts and clean source')
    subjects = {row['name']: digest(folder / row['name']) for row in manifest['artifacts']}
    if any(subjects[row['name']] != row['sha256'] for row in manifest['artifacts']):
        raise ValueError('campaign distribution bytes differ from manifest')
    return {'source_revision': revision, 'version': manifest['version'], 'artifacts': subjects,
            'baseline_binary_sha256': digest(baseline), 'soak_seconds': SOAK_DURATION_SECONDS,
            'performance_rounds': PERFORMANCE_ROUNDS, 'performance_order': PERFORMANCE_ORDER,
            'large_transfer_rounds': 2}


def check_fixture_state(output, nonce):
    """The fixtures were last prepared by this attempt's reset, and by nothing after it.

    Every preparation records its nonce in fixture-state.json under the
    campaign lock, which this runner holds from here on: a different nonce,
    or a preparation that was not a reset, means the fixtures changed after
    the reset of this attempt.
    """
    try:
        state = json.loads((output / 'fixture-state.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        state = None
    if state != {'nonce': nonce, 'kind': 'reset'}:
        raise ValueError('fixtures were not reset for this attempt, or were prepared again after the reset; '
                         'nothing was measured')


def check_inputs(inputs, expected, baseline):
    """Every file of this attempt's input directory, and nothing else, has the
    digest the controller expects; so has the baseline binary as mounted.

    The directory belongs to the epoch of this runner's admission; a file of
    another controller in it, or a changed byte, stops the run before its
    result can be recorded.
    """
    inputs = Path(inputs)
    found = {}
    for path in sorted(inputs.rglob('*')):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError('campaign inputs contain an entry that is not a regular file; nothing is recorded')
        if path.is_file():
            found[path.relative_to(inputs).as_posix()] = digest(path)
    if found != expected or digest(baseline) != expected.get('baseline/plenora-storage'):
        raise ValueError('campaign inputs differ from the ones the controller uploaded; nothing is recorded')


def input_fence(epoch_fence, inputs, expected, baseline):
    """The runner's fence: the admission epoch, then every input digest."""
    def fence():
        epoch_fence()
        check_inputs(inputs, expected, baseline)
    return fence


def fenced_phase(campaign, fence, name, action, *, retry=False, reason=None):
    """Run one phase only while this runner's admission holds the VM.

    The epoch is checked before the phase starts and again when its action
    ends, before the phase can be recorded as passed: a phase that ran while
    another controller was admitted is recorded as failed and raises
    CampaignFenced, never kept as evidence.
    """
    fence()

    def guarded(path):
        action(path)
        fence()
    return campaign.phase(name, guarded, retry=retry, reason=reason)


def run(folder, baseline, output, retries, reason, backend_data, fixture_nonce, campaign_dir, epoch, inputs,
        expected):
    # Shared hold of the VM campaign lock for the whole run, then the epoch of
    # this runner's admission (campaign_fence): no controller is admitted while
    # this runner lives, and a runner started after another admission stops.
    # Every check of the epoch also checks every input against its digest, so
    # each phase is measured on exactly the inputs of this attempt, before
    # and after.
    with held(campaign_dir, epoch) as epoch_fence:
        fence = input_fence(epoch_fence, inputs, expected, baseline)
        fence()
        measure(folder, baseline, output, retries, reason, backend_data, fixture_nonce, fence, expected)


def measure(folder, baseline, output, retries, reason, backend_data, fixture_nonce, fence, expected):
    if sys.platform != 'linux':
        raise ValueError('VM campaign requires Linux')
    folder, baseline, output = folder.resolve(), baseline.resolve(), output.resolve()
    subject = identity(folder, baseline)
    spooled = parse_version(subject['version']).requires((2, 1, 0))
    binary = folder / 'plenora-storage'
    wheels = list(folder.glob('*.whl'))
    if len(wheels) != 1:
        raise ValueError('campaign requires exactly one wheel')
    wheel = wheels[0]
    # Later imports must not mutate an already inventoried SDK installation.
    env = dict(os.environ, PLENORA_CLI_BIN=str(binary), PYTHONDONTWRITEBYTECODE='1')
    spaces = {'workspace': ROOT, 'temporary': Path('/tmp'),
              **{f'backend-{index}': path for index, path in enumerate(backend_data)}}
    subject['space_locations'] = {label: str(path.resolve()) for label, path in spaces.items()}
    with exclusive(output):
        check_fixture_state(output, fixture_nonce)
        campaign = Campaign(output, subject)
        campaign.validate_retries(retries, phases_for(subject['version']))

        def phase(name, action):
            return fenced_phase(campaign, fence, name, action, retry=name in retries, reason=reason)

        def command(path, script, *arguments, environment=env, python=sys.executable):
            logged([str(python), str(ROOT / 'scripts' / script), *map(str, arguments)],
                   path, cwd=ROOT, env=environment)

        def transfers(path, *, size, workers, rounds, spool_uploads=False, paired=False):
            space = inspect(spaces, size, workers, spool_uploads=spool_uploads)
            write_json(path / 'disk-space.json', space)
            if space['status'] != 'PASS':
                raise ValueError('insufficient transfer headroom; no transfer started')
            if spool_uploads and size >= GIB:
                # The in-memory GCS fixture holds source and copy at once.
                memory = inspect_memory(available_memory())
                write_json(path / 'memory.json', memory)
                if memory['status'] != 'PASS':
                    raise ValueError('insufficient memory for the GCS fixture peak; no transfer started')
            # A paired run measures baseline and candidate alternately, slot by
            # slot, so a drift of the environment weighs the same on both.
            pair = (['--baseline-binary', baseline, '--baseline-output', path / 'baseline.json',
                     '--output', path / 'candidate.json'] if paired else ['--output', path / 'report.json'])
            command(path, 'qualify_transfers.py', '--bytes', size, '--workers', workers, '--rounds', rounds,
                    *pair, *(['--spool-uploads'] if spool_uploads else []))

        def qualify(path):
            copy = path / 'dist' / subject['version'] / TARGET
            shutil.copytree(folder, copy)
            command(path, 'qualify_target.py', copy)

        qualified = phase('qualify-linux', qualify)

        def sdk(path):
            venv.EnvBuilder(with_pip=True).create(path / 'sdk')
            python = path / 'sdk/bin/python'
            logged([str(python), '-m', 'pip', 'install', '--no-index', str(wheel)], path, cwd=ROOT)
            write_json(path / 'wheel.json', {'sha256': digest(wheel)})

        installed = phase('install-sdk', sdk)
        paired = phase('performance-ab', lambda path: transfers(path, size=1024**2, workers=4,
                                                                rounds=PERFORMANCE_ROUNDS, paired=True))
        old, new = paired / 'baseline.json', paired / 'candidate.json'
        comparison = phase('performance-compare', lambda path: command(path, 'check_performance.py',
                           old, new, '--output', path / 'report.json'))
        large = phase('transfers-large', lambda path: transfers(path, size=GIB, workers=1, rounds=2))
        four = phase('transfers-workers4', lambda path: transfers(path, size=1024**2, workers=4, rounds=2))
        sixteen = phase('transfers-workers16', lambda path: transfers(path, size=1024**2, workers=16, rounds=1))
        prepared = {}
        if spooled:
            for name, size, workers, rounds in [('large', GIB, 1, 2), ('workers4', 1024**2, 4, 2),
                                               ('workers16', 1024**2, 16, 1)]:
                result = phase('spooled-' + name, lambda path: transfers(
                    path, size=size, workers=workers, rounds=rounds, spool_uploads=True))
                prepared[f'transfers-spooled/{name}.json'] = result / 'report.json'
        soak = phase('soak', lambda path: command(path, 'stress_python.py', '--wheel', wheel, '--workers', 4,
                     '--interval-seconds', 30, '--duration-seconds', SOAK_DURATION_SECONDS,
                     *(['--both-upload-modes'] if spooled else []),
                     '--output', path / 'report.json', python=installed / 'sdk/bin/python'))
        selected = {'performance/baseline.json': old,
                    'performance/candidate.json': new, 'performance/report.json': comparison / 'report.json',
                    'transfers/large.json': large / 'report.json', 'transfers/workers4.json': four / 'report.json',
                    'transfers/workers16.json': sixteen / 'report.json', 'soak/report.json': soak / 'report.json', **prepared}
        for name, source in selected.items():
            destination = output / 'selected/gates' / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and digest(destination) != digest(source):
                raise ValueError('selected evidence differs; use a new collection directory')
            if not destination.exists():
                shutil.copyfile(source, destination)
        qualified_target = qualified / 'dist' / subject['version'] / TARGET
        reports = output / 'selected/linux-qualification'
        reports.mkdir(parents=True, exist_ok=True)
        for source in qualified_target.glob('*.json'):
            destination = reports / source.name
            if destination.exists() and digest(destination) != digest(source):
                raise ValueError('selected qualification differs')
            if not destination.exists():
                shutil.copyfile(source, destination)
        fence()
        write_json(output / 'selected/report.json', {'status': 'PASS', 'identity': subject, 'inputs': expected,
                   'files': {name: digest(path) for name, path in selected.items()}})
        print('PASS complete VM campaign; selected evidence is ready for final validation')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('distribution', type=Path)
    parser.add_argument('--baseline-binary', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--backend-data', type=Path, action='append', default=[])
    parser.add_argument('--retry-phase', action='append', default=[])
    parser.add_argument('--retry-reason')
    parser.add_argument('--fixture-nonce', required=True,
                        help='nonce of the fixture reset of this attempt, checked before measuring')
    parser.add_argument('--campaign-dir', required=True, type=Path,
                        help='VM campaign directory with the admission lock and epoch (campaign_fence)')
    parser.add_argument('--epoch', required=True, help='epoch of the admission that started this runner')
    parser.add_argument('--inputs', required=True, type=Path, help='input directory of this attempt')
    parser.add_argument('--expected-inputs', required=True, type=json.loads,
                        help='JSON object: digest of every file of the input directory, by relative path')
    args = parser.parse_args()
    run(args.distribution, args.baseline_binary, args.output, args.retry_phase, args.retry_reason, args.backend_data,
        args.fixture_nonce, args.campaign_dir, args.epoch, args.inputs, args.expected_inputs)
