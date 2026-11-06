# Copyright (c) 2026, Basalte bv
# Copyright (c) 2026 NXP
#
# SPDX-License-Identifier: Apache-2.0

"""Terminal status display adapted from pdgendt/west commit 61cc9eb.

Modified for file-backed project output, display-cell widths and explicit I/O errors.
"""

import os
import shutil
import unicodedata
from collections.abc import Iterable
from typing import TextIO


def _cells(character: str) -> int:
    # Uncomposed marks occupy a cell in legacy Windows consoles.
    return 2 if unicodedata.east_asian_width(character) in ('W', 'F') else 1


def _truncate(text: str, limit: int) -> tuple[str, int]:
    width = sum(map(_cells, text))
    if width <= limit:
        return text, width
    suffix = '.' * min(3, limit)
    budget = limit - len(suffix)
    prefix = []
    width = 0
    for character in text:
        cells = _cells(character)
        if width + cells > budget:
            break
        prefix.append(character)
        width += cells
    return ''.join(prefix) + suffix, width + len(suffix)


class StatusLineReporter:
    def __init__(self, stream: TextIO, width: int | None = None) -> None:
        self._stream = stream
        self._width = width
        self._shown = ''
        self._shown_cells = 0

    def _limit(self) -> int:
        if self._width is not None:
            columns = self._width
        else:
            try:
                columns = os.get_terminal_size(self._stream.fileno()).columns
            except (AttributeError, OSError, ValueError):
                # Text-only wrappers need the standard environment/default fallback.
                columns = shutil.get_terminal_size().columns
        return max(0, columns - 1)

    def _write(self, text: str) -> None:
        written = self._stream.write(text)
        # Colorama's text writer does not return a character count.
        if written is not None and written != len(text):
            raise OSError('short write displaying update status')
        self._stream.flush()

    def clear(self) -> None:
        if self._shown:
            self._write('\r' + ' ' * min(self._shown_cells, self._limit()) + '\r')
            self._shown = ''
            self._shown_cells = 0

    def progress(self, done: int, total: int, in_flight: Iterable[str]) -> None:
        names = ', '.join(unicodedata.normalize('NFC', name) for name in in_flight)
        names = ''.join(
            character
            if character.isprintable()
            else character.encode('unicode_escape').decode('ascii')
            for character in names
        )
        limit = self._limit()
        line, cells = _truncate(f'[{done}/{total}] updating: {names}', limit)
        if line == self._shown:
            return
        if self._shown_cells > limit:
            self.clear()
        padding = ' ' * max(0, self._shown_cells - cells)
        self._shown = line
        self._shown_cells = cells
        self._write('\r' + line + padding + ('\r' if padding else ''))

    def close(self) -> None:
        self.clear()
