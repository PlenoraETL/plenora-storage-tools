"""Record digests of reviewed, tracked source members only; never build outputs.

Inventories, provenance and container declarations must already exist.
This is a deliberate policy edit, never a build or verification step.
"""
import argparse
import hashlib
import json
from pathlib import Path
import check_packages as gate


def record_rule(rule, data, name='', distribution=''):
    gate.check_build_paths({name or 'payload': rule})
    if gate.native_member(distribution, name):
        gate.scan_entries([(name, data)], {name: rule}, 0, distribution)
        return
    if 'container' in rule and rule['container'].get('distribution') is True:
        gate.classify_member(data, name, rule=rule, distribution=distribution)
        return
    container = gate.container_format(data)
    if container and (container not in gate.SUPPORTED or 'container' not in rule):
        raise ValueError('unsupported or undeclared source container')
    if container or not gate.is_text(data) or 'sha256' in rule:
        if 'source' not in rule:
            raise ValueError('source provenance must be declared before recording')
        committed = gate.checkout_source_bytes(rule['source'])
        if data != committed:
            raise ValueError('source bytes differ from git')
        rule['sha256'] = hashlib.sha256(committed).hexdigest()
    gate.classify_member(data, name, rule=rule, distribution=distribution)


@gate.repository_snapshot
def record(paths, repository=False, member=None):
    policy = gate.registry()
    if member:
        kind, name, source = member
        templates = gate.path_lookup(gate.path_lookup(policy, 'crates'), kind[7:]) if kind.startswith('crates/') else gate.path_lookup(policy, kind)
        if gate.normalize_path(name) not in gate.inventory(templates):
            raise ValueError('member outside exact inventory')
        rules = gate.member_rules(policy, kind)
        rule = gate.path_lookup(rules, name)
        if any(item[0] == kind and item[1] and gate.normalize_path(item[1]) == gate.normalize_path(name) for item in gate.NATIVE_DISTRIBUTIONS.values()):
            raise ValueError('cannot record a digest for a build output')
        record_rule(rule, Path(source).read_bytes(), name, kind)
    if repository:
        for kind, rules in (gate.path_lookup(policy, 'member_rules').items() if 'member_rules' in policy else []):
            for name, rule in rules.items():
                if 'source' in rule:
                    record_rule(rule, (gate.ROOT / rule['source']).read_bytes(), name, kind)
    for path in paths:
        kind, prefix, expected = gate.distribution_policy(path.name)
        entries = gate.members(path.read_bytes(), path.name)
        entries = gate.relative_entries(entries, prefix)
        gate.compare([n for n, _ in entries], expected)
        templates = gate.path_lookup(gate.path_lookup(policy, 'crates'), kind[7:]) if kind.startswith('crates/') else gate.path_lookup(policy, kind)
        mapping = dict(zip(expected, templates))
        rules = gate.member_rules(policy, kind)
        context = path.name.split('-', 2)[2][:-4] if path.name.endswith('.whl') else kind
        mapping = {gate.normalize_path(n): template for n, template in mapping.items()}
        for name, data in entries:
            if gate.generated_member(context, name):
                continue
            record_rule(gate.path_lookup(rules, gate.path_lookup(mapping, name)), data, name, context)
        gate.scan_entries(entries, {n: gate.path_lookup(rules, gate.path_lookup(mapping, n)) for n, _ in entries}, 0, context)
    gate.REGISTRY.write_text(json.dumps(policy, indent=2) + '\n', encoding='utf-8', newline='\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archives', nargs='*', type=Path)
    parser.add_argument('--repository', action='store_true')
    parser.add_argument('--member', nargs=3, metavar=('KIND', 'NAME', 'FILE'))
    args = parser.parse_args()
    record(args.archives, args.repository, args.member)
