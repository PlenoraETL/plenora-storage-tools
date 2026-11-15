"""Release/PR environment policy over executable workflow definitions."""
from ci_plan import job_plan
import shlex
from ci_shell import recheck_steps


def actions(job, action):
    return [step for step in job['steps'] if step.get('uses', '').split('@')[0] == action]


def runs(job):
    return [step['run'] for step in job['steps'] if 'run' in step]


def check(graph, invocations):
    def job(workflow, name):
        return job_plan(graph, workflow, name)
    def calls(value):
        return [call for command in runs(value) for call in invocations(command)]
    # Platforms and counterpart roles are policy, not a second pipeline.
    counterparts = {'linux': 'docker-conformance', 'windows': 'native-windows',
                    'python-compatibility': 'python-compatibility'}
    release_jobs = {node['context']['job'] for node in graph['nodes'].values()
                    if node['context']['workflow'] == 'release-candidate.yml'
                    and node['kind'] == 'step' and invocations(node['definition'].get('run', ''))}
    if release_jobs != set(counterparts):
        raise ValueError('release guard invocation changed; extend PR counterparts')
    for source, destination in counterparts.items():
        candidate, ci = job('release-candidate.yml', source), job('ci.yml', destination)
        if candidate['runs-on'] != ci['runs-on'] or any(call not in calls(ci) for call in calls(candidate)):
            raise ValueError('candidate and CI verification environments differ')
        if source == 'python-compatibility':
            for value in (candidate, ci):
                if value['strategy']['matrix']['os'] != ['ubuntu-24.04', 'windows-latest']:
                    raise ValueError('installed wheel needs both operating systems')
                if not any(s.get('with', {}).get('python-version-file') == '.python-version'
                           for s in actions(value, 'actions/setup-python')):
                    raise ValueError('installed wheel Python identity differs')
        if source == 'linux':
            def transport(value):
                command = next(c for c in runs(value) if ('qualify_target', '') in invocations(c))
                tokens = shlex.split(command, comments=True)
                return tokens[:tokens.index('-c') + 1]
            if transport(candidate) != transport(ci):
                raise ValueError('candidate and CI container transports differ')
    publication = {node['context']['job'] for node in graph['nodes'].values()
                   if node['context']['workflow'] == 'release.yml' and node['kind'] == 'step'
                   and invocations(node['definition'].get('run', ''))}
    if publication != {'qualify', 'publish'}:
        raise ValueError('publication guard invocation changed')
    for name, required in [('qualify', [('release_publication', 'prepare'), ('release_publication', 'upload')]),
                           ('publish', [('release_publication', 'publish')])]:
        value = job('release.yml', name)
        if value['runs-on'] != 'ubuntu-24.04' or calls(value) != required:
            raise ValueError('publication counterpart differs')
    recheck = job('ci.yml', 'release-package-recheck')
    if recheck['runs-on'] != 'ubuntu-24.04' or set(recheck['needs']) != {
            'release-package-build', 'docker-conformance', 'native-windows'}:
        raise ValueError('recheck environment or producers differ')
    release_step = recheck_steps(recheck, invocations)['verify-release-packages']
    if '--release' not in shlex.split(release_step['run'], comments=True):
        raise ValueError('complete release recheck missing')


def artifact_plan(graph):
    """Select the three handoff producers and consumer from the same graph."""
    from ci_plan import compile_plan
    jobs = {}
    producers = {'docker-conformance': 'guard-candidate-linux',
                 'native-windows': 'guard-candidate-windows',
                 'release-package-build': 'release-package-regression'}
    for name, asset in producers.items():
        value = job_plan(graph, 'ci.yml', name)
        steps = [s for s in actions(value, 'actions/upload-artifact') if s['with']['name'] == asset]
        if len(steps) != 1:
            raise ValueError('missing or duplicate artifact producer')
        jobs[name] = dict(value, steps=steps, needs=[])
    value = job_plan(graph, 'ci.yml', 'release-package-recheck')
    jobs['release-package-recheck'] = dict(value, steps=actions(value, 'actions/download-artifact'))
    return compile_plan({'ci.yml': {'jobs': jobs}})
