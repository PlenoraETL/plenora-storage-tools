"""Fail-closed release tiers; eligibility is recomputed from committed Git objects."""
import argparse
import json
import strict_json
from pathlib import Path
import re
import subprocess
import tomllib

from versioning import parse_version

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'PlenoraETL/plenora-storage-tools'
OMITTED = {name: 'dependency-patch: maintainer decision 2026-11-04; performance regressions unmeasured'
           for name in ('performance-ab', 'performance-compare', 'soak')}
FUNCTIONAL = ['qualify-windows', 'qualify-linux', 'install-sdk', 'transfers-large',
              'transfers-workers4', 'transfers-workers16', 'spooled-large', 'spooled-workers4', 'spooled-workers16']
DOCS = {'CHANGELOG.md', 'docs/STATO.md', 'docs/release.md', 'docs/release-campaign.md',
        'docs/release-evidence-bundle.md'}


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root).decode('utf-8').strip()


def without_versions(value, old, new, context=()):
    if isinstance(value, dict):
        own_version = context in (('workspace', 'package'), ('package',), ('project',))
        local_dependency = 'path' in value and any(part.endswith('dependencies') for part in context)
        return {key: ('<release-version>' if key == 'version' and (own_version or local_dependency)
                      and item in (old, new) else without_versions(item, old, new, (*context, key)))
                for key, item in value.items()}
    if isinstance(value, list):
        return [without_versions(item, old, new, context) for item in value]
    return value


def verify_diff(revision, previous='v3.0.2', root=ROOT):
    candidate = git(root, 'rev-parse', revision + '^{commit}')
    base = git(root, 'rev-parse', previous + '^{commit}')
    subprocess.run(['git', 'merge-base', '--is-ancestor', base, candidate], cwd=root, check=True)
    read = lambda rev, name: git(root, 'show', rev + ':' + name)
    old = tomllib.loads(read(base, 'Cargo.toml'))['workspace']['package']['version']
    new = tomllib.loads(read(candidate, 'Cargo.toml'))['workspace']['package']['version']
    a, b = parse_version(old), parse_version(new)
    if previous != 'v' + old or a.stage or b.stage or b.core != (*a.core[:2], a.core[2] + 1):
        raise ValueError('dependency-patch requires the next stable patch of the previous release tag')
    changes = git(root, 'diff', '--no-renames', '--name-status', base, candidate).splitlines()
    names = []
    for change in changes:
        status, name = change.split('\t')
        names.append(name)
        if status != 'M' and not (status == 'A' and name == f'docs/release-notes-{new}.md'):
            raise ValueError('dependency-patch refuses added, deleted or renamed files: ' + name)
        modes = git(root, 'diff', '--raw', '--no-abbrev', base, candidate, '--', name).split()
        if modes[0] != ':100644' or modes[1] != '100644':
            if not (status == 'A' and modes[0] == ':000000' and modes[1] == '100644'):
                raise ValueError('dependency-patch refuses file mode changes')
        if name in ('Cargo.lock', 'fuzz/Cargo.lock') or name in DOCS or name == f'docs/release-notes-{new}.md':
            if name == 'docs/STATO.md':
                if read(base, name).replace(old, '<version>') != read(candidate, name).replace(new, '<version>'):
                    raise ValueError('generated inventory changed beyond release version')
            continue
        if Path(name).name in ('Cargo.toml', 'pyproject.toml'):
            before, after = tomllib.loads(read(base, name)), tomllib.loads(read(candidate, name))
            if without_versions(before, old, new) == without_versions(after, old, new):
                continue
        raise ValueError('dependency-patch refuses non-dependency change: ' + name)
    if not set(names) & {'Cargo.lock', 'fuzz/Cargo.lock'}:
        raise ValueError('dependency-patch requires a dependency lock change')
    return dict(previous_tag=previous, previous_revision=base, source_revision=candidate,
                version=new, changed_files=sorted(names))


def workflow(run_id, revision, filename):
    if not re.fullmatch(r'[0-9]+', str(run_id)):
        raise ValueError('invalid workflow run ID')
    def api(path):
        return strict_json.loads(subprocess.check_output(['gh', 'api', f'repos/{REPOSITORY}/actions/' + path], text=True))
    run = api(f'runs/{run_id}')
    if (run['head_sha'] != revision or run['status'] != 'completed' or run['conclusion'] != 'success'
            or run['path'] != '.github/workflows/' + filename
            or run['repository']['full_name'] != REPOSITORY):
        raise ValueError('required workflow identity or successful conclusion differs')
    jobs, page = [], 1
    while True:
        batch = api(f'runs/{run_id}/attempts/{run["run_attempt"]}/jobs?per_page=100&page={page}')['jobs']
        jobs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    if not jobs or any(job['conclusion'] != 'success' for job in jobs):
        raise ValueError('required workflow contains skipped or failed jobs')
    required = {'docker-conformance', 'native-windows'} if filename == 'ci.yml' else {'linux', 'windows'}
    if not required <= {job['name'] for job in jobs}:
        raise ValueError('required workflow lacks Linux and Windows jobs')
    return {'run_id': str(run_id), 'run_attempt': run['run_attempt']}


def declaration(revision, ci_run, candidate_run, previous='v3.0.2'):
    return {'profile': 'dependency-patch', 'eligibility': verify_diff(revision, previous),
            'executed': FUNCTIONAL, 'omitted': OMITTED,
            'workflows': {'ci.yml': workflow(ci_run, revision, 'ci.yml'),
                          'release-candidate.yml': workflow(candidate_run, revision, 'release-candidate.yml')}}


def validate(report, revision):
    if report.get('profile') != 'dependency-patch' or report.get('executed') != FUNCTIONAL or report.get('omitted') != OMITTED:
        raise ValueError('invalid qualification profile or phase inventory')
    eligibility = report['eligibility']
    if eligibility != verify_diff(revision, eligibility['previous_tag']):
        raise ValueError('dependency-patch diff proof differs')
    if set(report['workflows']) != {'ci.yml', 'release-candidate.yml'}:
        raise ValueError('missing required workflow')
    for filename, recorded in report['workflows'].items():
        if recorded != workflow(recorded['run_id'], revision, filename):
            raise ValueError('workflow attempt changed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', default='HEAD')
    parser.add_argument('--previous-tag', default='v3.0.2')
    args = parser.parse_args()
    print(json.dumps(verify_diff(args.revision, args.previous_tag), indent=2))
