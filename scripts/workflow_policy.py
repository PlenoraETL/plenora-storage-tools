"""Owner policy: expensive evidence requires an explicit workflow dispatch."""
from pathlib import Path

MANUAL = "github.event_name == 'workflow_dispatch'"
LONG_JOBS = {
    ('ci.yml', name) for name in (
        'production-coverage', 'parser-fuzz', 'api-compatibility', 'product-quality',
        'docker-conformance', 'native-windows', 'python-compatibility',
        'release-package-build', 'release-package-recheck')
} | {('scheduled.yml', 'parser-fuzz')}


def automatic_jobs(documents, events=('push', 'pull_request', 'schedule')):
    """Follow reusable calls only from automatic event roots and enabled jobs."""
    seen = set()

    def visit(name):
        if name in seen:
            return
        seen.add(name)
        for key, job in documents[name]['jobs'].items():
            if job.get('if') == MANUAL and 'workflow_dispatch' not in events:
                continue
            yield name, key
            if 'uses' in job:
                yield from visit(Path(job['uses']).name)

    for name, document in documents.items():
        if set(events) & set(document.get('on') or {}):
            yield from visit(name)


def check(documents):
    for name, document in documents.items():
        for key, job in document['jobs'].items():
            runner = job.get('runs-on')
            runners = (job.get('strategy', {}).get('matrix', {}).get('os', [])
                       if runner == '${{ matrix.os }}' else [runner] if runner else [])
            if runner and (not runners or any(not value.startswith(('ubuntu-', 'windows-'))
                                              for value in runners)):
                raise ValueError(f'{name}/{key}: unsupported platform')
    for name, key in LONG_JOBS:
        document = documents[name]
        if ('workflow_dispatch' not in document.get('on', {})
                or document['jobs'][key].get('if') != MANUAL):
            raise ValueError(f'{name}/{key}: requires manual dispatch')
    for name, key in automatic_jobs(documents):
        job = documents[name]['jobs'][key]
        if 'uses' not in job and job.get('timeout-minutes', 360) > 30:
            raise ValueError(f'{name}/{key}: automatic budget exceeds 30 minutes')
