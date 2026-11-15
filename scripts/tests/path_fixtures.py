"""Real short-path fixture for Windows tests; no emulated resolver."""
import ctypes
import os
from pathlib import Path
import unittest


def short_path(path):
    if os.name != 'nt':
        raise unittest.SkipTest('Windows 8.3 filesystem alias')
    function = ctypes.windll.kernel32.GetShortPathNameW
    function.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    function.restype = ctypes.c_uint
    buffer = ctypes.create_unicode_buffer(32768)
    size = function(str(path), buffer, len(buffer))
    if not size or size >= len(buffer):
        raise OSError('cannot obtain Windows filesystem alias')
    result = Path(buffer.value)
    if result == path.resolve():
        raise unittest.SkipTest('filesystem has no distinct 8.3 alias')
    return result
