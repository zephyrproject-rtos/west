# Copyright 2026 NXP
#
# SPDX-License-Identifier: Apache-2.0

"""Private ordered output contexts for opted-in project updates."""

from __future__ import annotations

import codecs
import re
import struct
import subprocess
import sys
import tempfile
import threading
from _thread import LockType
from collections.abc import Iterator
from concurrent.futures import CancelledError
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from io import BufferedReader
from typing import IO, TextIO

from colorama.ansitowin32 import AnsiToWin32, StreamWrapper

from west.util import PathType

_current: ContextVar[ProjectContext | None] = ContextVar('west_project_execution', default=None)
_INCOMPLETE_ANSI = re.compile(r'\x1b(?:\[[0-9;]*|\][^\x07]*)?\Z')
_OUTPUT_RECORD = struct.Struct('!BQ')


class _ProjectOutput:
    def __init__(
        self, file: IO[bytes], lock: LockType | None = None, *, stderr: bool = False
    ) -> None:
        self.file = file
        self._lock = threading.Lock() if lock is None else lock
        self._stderr = stderr
        self._decoder: codecs.IncrementalDecoder | None = None
        self._pending = ''

    def write(self, text: str) -> int:
        self.write_bytes(text.encode('utf-8', errors='surrogatepass'), python=True)
        return len(text)

    def write_bytes(self, data: bytes, *, python: bool = False) -> None:
        if data:
            record = _OUTPUT_RECORD.pack(int(self._stderr) | (int(python) << 1), len(data))
            with self._lock:
                if self.file.write(record) != len(record) or self.file.write(data) != len(data):
                    raise OSError('short write to project output spool')

    def flush(self) -> None:
        with self._lock:
            self.file.flush()

    def replay(self, destination: TextIO, stderr: TextIO | None = None) -> None:
        self.flush()
        end = self.file.tell()
        self.file.seek(0)
        destinations = destination, destination if stderr is None else stderr
        renderers = self, _ProjectOutput(self.file)
        previous: int | None = None
        while self.file.tell() < end:
            header = self.file.read(_OUTPUT_RECORD.size)
            if len(header) != _OUTPUT_RECORD.size:
                raise OSError('incomplete project output spool')
            tag, length = _OUTPUT_RECORD.unpack(header)
            if tag > 3 or length > end - self.file.tell():
                raise OSError('invalid project output spool record')
            stream = tag & 1
            if previous is not None and previous != stream:
                destinations[previous].flush()
            renderers[stream]._copy(destinations[stream], length, python=bool(tag & 2))
            previous = stream
        for renderer, stream in zip(renderers, destinations, strict=True):
            if renderer._decoder is not None:
                tail = renderer._pending + renderer._decoder.decode(b'', final=True)
                if tail:
                    stream.write(tail)
            stream.flush()

    def _copy(self, destination: TextIO, length: int, *, python: bool) -> None:
        if not length:
            return
        binary = getattr(destination, 'buffer', None)
        converter = None
        if (
            not python
            and binary is not None
            and isinstance(destination, StreamWrapper)
            and sys.platform == 'win32'
        ):
            renderer = AnsiToWin32(destination, strip=False)
            if renderer.convert:
                converter = renderer
                binary = None
        if python or binary is None:
            encoding = 'utf-8' if python else getattr(destination, 'encoding', None) or 'utf-8'
            if python:
                decoder = codecs.getincrementaldecoder(encoding)(errors='surrogatepass')
            else:
                if self._decoder is None:
                    self._decoder = codecs.getincrementaldecoder(encoding)(
                        errors='backslashreplace'
                    )
                decoder = self._decoder
        else:
            destination.flush()
        pending = '' if python else self._pending
        while length:
            data = self.file.read(min(length, 65536))
            if not data:
                raise OSError('incomplete project output spool')
            length -= len(data)
            if python or binary is None:
                text = pending + decoder.decode(data, final=python and not length)
                pending = ''
                if (length or not python) and (
                    isinstance(destination, StreamWrapper) or converter is not None
                ):
                    partial = _INCOMPLETE_ANSI.search(text)
                    if partial is not None:
                        pending = text[partial.start() :]
                        text = text[: partial.start()]
                if converter is not None:
                    converter.write(text)
                else:
                    destination.write(text)
            elif binary.write(data) != len(data):
                raise OSError('short write replaying project output')
        if not python:
            self._pending = pending


class ProjectContext:
    def __init__(
        self,
        cancelled: threading.Event,
        *,
        buffered: bool,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        self._cancelled = cancelled
        self._stdout = sys.stdout if stdout is None else stdout
        self._stderr = sys.stderr if stderr is None else stderr
        self._out: _ProjectOutput | None = None
        self._err: _ProjectOutput | None = None
        self._replayed = False
        with ExitStack() as files:
            if buffered:
                file = files.enter_context(tempfile.TemporaryFile('w+b', buffering=0))
                lock = threading.Lock()
                self._out = _ProjectOutput(file, lock)
                self._err = _ProjectOutput(file, lock, stderr=True)
            self._files = files.pop_all()

    @contextmanager
    def activate(self) -> Iterator[ProjectContext]:
        token = _current.set(self)
        try:
            yield self
        finally:
            _current.reset(token)

    def output(self, stderr: bool) -> TextIO | _ProjectOutput:
        if stderr:
            return self._err if self._err is not None else self._stderr
        return self._out if self._out is not None else self._stdout

    def git(
        self,
        args: list[str],
        cwd: PathType,
        capture_stdout: bool,
        capture_stderr: bool,
    ) -> tuple[subprocess.Popen[bytes], bytes | None, bytes | None]:
        if self._cancelled.is_set():
            raise CancelledError()
        for output in (self._out, self._err):
            if output is not None:
                output.flush()
        stdout = subprocess.PIPE if capture_stdout or self._out is not None else None
        stderr = subprocess.PIPE if capture_stderr or self._err is not None else None
        process = subprocess.Popen(args, cwd=cwd, stdout=stdout, stderr=stderr)
        readers: list[threading.Thread] = []
        errors: list[OSError | ValueError] = []
        error_lock = threading.Lock()

        def read(pipe: BufferedReader, output: _ProjectOutput) -> None:
            try:
                with pipe:
                    while data := pipe.read1(65536):
                        output.write_bytes(data)
            except (OSError, ValueError) as error:
                with error_lock:
                    errors.append(error)
                self._cancelled.set()

        try:
            for name, output, capture in (
                ('stdout', self._out, capture_stdout),
                ('stderr', self._err, capture_stderr),
            ):
                if output is not None and not capture:
                    pipe = getattr(process, name)
                    reader = threading.Thread(
                        target=read, args=(pipe, output), name=f'west-output-{name}'
                    )
                    reader.start()
                    readers.append(reader)
                    setattr(process, name, None)
            out, err = process.communicate()
        except (OSError, ValueError, RuntimeError, KeyboardInterrupt) as original:
            self._cancelled.set()
            try:
                process.communicate()
            except (OSError, ValueError) as cleanup_error:
                raise original from cleanup_error
            raise
        finally:
            for reader in readers:
                reader.join()
        if errors:
            raise OSError(
                'could not buffer Git output: ' + '; '.join(map(str, errors))
            ) from errors[0]
        return process, out, err

    def replay(self) -> None:
        if not self._replayed:
            self._replayed = True
            if self._out is not None:
                self._out.replay(self._stdout, self._stderr)

    def close(self) -> None:
        self._files.close()


def current_context() -> ProjectContext | None:
    return _current.get()


def output_stream(*, stderr: bool = False) -> TextIO | _ProjectOutput:
    context = current_context()
    if context is not None:
        return context.output(stderr)
    return sys.stderr if stderr else sys.stdout
