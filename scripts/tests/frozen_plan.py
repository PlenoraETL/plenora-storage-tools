"""Frozen receipt oracle for the production workflow graph.

The executor is injected: replay receipts are never GitHub execution receipts.
No shell/action is executed by this module. Real boundary probes live in tests.
"""
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_release_guard_coverage import read_workflow, parse_workflow


from ci_plan import digest, workflows, matrix, compile_plan


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
