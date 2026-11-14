"""Static PR audit of proof producers, duplicated matrices and workflow wiring.

No product imports, compilation, fixture contact or qualification claims.
The JSON is regenerated from this checkout, never used as an authority.
"""
import argparse
import ast
import json
from pathlib import Path
import re
import tomllib

from proof_sources import ROOT, dictionary_keys, fuzz_targets, providers, tree, passed_names


def literal(name, variable, root=ROOT):
    nodes = [n.value for n in tree(name, root).body if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == variable for t in n.targets)]
    if len(nodes) != 1:
        raise ValueError(f'{name}: missing or ambiguous {variable}')
    return ast.literal_eval(nodes[0])


def same(actual, expected, label):
    if len(actual) != len(set(actual)) or set(actual) != set(expected):
        raise ValueError(label + ': inventory differs from its producer')


def workflows(root=ROOT):
    result = {}
    for path in sorted((root / '.github/workflows').glob('*.y*ml')):
        text = path.read_text(encoding='utf-8')
        refs = sorted(set(re.findall(r'(?:scripts/[\w-]+\.(?:py|sh|ps1)|\./\.github/workflows/[\w-]+\.ya?ml)', text)))
        for ref in refs:
            if not (root / ref).is_file():
                raise ValueError(path.name + ': missing executable reference ' + ref)
        commands = text.replace('\\\n', ' ')
        for script, tail in re.findall(r'python3?\s+(scripts/[\w-]+\.py)([^\n]*)', commands):
            parsed = tree(script, root)
            options = {arg.value for node in ast.walk(parsed)
                       if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                       and node.func.attr == 'add_argument' for arg in node.args
                       if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith('--')}
            if options and set(re.findall(r'(?<!\w)--[a-z][a-z0-9-]*', tail)) - options:
                raise ValueError(path.name + ': workflow arguments differ from script parser')
        result[path.name] = {'references': refs,
                             'events': re.findall(r'^  (pull_request|push|schedule|workflow_call|workflow_dispatch):', text),
                             'jobs': re.findall(r'^  ([\w-]+):', text.split('jobs:\n', 1)[1])}
    ci = (root / '.github/workflows/ci.yml').read_text(encoding='utf-8')
    if not re.search(r'^  pull_request:\s*$', ci, re.M):
        raise ValueError('proof audit requires unconditional PR CI')
    for command in ('python3 scripts/check_proof_inventory.py', 'python3 -m unittest discover -s scripts/tests -v'):
        if command not in ci:
            raise ValueError('proof PR job lost required command')
    for name, item in result.items():
        if 'schedule' in item['events']:
            for ref in item['references']:
                if ref.startswith('./.github/workflows/') and ref not in ci:
                    raise ValueError(name + ': scheduled reusable workflow has no PR counterpart')
    reachable = {'ci.yml'}
    while True:
        expanded = reachable | {Path(ref).name for name in reachable
                                for ref in result[name]['references'] if ref.startswith('./.github/workflows/')}
        if expanded == reachable:
            break
        reachable = expanded
    for name in result:
        result[name]['pr_reachable'] = name in reachable or 'pull_request' in result[name]['events']
    return result


def check(root=ROOT):
    workflow_inventory = workflows(root)
    runtime = (root / 'crates/plenora-storage-core/src/runtime.rs').read_text(encoding='utf-8')
    block = runtime.split('pub const RUNTIME_OPERATIONS:', 1)[1].split('];', 1)[0]
    operations = re.findall(r'"(storage\.\w+)"', block)
    if not operations or len(operations) != len(set(operations)):
        raise ValueError('runtime operation inventory is missing or ambiguous')
    for name in ('qualify_cli.py', 'qualify_extended.py', 'qualify_python.py'):
        parsed = tree('scripts/' + name, root)
        counts = [ast.literal_eval(v) for n in ast.walk(parsed) if isinstance(n, ast.Dict)
                  for k, v in zip(n.keys, n.values) if isinstance(k, ast.Constant) and k.value == 'operations']
        if counts != [len(operations)]:
            raise ValueError(name + ': operation count differs from runtime descriptors')
    expected = providers(root)
    for name, variable in [('scripts/fixture_connections.py', 'configurations'),
                           ('scripts/qualify_python.py', 'configs')]:
        same(dictionary_keys(name, variable, root), expected, name)
    extended = dictionary_keys('scripts/qualify_extended.py', 'configs', root)
    cli = tree('scripts/qualify_cli.py', root)
    base = next(ast.literal_eval(n.iter) for n in ast.walk(cli) if isinstance(n, ast.For)
                and isinstance(n.target, ast.Name) and n.target.id == 'provider')
    same([*base, *extended], expected, 'CLI provider partition')
    probe_tree = tree('scripts/check_fixtures.py', root)
    probe = next(n for n in probe_tree.body if isinstance(n, ast.FunctionDef) and n.name == 'probes')
    probe_names = [ast.literal_eval(k) for n in ast.walk(probe) if isinstance(n, ast.Return)
                   and isinstance(n.value, ast.Dict) for k in n.value.keys]
    same(literal('scripts/check_fixtures.py', 'SERVICES', root), probe_names, 'fixture application probes')
    services = set()
    for name in ('docker-compose.yml', 'compose.extended.yml'):
        text = (root / name).read_text(encoding='utf-8').split('services:\n', 1)[1]
        services.update(re.findall(r'^  ([\w-]+):', text.split('\nvolumes:', 1)[0], re.M))
    if not set(probe_names) <= services:
        raise ValueError('fixture probes reference absent Compose services')
    targets = fuzz_targets(root)
    manifest = tomllib.loads((root / 'fuzz/Cargo.toml').read_text(encoding='utf-8'))
    same(targets, [p.stem for p in (root / 'fuzz/fuzz_targets').glob('*.rs')], 'fuzz targets')
    same(targets, [p.name for p in (root / 'fuzz/seeds').iterdir() if p.is_dir()], 'fuzz seeds')
    for entry in manifest['bin']:
        if not (root / 'fuzz' / entry['path']).is_file():
            raise ValueError('fuzz target source missing')
    pythons = literal('scripts/release_evidence.py', 'PYTHONS', root)
    for name in ('ci.yml', 'release-candidate.yml'):
        text = (root / '.github/workflows' / name).read_text(encoding='utf-8')
        matrices = re.findall(r'^        python: (\[.*\])$', text, re.M)
        if not matrices:
            raise ValueError(name + ': missing Python matrix')
        for matrix in matrices:
            same(ast.literal_eval(matrix), pythons, name + ': Python versions')
    # The package exemption list remains closed/reviewed; check its Cartesian
    # inventories against the executable producer axes, without widening it.
    members = literal('scripts/package_generated.py', 'GENERATED_MEMBERS', root)['qualification']
    platforms = literal('scripts/release_evidence.py', 'TARGETS', root)
    same([p for p in members if p.startswith('evidence/gates/fuzz/')],
         ['evidence/gates/fuzz/report.json', *[f'evidence/gates/fuzz/{t}/run.log' for t in targets]], 'packaged fuzz evidence')
    same([p for p in members if p.startswith('evidence/gates/sdk/')],
         [f'evidence/gates/sdk/{t}/{p}/{n}' for t in platforms for p in pythons
          for n in ('coverage.json', 'sdk-tests.json', 'sdk-tests.log', 'sdk-typing.json', 'sdk-typing.log')],
         'packaged SDK evidence')
    # Source-derived enumeration includes tests outside the unittest directory;
    # those must have an explicit workflow invocation (Rust API compiler probe).
    tests = {}
    scripts = {}
    all_workflows = '\n'.join((root / '.github/workflows' / n).read_text(encoding='utf-8')
                              for n, w in workflow_inventory.items() if w['pr_reachable'])
    for path in sorted((root / 'scripts').rglob('*.py')):
        relative = path.relative_to(root).as_posix()
        parsed = ast.parse(path.read_text(encoding='utf-8'))
        if path.parent == root / 'scripts':
            for node in ast.walk(parsed):
                if (isinstance(node, ast.Compare) and any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops)
                        and any(isinstance(n, ast.Constant) and n.value == 'operations' for n in ast.walk(node.left))):
                    counts = [n.value for n in node.comparators if isinstance(n, ast.Constant) and type(n.value) is int]
                    if any(count != len(operations) for count in counts):
                        raise ValueError(relative + ': validator operation count differs from runtime descriptors')
        definitions = sorted(n.name for n in ast.walk(parsed) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
        scripts[relative] = definitions
        is_suite = any(isinstance(n, ast.ClassDef) and any(
            isinstance(b, ast.Attribute) and b.attr == 'TestCase' for b in n.bases) for n in ast.walk(parsed))
        if path.name.startswith('test_') or is_suite:
            tests[relative] = [n for n in definitions if n.startswith('test_')]
            discovered = path.parent == root / 'scripts/tests' and path.name.startswith('test_')
            if not discovered and relative not in all_workflows:
                raise ValueError(relative + ': self-test not wired to a workflow')
    rust_tests = {}
    for path in sorted((root / 'crates').rglob('*.rs')):
        text = path.read_text(encoding='utf-8')
        names = re.findall(r'#\[(?:[\w]+::)*test(?:\([^]]*\))?\]\s*(?:#\[[^]]*\]\s*)*(?:async\s+)?fn\s+(\w+)', text)
        if names:
            rust_tests[path.relative_to(root).as_posix()] = names
    # A required named test may not silently become an obsolete log sentinel.
    named = re.findall(r"['\"]([a-z_][a-z0-9_]*) \.\.\. ok['\"]",
                       (root / 'scripts/qualify_release.py').read_text(encoding='utf-8'))
    discovered = {name for names in rust_tests.values() for name in names}
    if not set(named) <= discovered:
        raise ValueError('release log expects Rust tests absent from sources')
    faults = passed_names('scripts/qualify_extended_faults.py', root)
    if not set(literal('scripts/qualify_extended_faults.py', 'LEGACY_TESTS', root)) <= faults:
        raise ValueError('historical fault subset no longer has producer probes')
    campaigns = {name: sorted(passed_names('scripts/' + name, root, function='phase'))
                 for name in ('run_vm_campaign.py', 'release_campaign.py')}
    return {'schema_version': 1, 'kind': 'static_source_inventory', 'workflows': workflow_inventory,
            'scripts': scripts, 'python_tests': tests, 'providers': list(expected),
            'fixture_probes': probe_names, 'fuzz_targets': list(targets), 'python_versions': list(pythons),
            'rust_test_declarations': rust_tests, 'fault_probe_labels': sorted(faults),
            'campaign_phases': campaigns, 'operations': operations}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'target/proof-inventory.json')
    args = parser.parse_args()
    report = check()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print('PASS static proof inventories and PR wiring (no runtime qualification)')


if __name__ == '__main__':
    main()
