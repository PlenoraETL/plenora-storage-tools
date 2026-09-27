"""Check generated state, SDK guides, local links, anchors and Python examples."""
import ast
import re
from pathlib import Path
import tomllib
from urllib.parse import unquote
from render_state import ROOT, render
from versioning import workspace_version
from render_api_inventory import render as render_api_inventory


def anchors(text):
    # Ignore fenced examples: a Python comment is not a Markdown heading.
    text = re.sub(r'^```[^\n]*\n.*?^```\s*$', '', text, flags=re.MULTILINE | re.DOTALL)
    counts, result = {}, set()
    for title in re.findall(r'^ {0,3}#{1,6}\s+(.+?)\s*#*$', text, re.MULTILINE):
        slug = re.sub(r'[^\w\- ]', '', title.strip().lower()).replace(' ', '-')
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        result.add(slug + (f'-{count}' if count else ''))
    result.update(re.findall(r'<(?:a|span)\b[^>]*(?:id|name)=[\"\']([^\"\']+)', text))
    return result


def check_document(path, root=ROOT):
    text = path.read_text(encoding='utf-8')
    links = re.findall(r'\]\(([^)]+)\)', text)
    links += re.findall(r'^\[[^\]]+\]:\s+(\S+)', text, re.MULTILINE)
    for link in links:
        if '://' in link or link.startswith('mailto:'):
            continue
        location, _, fragment = link.strip('<>').partition('#')
        target = (path.parent / unquote(location)).resolve() if location else path
        if not target.exists():
            raise ValueError(f'{path.relative_to(root)}: broken link {link}')
        if fragment and target.suffix.lower() == '.md':
            if unquote(fragment) not in anchors(target.read_text(encoding='utf-8')):
                raise ValueError(f'{path.relative_to(root)}: missing anchor {link}')
    for example in re.findall(r'^```(?:python|py)\s*\n(.*?)^```\s*$', text, re.MULTILINE | re.DOTALL):
        compile(example, str(path), 'exec', flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    for command in re.findall(r'\b(?:python3?|bash|pwsh)\s+((?:scripts|crates)/[\w./-]+\.(?:py|sh|ps1))\b', text):
        if not (root / command).is_file():
            raise ValueError(f'{path.relative_to(root)}: command script is missing: {command}')


def main():
    expected = render()
    assert (ROOT / 'docs/STATO.md').read_text(encoding='utf-8') == expected, 'run scripts/render_state.py'
    assert (ROOT / 'docs/API-INVENTORY.md').read_text(encoding='utf-8') == render_api_inventory(), 'run scripts/render_api_inventory.py'
    version = workspace_version()
    pyproject = tomllib.loads((ROOT / 'crates/plenora-storage-py/pyproject.toml').read_text())
    assert pyproject['project']['version'] == version.python, 'Python and native versions differ'
    paths = [ROOT / 'README.md', ROOT / 'AGENTS.md', *sorted((ROOT / 'docs').rglob('*.md')),
             *sorted((ROOT / 'crates').glob('plenora-storage-*/README.md')),
             *sorted((ROOT / 'crates').glob('plenora-storage-*/docs/**/*.md'))]
    for path in paths:
        check_document(path)
    print('PASS generated state, SDK versions, documentation links, anchors and Python examples')


if __name__ == '__main__':
    main()
