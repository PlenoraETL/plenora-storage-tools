"""Keep owned Rust tests in cfg(test) child files, without broadening public APIs."""
from pathlib import Path
import re

from rust_source import inline_test_modules, mask_noncode

ROOT = Path(__file__).resolve().parents[1]


def test_files(root=ROOT):
    result = set()
    files = sorted((root / 'crates').glob('plenora-storage-*/src/**/*.rs'))
    for path in files:
        source = path.read_text(encoding='utf-8')
        code = mask_noncode(source)
        pattern = r'#\[cfg\(test\)\]\s*#\[path\s*=\s*"([^"\n]+)"\]\s*mod\s+\w+\s*;'
        for match in re.finditer(pattern, source):
            if code[match.start()] == '#':
                child = (path.parent / match[1]).resolve()
                if not child.is_relative_to((root / 'crates').resolve()) or not child.is_file():
                    raise ValueError('test module path escapes crates or is missing')
                result.add(child)
    return files, result


def check(root=ROOT):
    files, dedicated = test_files(root)
    for path in files:
        if path.resolve() in dedicated:
            continue
        source = path.read_text(encoding='utf-8')
        code = mask_noncode(source)
        if (inline_test_modules(source) or re.search(r'#\[(?:\w+::)*test(?:\([^]]*\))?\]', code)
                or path.name.endswith('_tests.rs')):
            raise ValueError(f'{path.relative_to(root)}: tests must be in cfg(test) child files')
    print(f'PASS Rust test layout: {len(files)} owned files, {len(dedicated)} test modules')


if __name__ == '__main__':
    check()
