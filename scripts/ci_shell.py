"""Run selected workflow shells with inert, observed external-tool adapters.

The shell is real. Cargo, Docker, package installation and release scripts are
not executed. These receipts prove dispatch/argv only, not tool compatibility.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import strict_json
from ci_plan import job_plan, digest

# Different subjects of the same entrypoint are different executable contracts.
# Keep the complete-release argv independent from the archive-only obligation.
RECHECK_COMMANDS = {
    'verify-crate-packages': [['scripts/check_release_packages.py', 'target/release-package-regression']],
    'verify-release-packages': [['scripts/release_version.py'], ['scripts/check_release_packages.py',
                                'target/candidate-recheck/4.0.0', '--release']],
}


def recheck_steps(job, invocations):
    steps = [step for step in job['steps']
             if ('check_release_packages', '') in invocations(step.get('run', ''))]
    identities = [step.get('id') for step in steps]
    if len(identities) != len(set(identities)) or set(identities) != set(RECHECK_COMMANDS):
        raise ValueError('recheck executor identities missing, duplicated or unknown')
    return {step['id']: step for step in steps}

PROBE = '''import json,os,sys
args=sys.argv[1:]
if args[0] in ('python', 'python3'):
    args=args[1:]
    if args[0] == 'scripts/run_logged.py': args=args[3:]
with open(os.environ['CI_TRACE'],'a',encoding='utf-8') as out:
    out.write(json.dumps(args)+'\\n')
if args == ['scripts/release_version.py']: print('4.0.0')
'''


def observe(body, windows):
    with tempfile.TemporaryDirectory(prefix='ci-shell-') as temp:
        root = Path(temp)
        probe, trace = root / 'probe.py', root / 'trace.jsonl'
        probe.write_text(PROBE, encoding='utf-8')
        env = dict(os.environ, CI_PYTHON=sys.executable, CI_PROBE=str(probe), CI_TRACE=str(trace), PATH=str(root))
        for startup in ('BASH_ENV', 'ENV'):
            env.pop(startup, None)
        if windows:
            shell = shutil.which('pwsh') or shutil.which('powershell')
            prefix = 'function python { & $env:CI_PYTHON $env:CI_PROBE python @args; $global:LASTEXITCODE = $LASTEXITCODE }\n'
            command = [shell, '-NoProfile', '-NonInteractive', '-Command', prefix + body]
        else:
            shell = shutil.which('bash')
            env.update(CI_BASH=shell or '', MSYS2_ARG_CONV_EXCL='*', MSYS_NO_PATHCONV='1')
            prefix = '''probe() { "$CI_PYTHON" "$CI_PROBE" "$@"; }
python() { probe python "$@"; }
python3() { probe python3 "$@"; }
cargo() { probe cargo "$@"; }
cp() { probe cp "$@"; }
update-ca-certificates() { probe update-ca-certificates "$@"; }
docker() { probe docker "${@:1:$#-1}"; "$CI_BASH" --noprofile --norc -euo pipefail -c "${!#}"; }
export -f probe python python3 cargo cp update-ca-certificates docker
'''
            script = root / 'step.sh'
            script.write_text(prefix + body, encoding='utf-8', newline='\n')
            command = [shell, '--noprofile', '--norc', '-euo', 'pipefail', str(script)]
        if not shell:
            raise ValueError('required contract shell unavailable')
        result = subprocess.run(command, cwd=root, env=env, capture_output=True, timeout=20)
        if result.returncode:
            raise ValueError('workflow shell contract failed')
        return [strict_json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []


def verify_commands(records, expected):
    if records != expected:
        raise ValueError('workflow executor did not reach the required argv')


def contracts(graph, invocations, *, windows=None):
    windows = sys.platform == 'win32' if windows is None else windows
    rows = []
    for workflow, name in [('ci.yml', 'python-compatibility'), ('release-candidate.yml', 'python-compatibility'),
                           ('ci.yml', 'native-windows' if windows else 'docker-conformance'),
                           ('release-candidate.yml', 'windows' if windows else 'linux'),
                           *([] if windows else [('ci.yml', 'release-package-recheck')])]:
        job = job_plan(graph, workflow, name)
        if name == 'release-package-recheck':
            recheck_steps(job, invocations)
        for number, step in enumerate(job['steps']):
            calls = invocations(step.get('run', ''))
            if name == 'python-compatibility':
                selected = ('check_installed_sdk', '') in calls
                expected = [['scripts/check_installed_sdk.py', 'downloaded-wheel', '--coverage', '--typing',
                             '--output', 'target/sdk-compatibility']]
            elif windows:
                building = ('build_release', '') in calls
                selected = building or ('qualify_windows_candidate', '') in calls
                expected = ([['scripts/build_release.py']] if building else
                            [['scripts/release_version.py'], ['scripts/qualify_windows_candidate.py', 'dist/4.0.0']])
            elif name == 'release-package-recheck':
                selected = ('check_release_packages', '') in calls
                expected = RECHECK_COMMANDS.get(step.get('id'))
            else:
                selected = ('qualify_target', '') in calls
                expected = [['docker', 'compose', '-f', 'docker-compose.yml', '-f', 'compose.extended.yml',
                             'run', '--rm', '--no-deps', 'storage-rust', 'bash', '-c'],
                            ['scripts/release_version.py'], ['cargo', 'fetch', '--locked'],
                            ['scripts/build_release.py'],
                            ['cp', '.fixtures/ca.crt', '/usr/local/share/ca-certificates/plenora-storage-fixture.crt'],
                            ['update-ca-certificates'],
                            ['scripts/qualify_target.py', 'dist/4.0.0/x86_64-unknown-linux-gnu']]
            if selected:
                try:
                    observed = observe(step['run'], windows)
                    verify_commands(observed, expected)
                except ValueError as error:
                    raise ValueError(f'workflow shell contract failed ({workflow}, {name}, step {number})') from error
                rows.append({'workflow': workflow, 'job': name, 'step': number, 'definition': digest(step),
                             'executor': step.get('id'),
                             'status': 'PASS', 'scope': 'shell-argv-probe', 'observed': observed})
    if len(rows) != 6:
        raise ValueError('shell executor contract inventory differs')
    return {'platform': sys.platform, 'shell': 'powershell' if windows else 'bash', 'rows': rows}
