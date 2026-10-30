# Copyright (c) 2026 NXP
#
# SPDX-License-Identifier: Apache-2.0

"""Private output and Git-child ownership for opted-in project updates."""

from __future__ import annotations

import codecs
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from concurrent.futures import CancelledError
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from typing import IO, TextIO

from colorama.ansitowin32 import AnsiToWin32, StreamWrapper

from west.util import PathType

_current: ContextVar[ProjectContext | None] = ContextVar('west_project_execution', default=None)
_INCOMPLETE_ANSI = re.compile(r'\x1b(?:\[[0-9;]*|\][^\x07]*)?\Z')


if sys.platform == 'win32':
    import ctypes
    from ctypes import wintypes

    class _ThreadEntry(ctypes.Structure):
        _fields_ = [
            ('dwSize', wintypes.DWORD),
            ('cntUsage', wintypes.DWORD),
            ('th32ThreadID', wintypes.DWORD),
            ('th32OwnerProcessID', wintypes.DWORD),
            ('tpBasePri', wintypes.LONG),
            ('tpDeltaPri', wintypes.LONG),
            ('dwFlags', wintypes.DWORD),
        ]

    class _WindowsJob:
        def __init__(self) -> None:
            self._api = ctypes.WinDLL('kernel32', use_last_error=True)
            self._api.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
            self._api.CreateJobObjectW.restype = wintypes.HANDLE
            self._api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self._api.OpenProcess.restype = wintypes.HANDLE
            self._api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            self._api.AssignProcessToJobObject.restype = wintypes.BOOL
            self._api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            self._api.TerminateJobObject.restype = wintypes.BOOL
            self._api.CloseHandle.argtypes = [wintypes.HANDLE]
            self._api.CloseHandle.restype = wintypes.BOOL
            self._api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
            self._api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            self._api.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
            self._api.Thread32First.restype = wintypes.BOOL
            self._api.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
            self._api.Thread32Next.restype = wintypes.BOOL
            self._api.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self._api.OpenThread.restype = wintypes.HANDLE
            self._api.GetProcessIdOfThread.argtypes = [wintypes.HANDLE]
            self._api.GetProcessIdOfThread.restype = wintypes.DWORD
            self._api.ResumeThread.argtypes = [wintypes.HANDLE]
            self._api.ResumeThread.restype = wintypes.DWORD
            self._handle = self._api.CreateJobObjectW(None, None)
            if not self._handle:
                raise ctypes.WinError(ctypes.get_last_error())

        def add(self, process: subprocess.Popen[bytes]) -> None:
            # AssignProcessToJobObject requires SET_QUOTA and TERMINATE access.
            handle = self._api.OpenProcess(0x0101, False, process.pid)
            if not handle:
                if process.poll() is not None:
                    return
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not self._api.AssignProcessToJobObject(self._handle, handle):
                    error = ctypes.get_last_error()
                    if process.poll() is None:
                        raise ctypes.WinError(error)
            finally:
                self._close_handle(handle)

        def resume(self, process: subprocess.Popen[bytes]) -> None:
            # Popen closes the primary thread handle; find its suspended thread.
            snapshot = self._api.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
            if snapshot == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            threads = []
            try:
                entry = _ThreadEntry()
                entry.dwSize = ctypes.sizeof(entry)
                more = self._api.Thread32First(snapshot, ctypes.byref(entry))
                while more:
                    if entry.th32OwnerProcessID == process.pid:
                        threads.append(entry.th32ThreadID)
                    entry.dwSize = ctypes.sizeof(entry)
                    more = self._api.Thread32Next(snapshot, ctypes.byref(entry))
                if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                self._close_handle(snapshot)
            if len(threads) != 1:
                raise OSError(f'cannot identify the suspended Git thread for PID {process.pid}')
            # THREAD_QUERY_LIMITED_INFORMATION | THREAD_SUSPEND_RESUME
            thread = self._api.OpenThread(0x0802, False, threads[0])
            if not thread:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if self._api.GetProcessIdOfThread(thread) != process.pid:
                    raise OSError(f'suspended Git thread ownership changed for PID {process.pid}')
                if process.poll() is not None:
                    raise OSError(
                        f'suspended Git process exited before resuming: PID {process.pid}'
                    )
                count = self._api.ResumeThread(thread)
                if count == 0xFFFFFFFF:
                    raise ctypes.WinError(ctypes.get_last_error())
                if count != 1:
                    raise OSError(f'unexpected Git thread suspend count: {count}')
            finally:
                self._close_handle(thread)

        def _close_handle(self, handle: int) -> None:
            original = sys.exc_info()[1]
            if not self._api.CloseHandle(handle):
                error = ctypes.WinError(ctypes.get_last_error())
                if original is not None:
                    raise original from error
                raise error

        def terminate(self) -> None:
            if not self._api.TerminateJobObject(self._handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())

        def close(self) -> None:
            if self._handle:
                if not self._api.CloseHandle(self._handle):
                    raise ctypes.WinError(ctypes.get_last_error())
                self._handle = None


class ProcessController:
    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._children: set[subprocess.Popen[bytes]] = set()
        if sys.platform == 'win32':
            self._job: _WindowsJob | None = _WindowsJob()
        else:
            self._job = None

    def check(self) -> None:
        if self._cancelled.is_set():
            raise CancelledError()

    def popen(
        self,
        args: list[str],
        cwd: PathType,
        stdout: int | IO[bytes] | None,
        stderr: int | IO[bytes] | None,
    ) -> subprocess.Popen[bytes]:
        with self._lock:
            self.check()
            if sys.platform == 'win32':
                process = subprocess.Popen(
                    args,
                    cwd=cwd,
                    stdout=stdout,
                    stderr=stderr,
                    # CREATE_SUSPENDED prevents helpers escaping before job assignment.
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004,
                )
            else:
                process = subprocess.Popen(
                    args,
                    cwd=cwd,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
            self._children.add(process)
            if self._job is not None:
                self._job.add(process)
                self._job.resume(process)
            return process

    def finished(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._children.discard(process)

    def cancel(self) -> None:
        with self._lock:
            self._cancelled.set()
            children = tuple(self._children)
        errors: list[Exception] = []
        if sys.platform == 'win32':
            assert self._job is not None
            try:
                self._job.terminate()
            except OSError as error:
                errors.append(error)
            for process in children:
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError as error:
                        errors.append(error)
        else:

            def signal_groups(sig: int) -> None:
                for process in children:
                    try:
                        os.killpg(process.pid, sig)
                    except ProcessLookupError:
                        pass
                    except OSError as error:
                        errors.append(error)

            signal_groups(signal.SIGTERM)
            deadline = time.monotonic() + 0.25
            for process in children:
                try:
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    pass
            # The group may outlive its leader, so always stop remaining members.
            signal_groups(signal.SIGKILL)
        for process in children:
            try:
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired) as error:
                errors.append(error)
            else:
                self.finished(process)
        if errors:
            raise OSError(
                'could not stop owned Git children: ' + '; '.join(map(str, errors))
            ) from errors[0]

    def close(self) -> None:
        if self._job is not None:
            self._job.close()


class _ProjectOutput:
    def __init__(self, file: IO[bytes]) -> None:
        self.file = file
        # Unmarked ranges are native child bytes, not UTF-8-encoded Python text.
        self._text: list[tuple[int, int]] = []

    def write(self, text: str) -> int:
        data = text.encode('utf-8', errors='surrogatepass')
        start = self.file.tell()
        if self.file.write(data) != len(data):
            raise OSError('short write to project output spool')
        end = self.file.tell()
        if data:
            if self._text and self._text[-1][1] == start:
                self._text[-1] = self._text[-1][0], end
            else:
                self._text.append((start, end))
        return len(text)

    def flush(self) -> None:
        self.file.flush()

    def replay(self, destination: TextIO) -> None:
        self.flush()
        end = self.file.tell()
        self.file.seek(0)
        position = 0
        for start, stop in self._text:
            self._copy(destination, start - position, python=False)
            self._copy(destination, stop - start, python=True)
            position = stop
        self._copy(destination, end - position, python=False)
        destination.flush()

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
            decoder = codecs.getincrementaldecoder(encoding)(
                errors='surrogatepass' if python else 'backslashreplace'
            )
        else:
            destination.flush()
        pending = ''
        while length:
            data = self.file.read(min(length, 65536))
            if not data:
                raise OSError('incomplete project output spool')
            length -= len(data)
            if python or binary is None:
                text = pending + decoder.decode(data, final=not length)
                pending = ''
                if length and (isinstance(destination, StreamWrapper) or converter is not None):
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


class ProjectContext:
    def __init__(
        self,
        controller: ProcessController,
        *,
        buffered: bool,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        self._controller = controller
        self._stdout = sys.stdout if stdout is None else stdout
        self._stderr = sys.stderr if stderr is None else stderr
        self._out: _ProjectOutput | None = None
        self._err: _ProjectOutput | None = None
        self._replayed = False
        with ExitStack() as files:
            if buffered:
                self._out = _ProjectOutput(
                    files.enter_context(tempfile.TemporaryFile('w+b', buffering=0))
                )
                self._err = _ProjectOutput(
                    files.enter_context(tempfile.TemporaryFile('w+b', buffering=0))
                )
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
        for output in (self._out, self._err):
            if output is not None:
                output.flush()
        stdout = subprocess.PIPE if capture_stdout else self._out.file if self._out else None
        stderr = subprocess.PIPE if capture_stderr else self._err.file if self._err else None
        process = self._controller.popen(args, cwd, stdout, stderr)
        out, err = process.communicate()
        self._controller.finished(process)
        return process, out, err

    def replay(self) -> None:
        if not self._replayed:
            self._replayed = True
            if self._out is not None:
                self._out.replay(self._stdout)
            if self._err is not None:
                self._err.replay(self._stderr)

    def close(self) -> None:
        self._files.close()


def current_context() -> ProjectContext | None:
    return _current.get()


def output_stream(*, stderr: bool = False) -> TextIO | _ProjectOutput:
    context = current_context()
    if context is not None:
        return context.output(stderr)
    return sys.stderr if stderr else sys.stdout
