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
    if rule.get('container', {}).get('distribution') is True:
        gate.classify_member(data, name, rule=rule, distribution=distribution)
        return
    container = gate.container_format(data)
    if container and (container not in gate.SUPPORTED or 'container' not in rule):
        raise ValueError('unsupported or undeclared source container')
    if container or not gate.is_text(data) or 'sha256' in rule:
        if 'source' not in rule:
            raise ValueError('source provenance must be declared before recording')
        committed = gate.source_bytes(rule['source'])
        if data != committed:
            raise ValueError('source bytes differ from git')
        rule['sha256'] = hashlib.sha256(committed).hexdigest()
        gate.classify_member(data, name, rule=rule, distribution=distribution)


def record(paths, repository=False, member=None):
    policy = gate.registry()
    if member:
        kind, name, source = member
        templates = policy['crates'][kind[7:]] if kind.startswith('crates/') else policy[kind]
        if gate.normalize_path(name) not in gate.inventory(templates):
            raise ValueError('member outside exact inventory')
        rules = {gate.normalize_path(n): r for n, r in policy.get('member_rules', {}).get(kind, {}).items()}
        rule = rules.get(gate.normalize_path(name), {})
        if any(item[0] == kind and item[1] and gate.normalize_path(item[1]) == gate.normalize_path(name) for item in gate.NATIVE_DISTRIBUTIONS.values()):
            raise ValueError('cannot record a digest for a build output')
        record_rule(rule, Path(source).read_bytes())
    if repository:
        for rules in policy.get('member_rules', {}).values():
            for rule in rules.values():
                if 'source' in rule:
                    record_rule(rule, (gate.ROOT / rule['source']).read_bytes())
    for path in paths:
        kind, prefix, expected = gate.distribution_policy(path.name)
        entries = gate.members(path.read_bytes())
        entries = gate.relative_entries(entries, prefix)
        gate.compare([n for n, _ in entries], expected)
        templates = policy['crates'][kind[7:]] if kind.startswith('crates/') else policy[kind]
        mapping = dict(zip(expected, templates))
        rules = policy.get('member_rules', {}).get(kind, {})
        context = path.name.split('-', 2)[2][:-4] if path.name.endswith('.whl') else kind
        mapping = {gate.normalize_path(n): template for n, template in mapping.items()}
        for name, data in entries:
            record_rule(rules.get(mapping[gate.normalize_path(name)], {}), data, name, context)
    gate.REGISTRY.write_text(json.dumps(policy, indent=2) + '\n', encoding='utf-8', newline='\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archives', nargs='*', type=Path)
    parser.add_argument('--repository', action='store_true')
    parser.add_argument('--member', nargs=3, metavar=('KIND', 'NAME', 'FILE'))
    args = parser.parse_args()
    record(args.archives, args.repository, args.member)
