"""Record exact binary digests from reviewed artifacts; never run from a gate.

Container declarations and member inventories must already exist. This command
changes policy and requires review; it does not certify the input artifacts.
"""
import argparse
import hashlib
import json
from pathlib import Path
import check_packages as gate


def record(paths, repository=False, member=None):
    policy = gate.registry()
    if member:
        kind, name, source = member
        templates = policy['crates'][kind[7:]] if kind.startswith('crates/') else policy[kind]
        if name not in templates:
            raise ValueError('member outside exact inventory')
        data = Path(source).read_bytes()
        if gate.container_format(data):
            raise ValueError('use an archive inventory to record container members')
        policy.setdefault('member_rules', {}).setdefault(kind, {}).setdefault(name, {})['sha256'] = hashlib.sha256(data).hexdigest()
    if repository:
        for kind in ('source', 'cli-linux', 'cli-windows'):
            rules = policy.setdefault('member_rules', {}).setdefault(kind, {})
            for name in policy[kind]:
                source = gate.ROOT / name
                if source.is_file():
                    data = source.read_bytes()
                    if gate.container_format(data) or not gate.is_text(data):
                        rule = rules.setdefault(name, {})
                        container = gate.container_format(data)
                        if container and (container not in gate.SUPPORTED or 'container' not in rule):
                            raise ValueError('unsupported or undeclared repository container')
                        rule['sha256'] = hashlib.sha256(data).hexdigest()
    for path in paths:
        kind, prefix, expected = gate.distribution_policy(path.name)
        entries = gate.members(path.read_bytes())
        if any(not n.startswith(prefix) for n, _ in entries):
            raise ValueError('unexpected distribution root')
        entries = [(n[len(prefix):], data) for n, data in entries]
        gate.compare([n for n, _ in entries], expected)
        rules = policy.setdefault('member_rules', {}).setdefault(kind, {})
        # Resolve template names without deriving new inventory entries.
        templates = policy['crates'][kind[7:]] if kind.startswith('crates/') else policy[kind]
        mapping = dict(zip(expected, templates))
        for name, data in entries:
            container = gate.container_format(data)
            rule = rules.setdefault(mapping[name], {})
            if container and (container not in gate.SUPPORTED or 'container' not in rule):
                raise ValueError('unsupported or undeclared container during digest recording')
            if 'container' in rule or not gate.is_text(data) or 'sha256' in rule:
                rule['sha256'] = hashlib.sha256(data).hexdigest()
            if not rule:
                del rules[mapping[name]]
    gate.REGISTRY.write_text(json.dumps(policy, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archives', nargs='*', type=Path)
    parser.add_argument('--repository', action='store_true')
    parser.add_argument('--member', nargs=3, metavar=('KIND', 'NAME', 'FILE'))
    args = parser.parse_args()
    record(args.archives, args.repository, args.member)
