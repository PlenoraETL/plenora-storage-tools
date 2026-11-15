"""Test-only executable receipt contract, sourced from workflow YAML.

The executor is injected: replay receipts are never GitHub execution receipts.
No shell/action is executed by this module. Real boundary probes live in tests.
"""
import hashlib
import itertools
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_release_guard_coverage import read_workflow, parse_workflow


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def workflows(root):
    return {p.name: read_workflow(p)
            for p in sorted((root / '.github/workflows').glob('*.yml'))}


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


def replay(plan, subject, executor):
    """Dispatch every node; failure has a receipt too, never silently a PASS."""
    receipts = {}
    for identifier in plan['order']:
        node = plan['nodes'][identifier]
        incoming = {parent: digest(receipts[parent]) for parent in node['needs']}
        status, observation = executor(identifier, node, subject, incoming)
        receipts[identifier] = {'node': identifier, 'definition': digest(node), 'subject': subject,
                               'scope': 'contract-replay', 'status': status, 'needs': incoming,
                               'observation': observation, 'observation_sha256': digest(observation)}
    return {'plan_sha256': digest(plan), 'subject': subject, 'receipts': receipts}


def verify(plan, subject, bundle):
    if bundle['plan_sha256'] != digest(plan) or bundle['subject'] != subject:
        raise ValueError('plan or subject changed')
    receipts = bundle['receipts']
    if set(receipts) != set(plan['nodes']):
        raise ValueError('missing or extra job/step receipt')
    for identifier in plan['order']:
        node, row = plan['nodes'][identifier], receipts[identifier]
        if (row['node'] != identifier or row['definition'] != digest(node) or row['subject'] != subject
                or row['scope'] != 'contract-replay' or row['status'] != 'OBSERVED'):
            raise ValueError('unobserved or foreign execution')
        if row['needs'] != {parent: digest(receipts[parent]) for parent in node['needs']}:
            raise ValueError('dependency receipt changed')
        if row['observation_sha256'] != digest(row['observation']):
            raise ValueError('observation changed')
        # The callback must acknowledge the actual dispatch, not just say PASS.
        if row['observation'] != {'node': identifier, 'input': digest(node), 'subject': digest(subject)}:
            raise ValueError('executor did not observe the declared subject/step')


def observer(identifier, node, subject, incoming):
    return 'OBSERVED', {'node': identifier, 'input': digest(node), 'subject': digest(subject)}


def export_contract(root, folder, subject):
    """Persist the dispatch proof separately from test and qualification receipts."""
    graph = compile_plan(workflows(root))
    bound = {k: subject[k] for k in ('revision', 'target', 'run')}
    bound.update(mode='contract-replay', source_inventory_sha256=digest(subject['sources']))
    result = replay(graph, bound, observer)
    verify(graph, bound, result)
    paths = {}
    for name, value in [('workflow-plan.json', graph), ('workflow-receipts.json', result)]:
        path = folder / name
        path.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n', encoding='utf-8')
        paths[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {'scope': 'contract-replay', 'nodes': len(graph['nodes']),
            'jobs': sum(n['kind'] == 'job' for n in graph['nodes'].values()), 'files': paths}
