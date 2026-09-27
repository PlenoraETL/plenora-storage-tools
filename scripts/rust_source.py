"""Lexical helpers for source layout checks, preserving offsets and newlines.

These helpers do not type-check Rust. Cargo remains the syntax and semantic
authority; the scanner only keeps comments and literal braces out of layout
decisions. Lifetimes are code, while character literals are masked.
"""
import re

RAW = re.compile(r'(?:br|cr|r)(#*)"')
CHARACTER = re.compile(r"'(?:\\(?:u\{[0-9a-fA-F_]+\}|x[0-9a-fA-F]{2}|.)|[^\\'\n])'")


def mask_noncode(source, comments=None):
    masked = list(source)
    index = 0

    def hide(start, end):
        for position in range(start, end):
            if source[position] != '\n':
                masked[position] = ' '

    while index < len(source):
        start = index
        comment = False
        if source.startswith('//', index):
            comment = True
            end = source.find('\n', index)
            index = len(source) if end < 0 else end
        elif source.startswith('/*', index):
            comment = True
            depth, index = 1, index + 2
            while index < len(source) and depth:
                if source.startswith('/*', index):
                    depth, index = depth + 1, index + 2
                elif source.startswith('*/', index):
                    depth, index = depth - 1, index + 2
                else:
                    index += 1
            if depth:
                raise ValueError('unterminated Rust block comment')
        elif raw := RAW.match(source, index):
            closing = '"' + raw[1]
            end = source.find(closing, raw.end())
            if end < 0:
                raise ValueError('unterminated Rust raw string')
            index = end + len(closing)
        elif source[index] == '"':
            index += 1
            while index < len(source) and source[index] != '"':
                index += 2 if source[index] == '\\' else 1
            if index >= len(source):
                raise ValueError('unterminated Rust string')
            index += 1
        elif character := CHARACTER.match(source, index):
            index = character.end()
        else:
            index += 1
            continue
        hide(start, index)
        if comment and comments is not None:
            comments.append((start, source[start:index]))
    return ''.join(masked)


def inline_test_modules(source):
    code = mask_noncode(source)
    pattern = r'#\s*\[\s*cfg\s*\(\s*test\s*\)\s*\]\s*mod\s+(\w+)\s*\{'
    result = []
    for match in re.finditer(pattern, code):
        opening = match.end() - 1
        depth = 1
        closing = opening + 1
        while closing < len(code) and depth:
            depth += (code[closing] == '{') - (code[closing] == '}')
            closing += 1
        if depth:
            raise ValueError('unbalanced inline test module')
        result.append((match.start(), opening, closing, match[1]))
    return result
