"""Fail closed when a release guard invocation loses its PR CI counterpart."""
import ast
import strict_json
from functools import lru_cache
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Workflow(dict):
    """Structured workflow with the legacy classifier's raw job view."""

    def __init__(self, values, raw_jobs):
        super().__init__(values)
        self.raw_jobs = raw_jobs


def parse_workflow(text):
    """Read the repository's bounded YAML subset, without external packages.

    Supports indentation maps/sequences, literal blocks, scalar flow lists,
    quoted strings and JSON-style numbers/booleans/null. Unsupported YAML
    (anchors, tags, folded blocks, complex keys/flow maps, multiline scalars)
    fails closed. Actions expressions remain opaque strings. Both classifier
    and receipt plan consume this reader; raw_jobs only preserves legacy API.
    """
    lines = text.splitlines(keepends=True)
    raw_jobs = {}

    def clean(value):
        quote = None
        i = 0
        while i < len(value):
            char = value[i]
            if quote:
                if char == '\\' and quote == '"':
                    i += 2
                    continue
                if char == quote:
                    if quote == "'" and i + 1 < len(value) and value[i + 1] == "'":
                        i += 2
                        continue
                    quote = None
            elif char in "\"'" and (i == 0 or value[i - 1] in ' [,'):
                quote = char
            elif char == '#' and (i == 0 or value[i - 1].isspace()):
                return value[:i].rstrip()
            i += 1
        if quote:
            raise ValueError('unterminated workflow scalar')
        return value.rstrip()

    def scalar(value):
        if not value:
            return None
        if value.startswith('['):
            if not value.endswith(']'):
                raise ValueError('multiline flow sequence unsupported')
            # Split only outside quoted strings. Nested collections are not
            # part of this workflow dialect and must never be misread.
            content = value[1:-1].strip()
            items = []
            while content:
                match = re.match(r'''\s*('(?:[^']|'')*'|"(?:[^"\\]|\\.)*"|[^,'"\[\]{}]+)\s*(,|$)''', content)
                if not match:
                    raise ValueError('unsupported flow sequence item')
                items.append(match[1])
                content = content[match.end():].strip()
            result = []
            for item in items:
                item = item.strip()
                if item.startswith(('[', '{')):
                    raise ValueError('nested flow collection unsupported')
                result.append(scalar(item))
            return result
        if value.startswith("'"):
            if not re.fullmatch(r"'(?:[^']|'')*'", value):
                raise ValueError('invalid single quoted scalar')
            return value[1:-1].replace("''", "'")
        if value.startswith('"'):
            return strict_json.loads(value)
        if value[0] in '&*!>|{}%@`~' or re.search(r':(?:\s|$)', value):
            raise ValueError('unsupported workflow scalar')
        if value in ('true', 'false', 'null') or re.fullmatch(r'-?(?:0|[1-9][0-9]*)', value):
            return strict_json.loads(value)
        if re.fullmatch(r'[-+]?[0-9][0-9.eExobA-F+-]*', value):
            raise ValueError('unsupported numeric scalar; quote string values')
        return value

    def entry(i):
        line = lines[i].rstrip('\r\n')
        indent = len(line) - len(line.lstrip(' '))
        if '\t' in line[:indent + 1]:
            raise ValueError('tab indentation unsupported')
        return indent, clean(line[indent:])

    def skip(i):
        while i < len(lines) and not entry(i)[1]:
            i += 1
        return i

    def pair(value):
        match = re.fullmatch(r'([\w-]+|"[\w-]+"|\'[\w-]+\'):(?: +(.*))?', value)
        if not match:
            raise ValueError('unsupported workflow mapping')
        return match[1].strip('\'"'), match[2] or ''

    def value_at(value, i, indent, path):
        if value in ('|', '|-', '|+'):
            start = i
            while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip(' ')) > indent):
                i += 1
            block = lines[start:i]
            nonblank = [len(line) - len(line.lstrip(' ')) for line in block if line.strip()]
            width = nonblank[0] if nonblank else indent + 1
            if any(level < width for level in nonblank):
                raise ValueError('invalid literal block indentation')
            content = ''.join(line[width:] if line.strip() else '\n' for line in block)
            if value != '|+':
                content = content.rstrip('\n') + ('\n' if value == '|' and nonblank else '')
            return content, i
        if value:
            return scalar(value), i
        child = skip(i)
        if child < len(lines) and entry(child)[0] > indent:
            return block_at(child, entry(child)[0], path)
        return None, i

    def block_at(i, indent, path):
        sequence = entry(i)[1].startswith('- ')
        result = [] if sequence else {}
        while (i := skip(i)) < len(lines):
            level, token = entry(i)
            if level < indent:
                break
            if level != indent:
                raise ValueError('unexpected workflow indentation')
            start = i
            if sequence:
                if not token.startswith('- '):
                    raise ValueError('mixed workflow collection')
                token = token[2:]
                # Workflow sequences contain mappings (steps/schedule) or scalars.
                if not re.match(r'[\w-]+:(?: |$)', token):
                    result.append(scalar(token))
                    i += 1
                    continue
                key, value = pair(token)
                parsed, i = value_at(value, i + 1, indent + 2, path)
                item = {key: parsed}
                child = skip(i)
                if child < len(lines) and entry(child)[0] > indent:
                    if entry(child)[0] != indent + 2:
                        raise ValueError('invalid sequence mapping indentation')
                    tail, i = block_at(child, indent + 2, path)
                    if not isinstance(tail, dict) or key in tail:
                        raise ValueError('duplicate workflow key')
                    item.update(tail)
                result.append(item)
            else:
                key, value = pair(token)
                if key in result:
                    raise ValueError('duplicate workflow key')
                result[key], i = value_at(value, i + 1, indent, path + (key,))
                if path == ('jobs',):
                    raw_jobs[key] = ''.join(lines[start + 1:i])
        return result, i

    start = skip(0)
    if start == len(lines) or entry(start)[0] != 0:
        raise ValueError('workflow must be a root mapping')
    values, end = block_at(start, 0, ())
    if not isinstance(values, dict) or not isinstance(values.get('jobs'), dict) or end != len(lines):
        raise ValueError('workflow jobs mapping required')
    return Workflow(values, raw_jobs)


def read_workflow(path):
    return parse_workflow(Path(path).read_text(encoding='utf-8'))


def jobs(name):
    return read_workflow(ROOT / '.github/workflows' / name).raw_jobs


@lru_cache(maxsize=1)
def guard_scripts():
    # Discover wrappers from imports and literal subprocess script paths. A new
    # wrapper is included without extending a hand-maintained name regex.
    dependencies = {}
    for path in (ROOT / 'scripts').glob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split('.')[0])
            elif isinstance(node, ast.Import):
                names.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if re.fullmatch(r'(?:scripts/)?[a-z_][a-z0-9_]*\.py', node.value):
                    names.add(Path(node.value).stem)
        dependencies[path.stem] = names
    guards = {'check_packages'}
    while True:
        expanded = guards | {name for name, deps in dependencies.items() if deps & guards}
        if expanded == guards:
            return guards
        guards = expanded


def invocations(job):
    found = re.findall(r'scripts/([a-z_][a-z0-9_]*)\.py(?:[ \t]+([\w-]+))?', job)
    return [(script, arg if script == 'release_publication' else '') for script, arg in found
            if script in guard_scripts() and
            (script != 'release_publication' or arg not in {'draft', 'smoke', 'smoke-cli'})]


def check(candidate=None, release=None, ci=None):
    from ci_plan import workflows, compile_plan
    from ci_policy import check as check_policy
    from ci_contract import source_subject, handoff, boundaries
    from ci_shell import contracts
    documents = workflows(ROOT)
    # Legacy callers supply raw job views; parse those with the same reader.
    for name, supplied in [('release-candidate.yml', candidate), ('release.yml', release), ('ci.yml', ci)]:
        if supplied is not None:
            documents[name] = parse_workflow('jobs:\n' + ''.join(
                '  ' + key + ':\n' + value for key, value in supplied.items()))
    graph = compile_plan(documents)
    check_policy(graph, invocations)
    subject = source_subject(ROOT)
    result = {'scope': 'offline-executor-contract', 'subject': subject, 'plan': graph,
              'handoff': handoff(graph, subject), 'boundaries': boundaries(ROOT, subject),
              'shell': contracts(graph, invocations)}
    if source_subject(ROOT)['sources'] != subject['sources']:
        raise ValueError('sources changed during execution')
    return result


if __name__ == '__main__':
    import argparse
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'target/ci-executor-receipts.json')
    args = parser.parse_args()
    result = check()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    print('PASS offline executor contracts; hosted Actions and Rust builds not executed')
