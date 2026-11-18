"""Executable CI graph derived only from the bounded workflow reader."""
import hashlib
import itertools
import json
from pathlib import Path
from check_release_guard_coverage import read_workflow, parse_workflow

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def workflows(root):
    return {p.name: read_workflow(p)
            for p in sorted((root / '.github/workflows').glob('*.y*ml'))}


def matrix(job):
    axes = job.get('strategy', {}).get('matrix', {})
    if any(not isinstance(v, list) for v in axes.values()) or {'include', 'exclude'} & axes.keys():
        raise ValueError('unsupported matrix; extend executable contract')
    return [dict(zip(axes, values)) for values in itertools.product(*axes.values())]


def compile_plan(documents):
    nodes = {}
    def expand(workflow, prefix, inherited=(), stack=()):
        if workflow in stack:
            raise ValueError('recursive workflow')
        document = documents[workflow]
        jobs = document['jobs']
        ids = {name: [prefix + '/' + name + '/' + str(i) for i, _ in enumerate(matrix(job))]
               for name, job in jobs.items()}
        final = []
        for name, job in jobs.items():
            needs = job.get('needs', [])
            needs = [needs] if isinstance(needs, str) else needs
            dependencies = list(inherited) + [node for need in needs for node in ids[need]]
            for identifier, values in zip(ids[name], matrix(job)):
                context = {'workflow': workflow, 'job': name, 'matrix': values,
                           'workflow_settings': {k: v for k, v in document.items() if k != 'jobs'},
                           'job_settings': {k: v for k, v in job.items() if k != 'steps'}}
                previous = dependencies
                if 'uses' in job:
                    called = job['uses']
                    if not called.startswith('./.github/workflows/'):
                        raise ValueError('unsupported external job workflow')
                    previous = expand(Path(called).name, identifier + '/call', dependencies, (*stack, workflow))
                else:
                    for number, step in enumerate(job['steps']):
                        if ('run' in step) == ('uses' in step):
                            raise ValueError('step must have exactly one executor')
                        sid = identifier + '/step/' + str(number)
                        nodes[sid] = {'kind': 'step', 'context': context, 'definition': step, 'needs': list(previous)}
                        previous = [sid]
                nodes[identifier] = {'kind': 'job', 'context': context, 'definition': {}, 'needs': list(previous)}
                final.append(identifier)
        return final
    for workflow in sorted(documents):
        expand(workflow, workflow)
    # Resolve DAG independently of declaration order and fail on cycles.
    pending, order = set(nodes), []
    while pending:
        available = sorted(n for n in pending if set(nodes[n]['needs']) <= set(order))
        if not available:
            raise ValueError('workflow dependency cycle')
        order.extend(available)
        pending.difference_update(available)
    return {'nodes': nodes, 'order': order}


def execute(plan, subject, executor, *, scope):
    """Execute a graph through an explicit adapter, retaining failed obligations.

    Scope names describe the adapter, never imply hosted Actions qualification.
    A dependent node is blocked after failure, while independent work continues.
    """
    receipts = {}
    for identifier in plan['order']:
        node = plan['nodes'][identifier]
        incoming = {parent: digest(receipts[parent]) for parent in node['needs']}
        status, observation = 'BLOCKED', {}
        if all(receipts[parent]['status'] == 'PASS' for parent in node['needs']):
            try:
                status, observation = executor(identifier, node, subject, incoming)
            except Exception:
                # Executor exceptions may contain payloads, paths or credentials.
                status, observation = 'ERROR', {'category': 'executor-failure'}
        receipts[identifier] = dict(node=identifier, definition=digest(node), subject=subject,
                                   scope=scope, status=status, needs=incoming,
                                   observation=observation, observation_sha256=digest(observation))
    return dict(plan_sha256=digest(plan), subject=subject, receipts=receipts)


def verify_execution(plan, subject, bundle, *, scope):
    if bundle['plan_sha256'] != digest(plan) or bundle['subject'] != subject:
        raise ValueError('execution subject or plan changed')
    receipts = bundle['receipts']
    if set(receipts) != set(plan['nodes']) or not receipts:
        raise ValueError('execution receipt inventory differs')
    for identifier in plan['order']:
        node, row = plan['nodes'][identifier], receipts[identifier]
        if (row['node'] != identifier or row['definition'] != digest(node)
                or row['subject'] != subject or row['scope'] != scope or row['status'] != 'PASS'
                or row['needs'] != {parent: digest(receipts[parent]) for parent in node['needs']}
                or row['observation_sha256'] != digest(row['observation'])
                or not row['observation']):
            raise ValueError('execution receipt not successful or changed')


def job_plan(graph, workflow, job):
    """Project definitions from the compiled graph, never parse YAML a second time."""
    rows = [node for key, node in graph['nodes'].items()
            if key.startswith(workflow + '/' + job + '/')
            and node['kind'] == 'step' and node['context']['workflow'] == workflow
            and node['context']['job'] == job]
    # The policy view of one definition is independent of matrix expansion.
    definitions = []
    for row in rows:
        if row['context']['matrix'] == rows[0]['context']['matrix']:
            definitions.append(row['definition'])
    if not rows:
        raise ValueError('required executable job missing')
    return dict(rows[0]['context']['job_settings'], steps=definitions)


