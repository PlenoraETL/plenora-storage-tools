"""Measure owned product code separately from tests and the maintained SMB fork.

Physical lines include documentation. Code lines count lines containing lexical
code (not comments or literal contents); they are a maintenance signal, not a
complexity or coverage score. Inputs come only from source package directories.
"""
import argparse
import ast
import io
import json
from pathlib import Path
import re
import tokenize

from check_test_layout import test_files
from rust_source import inline_test_modules, mask_noncode

ROOT = Path(__file__).resolve().parents[1]
BUDGET = ROOT / 'scripts/code-size-budget.json'


def fork_test_files(root):
    """Resolve cfg(test) external modules in the upstream fork as well."""
    dedicated = set()
    for path in (root / 'crates/plenora-smb2/src').rglob('*.rs'):
        code = mask_noncode(path.read_text(encoding='utf-8'))
        pattern = (r'#\s*\[\s*cfg\s*\(\s*test\s*\)\s*\]\s*'
                   r'(?:pub(?:\([^)]*\))?\s+)?mod\s+(\w+)\s*;')
        for match in re.finditer(pattern, code):
            base = path.parent if path.stem in {'lib', 'mod'} else path.with_suffix('')
            candidates = [base / (match[1] + '.rs'), base / match[1] / 'mod.rs']
            children = [child for child in candidates if child.is_file()]
            if len(children) != 1:
                raise ValueError('fork test module is missing or ambiguous')
            dedicated.add(children[0].resolve())
    return dedicated


def count_rust(source):
    for start, _, end, _ in reversed(inline_test_modules(source)):
        source = source[:start] + source[end:]
    return {'physical_lines': len(source.splitlines()),
            'code_lines': sum(bool(line.strip()) for line in mask_noncode(source).splitlines())}


def count_python(source):
    doc_lines = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if ast.get_docstring(node) is not None:
                doc = node.body[0]
                doc_lines.update(range(doc.lineno, doc.end_lineno + 1))
    code_lines = set()
    ignored = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
               tokenize.DEDENT, tokenize.ENDMARKER, tokenize.ENCODING}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type not in ignored and token.start[0] not in doc_lines:
            code_lines.add(token.start[0])
    return {'physical_lines': len(source.splitlines()), 'code_lines': len(code_lines)}


def measure(root=ROOT):
    _, dedicated = test_files(root)
    dedicated.update(fork_test_files(root))
    files, areas = {}, {}
    for path in sorted((root / 'crates').glob('*/src/**/*.rs')):
        if path.resolve() in dedicated:
            continue
        relative = path.relative_to(root).as_posix()
        area = path.relative_to(root).parts[1]
        count = count_rust(path.read_text(encoding='utf-8'))
        files[relative] = {'area': area, **count}
    package = root / 'crates/plenora-storage-py/python/plenora_storage'
    for path in sorted(package.rglob('*')):
        if path.suffix in {'.py', '.pyi'}:
            files[path.relative_to(root).as_posix()] = {
                'area': 'python-package', **count_python(path.read_text(encoding='utf-8'))}
    for count in files.values():
        area = areas.setdefault(count['area'], {'physical_lines': 0, 'code_lines': 0})
        for metric in area:
            area[metric] += count[metric]
    return {'schema_version': 1, 'areas': dict(sorted(areas.items())), 'files': files,
            'third_party_forks': ['plenora-smb2'],
            'owned_total': {metric: sum(count[metric] for area, count in areas.items()
                                       if area != 'plenora-smb2')
                            for metric in ('physical_lines', 'code_lines')}}


def check_budget(report, budget):
    failures = []
    if set(report['areas']) != set(budget['areas']):
        failures.append('area inventory differs; review the budget explicitly')
    for area, actual in report['areas'].items():
        if area not in budget['areas']:
            continue
        limits = budget['areas'][area]
        for metric in ('physical_lines', 'code_lines'):
            if actual[metric] > limits[metric]:
                failures.append(f'{area}: {metric} {actual[metric]} > {limits[metric]}')
        for path, count in report['files'].items():
            if count['area'] == area and count['code_lines'] > limits['max_file_code_lines']:
                failures.append(f'{path}: code_lines {count["code_lines"]} > {limits["max_file_code_lines"]}')
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = measure()
    encoded = json.dumps(report, indent=2, sort_keys=True) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding='utf-8', newline='\n')
    print(json.dumps({'areas': report['areas'], 'owned_total': report['owned_total']}, indent=2))
    if args.check:
        failures = check_budget(report, json.loads(BUDGET.read_text(encoding='utf-8')))
        if failures:
            raise SystemExit('\n'.join(failures))
        print('PASS product code size budgets')


if __name__ == '__main__':
    main()
