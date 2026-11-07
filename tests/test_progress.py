# Copyright (c) 2026, Basalte bv
# Copyright (c) 2026 NXP
#
# SPDX-License-Identifier: Apache-2.0

# Adapted from pdgendt/west 61cc9eb for the standalone renderer and width/error checks.

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from colorama.ansitowin32 import AnsiToWin32

WINDOWS_CONSOLE = r'''
import ctypes, json, sys
from ctypes import wintypes
from pathlib import Path
from west.app._progress import StatusLineReporter

class Coord(ctypes.Structure):
    _fields_ = [('x', wintypes.SHORT), ('y', wintypes.SHORT)]
class Rect(ctypes.Structure):
    _fields_ = [(name, wintypes.SHORT) for name in ('left', 'top', 'right', 'bottom')]
class Info(ctypes.Structure):
    _fields_ = [('size', Coord), ('cursor', Coord), ('attributes', wintypes.WORD),
                ('window', Rect), ('maximum', Coord)]

api = ctypes.WinDLL('kernel32', use_last_error=True)
api.GetStdHandle.argtypes = [wintypes.DWORD]
api.GetStdHandle.restype = wintypes.HANDLE
api.GetConsoleScreenBufferInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Info)]
api.GetConsoleScreenBufferInfo.restype = wintypes.BOOL
api.ReadConsoleOutputCharacterW.argtypes = [
    wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, Coord, ctypes.POINTER(wintypes.DWORD)
]
api.ReadConsoleOutputCharacterW.restype = wintypes.BOOL
handle = api.GetStdHandle(-12)

def snapshot():
    info = Info()
    if not api.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    data = ctypes.create_unicode_buffer(41)
    count = wintypes.DWORD()
    if not api.ReadConsoleOutputCharacterW(
        handle, data, 40, Coord(0, info.cursor.y), ctypes.byref(count)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return {'column': info.cursor.x, 'row': info.cursor.y, 'text': data.value}

assert sys.stderr.isatty()
results = {}
status = StatusLineReporter(sys.stderr, width=40)
for name, text in [('ascii', 'alpha'), ('wide', '\u96ea\u96ea'),
                   ('combining', 'cafe\u0301'), ('uncomposed', 'q\u0301')]:
    status.progress(0, 2, [text])
    results[name] = snapshot()
    status.clear()
    results[name + '_cleared'] = snapshot()
Path(sys.argv[1]).write_text(json.dumps(results), encoding='utf-8')
'''


def reporter(stream, width=None):
    from west.app._progress import StatusLineReporter

    return StatusLineReporter(stream, width=width)


def test_status_line_updates_and_clears():
    stream = io.StringIO()
    status = reporter(stream, width=80)
    status.progress(0, 3, ['alpha', 'beta'])
    assert stream.getvalue() == '\r[0/3] updating: alpha, beta'
    status.progress(0, 3, ['alpha', 'beta'])
    assert stream.getvalue() == '\r[0/3] updating: alpha, beta'

    stream.seek(0)
    stream.truncate()
    status.progress(1, 3, ['beta'])
    assert stream.getvalue() == '\r[1/3] updating: beta' + ' ' * 7 + '\r'

    stream.seek(0)
    stream.truncate()
    status.clear()
    assert stream.getvalue() == '\r' + ' ' * len('[1/3] updating: beta') + '\r'
    stream.seek(0)
    stream.truncate()
    status.close()
    assert stream.getvalue() == ''


@pytest.mark.parametrize('width', [0, 1, 2, 3, 4, 8, 20, 40])
def test_status_line_reserves_last_column(width):
    stream = io.StringIO()
    status = reporter(stream, width=width)
    status.progress(0, 3, ['a-project-name-that-is-longer-than-the-terminal'])
    text = stream.getvalue().replace('\r', '')
    assert len(text) <= max(0, width - 1)
    assert '\n' not in text
    if width > 4:
        assert text.endswith('...')


@pytest.mark.parametrize(
    'name, displayed, cells',
    [
        ('\u96ea\u96ea', '\u96ea\u96ea', 20),
        ('cafe\u0301', 'caf\u00e9', 20),
        ('q\u0301', 'q\u0301', 18),
    ],
)
def test_status_line_counts_display_cells(name, displayed, cells):
    stream = io.StringIO()
    status = reporter(stream, width=21)
    names = [name]
    status.progress(0, 2, names)
    assert names == [name]
    assert stream.getvalue() == '\r[0/2] updating: ' + displayed
    stream.seek(0)
    stream.truncate()
    status.clear()
    assert stream.getvalue() == '\r' + ' ' * cells + '\r'


def test_status_line_truncates_wide_names():
    stream = io.StringIO()
    status = reporter(stream, width=23)
    status.progress(0, 2, ['\u96ea' * 20])
    assert stream.getvalue() == '\r[0/2] updating: \u96ea...'


def test_status_line_escapes_control_characters():
    stream = io.StringIO()
    status = reporter(stream, width=100)
    status.progress(0, 1, ['name\n\x1b[31m\t\r'])
    assert stream.getvalue() == '\r[0/1] updating: name\\n\\x1b[31m\\t\\r'


def test_status_line_handles_terminal_shrinking(monkeypatch):
    from west.app import _progress

    width = 80
    with monkeypatch.context() as scoped:
        scoped.setattr(_progress.shutil, 'get_terminal_size', lambda: os.terminal_size((width, 24)))
        stream = io.StringIO()
        status = reporter(stream)
        status.progress(0, 2, ['a-long-running-project'])
        stream.seek(0)
        stream.truncate()
        width = 10
        status.progress(1, 2, ['beta'])
        status.close()
    assert all(len(part) <= 9 for part in stream.getvalue().split('\r'))


def test_status_line_uses_its_own_terminal_width(monkeypatch):
    from west.app import _progress

    class Terminal(io.StringIO):
        def fileno(self):
            return 123

    def terminal_size(fd):
        assert fd == 123
        return os.terminal_size((10, 24))

    with monkeypatch.context() as scoped:
        scoped.setattr(os, 'get_terminal_size', terminal_size)
        scoped.setattr(_progress.shutil, 'get_terminal_size', lambda: os.terminal_size((120, 24)))
        stream = Terminal()
        status = reporter(stream)
        status.progress(0, 2, ['a-long-project-name'])
        status.close()
    assert all(len(part) <= 9 for part in stream.getvalue().split('\r'))


@pytest.mark.parametrize('failure', ['write', 'flush', 'short-write', 'closed'])
def test_status_line_io_errors_are_visible(failure):
    class Broken(io.StringIO):
        def write(self, text):
            if failure == 'write':
                raise OSError('status write failed')
            if failure == 'closed':
                raise ValueError('status stream closed')
            if failure == 'short-write':
                return super().write(text[:-1])
            return super().write(text)

        def flush(self):
            if failure == 'flush':
                raise OSError('status flush failed')
            return super().flush()

    status = reporter(Broken(), width=80)
    with pytest.raises((OSError, ValueError), match='status|short write'):
        status.progress(0, 2, ['alpha'])


def test_status_line_clear_failure_is_not_swallowed():
    class Broken(io.StringIO):
        fail = False

        def write(self, text):
            if self.fail:
                raise OSError('status clear failed')
            return super().write(text)

    stream = Broken()
    status = reporter(stream, width=80)
    status.progress(0, 2, ['alpha'])
    stream.fail = True
    with pytest.raises(OSError, match='status clear failed'):
        status.close()
    stream.fail = False
    status.close()


def test_status_line_accepts_colorama_stream():
    stream = io.StringIO()
    wrapped = AnsiToWin32(stream, strip=True, convert=False).stream
    status = reporter(wrapped, width=80)
    status.progress(0, 2, ['alpha'])
    assert stream.getvalue() == '\r[0/2] updating: alpha'
    status.close()
    assert stream.getvalue().endswith('\r' + ' ' * len('[0/2] updating: alpha') + '\r')


@pytest.mark.skipif(sys.platform != 'win32', reason='native Windows console cells')
def test_status_line_native_windows_console(tmp_path):
    from west.app import _progress

    driver = tmp_path / 'console.py'
    evidence = tmp_path / 'console.json'
    driver.write_text(WINDOWS_CONSOLE, encoding='utf-8')
    env = dict(os.environ, PYTHONPATH=os.fspath(Path(_progress.__file__).parents[2]))
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    subprocess.run(
        [getattr(sys, '_base_executable', sys.executable), driver, evidence],
        env=env,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        startupinfo=startup,
        check=True,
        timeout=30,
    )
    snapshots = json.loads(evidence.read_text(encoding='utf-8'))
    assert snapshots['ascii']['column'] == 21
    assert snapshots['wide']['column'] == snapshots['combining']['column'] == 20
    assert snapshots['uncomposed']['column'] == 18
    assert len({value['row'] for value in snapshots.values()}) == 1
    for name in ('ascii', 'wide', 'combining', 'uncomposed'):
        cleared = snapshots[name + '_cleared']
        assert cleared['column'] == 0
        assert not cleared['text'].strip()
