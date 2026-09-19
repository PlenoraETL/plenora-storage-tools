"""Check generated state, Python version alignment and repository-local Markdown links."""
import re
from pathlib import Path
import tomllib
from urllib.parse import unquote
from render_state import ROOT, render
from versioning import workspace_version


def main():
    expected = render()
    assert (ROOT / 'docs/STATO.md').read_text(encoding='utf-8') == expected, 'run scripts/render_state.py'
    version = workspace_version()
    pyproject = tomllib.loads((ROOT / 'crates/plenora-storage-py/pyproject.toml').read_text())
    assert pyproject['project']['version'] == version.python, 'Python and native versions differ'
    paths = [ROOT / 'README.md', ROOT / 'AGENTS.md', *sorted((ROOT / 'docs').glob('*.md'))]
    for path in paths:
        for link in re.findall(r'\]\(([^)]+)\)', path.read_text(encoding='utf-8')):
            if '://' in link or link.startswith(('#', 'mailto:')):
                continue
            target = unquote(link.split('#', 1)[0]).strip('<>')
            if target:
                assert (path.parent / target).exists(), f'{path.relative_to(ROOT)}: broken link {link}'
    print('PASS generated state, SDK versions and documentation links')


if __name__ == '__main__':
    main()
