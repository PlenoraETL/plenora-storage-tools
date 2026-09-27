"""Require a traceable issue for unfinished-work markers in owned source comments.

This is an objective debt check, not a score for prose quality. Descriptions of
invariants, error effects and compatibility remain a human review responsibility.
String fixtures containing marker words are not source comments.
"""
import ast
import io
from pathlib import Path
import re
import tokenize

from rust_source import mask_noncode

ROOT = Path(__file__).resolve().parents[1]
MARKER = re.compile(r'\b(?:TODO|FIXME|HACK|XXX)\b')
ISSUE = re.compile(r'(?:https://\S+/(?:issues|pull)/\d+|(?<!\w)#\d+\b|\b[A-Z][A-Z0-9]+-\d+\b)')


def comments(path, source):
    if path.suffix == '.rs':
        spans = []
        mask_noncode(source, spans)
        return [(source.count('\n', 0, start) + 1, text) for start, text in spans]
    result = [(token.start[0], token.string) for token in tokenize.generate_tokens(io.StringIO(source).readline)
              if token.type == tokenize.COMMENT]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            text = ast.get_docstring(node)
            if text is not None:
                result.append((node.body[0].lineno, text))
    return result


def violations(path, source):
    return [line for line, text in comments(path, source) if MARKER.search(text) and not ISSUE.search(text)]


def main():
    files = [*sorted((ROOT / 'crates').glob('plenora-storage-*/src/**/*.rs')),
             *sorted((ROOT / 'crates/plenora-storage-py').rglob('*.py')),
             *sorted((ROOT / 'scripts').rglob('*.py'))]
    errors = [f'{path.relative_to(ROOT)}:{line}: unfinished-work comment requires an issue reference'
              for path in files for line in violations(path, path.read_text(encoding='utf-8'))]
    if errors:
        raise SystemExit('\n'.join(errors))
    print(f'PASS comment debt references: {len(files)} owned Rust/Python files')


if __name__ == '__main__':
    main()
