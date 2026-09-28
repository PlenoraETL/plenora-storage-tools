"""Require a traceable issue for unfinished-work markers in owned source comments.

This is an objective debt check, not a score for prose quality. Descriptions of
invariants, error effects and compatibility remain a human review responsibility.
String fixtures containing marker words are not source comments.
"""
import ast
import io
from pathlib import Path
import re
import subprocess
import tokenize

from rust_source import mask_noncode
from hash_comments import comments as hash_comments

ROOT = Path(__file__).resolve().parents[1]
MARKER = re.compile(r'\b(?:TODO|FIXME|HACK|XXX)\b')
ISSUE = re.compile(r'(?:https://\S+/(?:issues|pull)/\d+|(?<!\w)#\d+\b|\b[A-Z][A-Z0-9]+-\d+\b)')
HISTORY = re.compile(r'\b(?:pre-fix|post-review|fix review|in this commit|in the previous implementation|'
                     r'prima di questo fix|nello stesso commit|prima mancava|qui c[’\']era)\b', re.IGNORECASE)
EXTENSIONS = {'.rs', '.py', '.pyi', '.sh', '.ps1', '.toml', '.yaml', '.yml'}
EXCLUDED = {'target', 'dist', 'vendor', 'contracts', 'docs', '.venv', '.fixtures', '__pycache__'}


def comments(path, source):
    if path.suffix == '.rs':
        spans = []
        mask_noncode(source, spans)
        return [(source.count('\n', 0, start) + 1, text) for start, text in spans]
    if path.suffix not in {'.py', '.pyi'}:
        return hash_comments(source, path.suffix)
    result = [(token.start[0], token.string) for token in tokenize.generate_tokens(io.StringIO(source).readline)
              if token.type == tokenize.COMMENT]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            text = ast.get_docstring(node)
            if text is not None:
                result.append((node.body[0].lineno, text))
    return result


def violations(path, source):
    return [line for line, text in comments(path, source)
            if (MARKER.search(text) and not ISSUE.search(text)) or HISTORY.search(text)]


def owned_files(root=ROOT):
    names = subprocess.run(['git', 'ls-files', '--cached', '--others', '--exclude-standard'],
                           cwd=root, check=True, capture_output=True, text=True).stdout.splitlines()
    return sorted({root / name for name in names
                   if not (set(Path(name).parts) & EXCLUDED)
                   and 'plenora-smb2' not in Path(name).parts
                   and Path(name).suffix in EXTENSIONS and (root / name).is_file()})


def main():
    files = owned_files()
    errors = [f'{path.relative_to(ROOT)}:{line}: unreferenced debt or development-history comment'
              for path in files for line in violations(path, path.read_text(encoding='utf-8'))]
    if errors:
        raise SystemExit('\n'.join(errors))
    print(f'PASS comment policy: {len(files)} owned source/configuration files')


if __name__ == '__main__':
    main()
