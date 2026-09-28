"""Extract hash comments while excluding quoted and embedded scalar data.

This is a lexical policy helper, not a shell interpreter. Shell here-documents,
PowerShell here-strings and YAML block scalars are treated as data; embedded
programs require their own source checks. Parsers/build tools validate syntax.
"""
import re


def comments(source, suffix):
    result = []
    quote = None
    block = None
    block_line = 0
    block_text = []
    heredocs = []
    scalar_indent = None
    powershell = suffix == '.ps1'
    yaml = suffix in {'.yml', '.yaml'}
    for number, line in enumerate(source.splitlines(), 1):
        if heredocs:
            delimiter, strip_tabs = heredocs[0]
            if (line.lstrip('\t') if strip_tabs else line) == delimiter:
                heredocs.pop(0)
            continue
        if scalar_indent is not None:
            if not line.strip() or len(line) - len(line.lstrip()) > scalar_indent:
                continue
            scalar_indent = None
        index = 0
        while index < len(line):
            if block:
                end = line.find(block, index)
                if block == '#>':
                    block_text.append(line[index:] if end < 0 else line[index:end])
                if end < 0:
                    break
                if block == '#>':
                    result.append((block_line, '\n'.join(block_text)))
                    block_text = []
                index = end + len(block)
                block = None
                continue
            if quote:
                if line.startswith(quote, index):
                    if quote == "'" and powershell and line.startswith("''", index):
                        index += 2
                        continue
                    index += len(quote)
                    quote = None
                elif line[index] == ('`' if powershell else '\\') and quote != "'":
                    index += 2
                else:
                    index += 1
                continue
            if powershell and line.startswith('<#', index):
                block, block_line = '#>', number
                index += 2
                continue
            if powershell and line[index:].rstrip() in {"@'", '@"'}:
                block = line[index + 1] + '@'
                break
            if suffix == '.sh' and (match := re.match(r"<<(-?)\s*(['\"]?)([\w]+)\2", line[index:])):
                heredocs.append((match[3], bool(match[1])))
                index += match.end()
                continue
            if suffix == '.toml' and line[index:index + 3] in {'"""', "'''"}:
                quote = line[index:index + 3]
                index += 3
                continue
            char = line[index]
            if char in {'"', "'"} and (not yaml or index == 0 or line[index - 1] in ' :,[{-'):
                quote = char
            elif char == '#' and (suffix == '.toml' or powershell or index == 0 or line[index - 1].isspace() or line[index - 1] in ';|&()'):
                result.append((number, line[index:]))
                break
            elif char == ('`' if powershell else '\\') and not yaml:
                index += 1
            elif yaml and char in '|>' and re.fullmatch(r'[|>][+-]?[1-9]?\s*(?:#.*)?', line[index:]):
                scalar_indent = len(line) - len(line.lstrip())
            index += 1
    if block == '#>':
        raise ValueError('unterminated PowerShell block comment')
    return result
