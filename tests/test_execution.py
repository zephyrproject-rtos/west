# Copyright 2026 NXP

import ctypes
import io
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from concurrent.futures import CancelledError
from pathlib import Path
from types import SimpleNamespace

import pytest
from colorama.ansitowin32 import AnsiToWin32
from conftest import cmd, create_repo

from west.app.main import LogHandler
from west.app.project import Update
from west.commands import Verbosity, WestCommand
from west.manifest import Project


class OutputCommand(WestCommand):
    def do_add_parser(self, parser_adder):
        pass

    def do_run(self, args, unknown):
        pass


@pytest.fixture(autouse=True)
def quiet_manifest_logger():
    logger = logging.getLogger('west.manifest')
    level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        logger.setLevel(level)


def project_at(path):
    create_repo(os.fspath(path))
    return Project('output-test', 'unused', path=path.name, topdir=path.parent)


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail('readiness deadline exceeded')
        threading.Event().wait(0.02)


def process_active(pid):
    if sys.platform == 'win32':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:
                return False
            raise ctypes.WinError(error)
        try:
            status = kernel.WaitForSingleObject(handle, 0)
            if status not in (0, 258):
                raise ctypes.WinError(ctypes.get_last_error())
            return status == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f'/proc/{pid}/stat')
    if stat.exists():
        try:
            return not stat.read_text().split(') ', 1)[1].startswith('Z')
        except FileNotFoundError:
            return False
    return True


def test_cli_helper_restores_manifest_logging(caplog):
    logger = logging.getLogger('west.manifest')
    level, handlers = logger.level, list(logger.handlers)
    cmd(['-q', 'config', '--global', 'test.execution-isolation', 'true'])
    assert logger.level == level
    assert logger.handlers == handlers
    logger.warning('manifest logging remains available to the next test')
    assert 'manifest logging remains available to the next test' in caplog.text


def test_buffered_producers_keep_stream_ownership(tmp_path, monkeypatch):
    from west import _execution

    project = project_at(tmp_path / 'repo space \u96ea')
    out, err = io.StringIO(), io.StringIO()
    controller = threading.Event()
    scope = _execution.ProjectContext(controller, buffered=True, stdout=out, stderr=err)
    command = OutputCommand('output', 'output', 'output')
    command.verbosity = Verbosity.DBG_EXTREME
    logger = logging.getLogger('west.manifest')
    handler = LogHandler()
    handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
    monkeypatch.setattr(logger, 'handlers', [handler])
    monkeypatch.setattr(logger, 'propagate', False)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    original = sys.stdout, sys.stderr, Path.cwd(), dict(os.environ)
    try:
        with scope.activate():
            assert _execution.current_context() is scope
            command.inf('python-start \u96ea')
            logger.info('manifest-out')
            logger.warning('manifest-err')
            project.git(['-c', "alias.emit=!printf 'git-out\\n'; printf 'git-err\\n' >&2", 'emit'])
            private = project.git(
                ['-c', "alias.emit=!printf 'private-out\\n'; printf 'private-err\\n' >&2", 'emit'],
                capture_stdout=True,
                capture_stderr=True,
            )
            command.inf('python-end')
            command.wrn('python-warning')
        assert _execution.current_context() is None
        assert out.getvalue() == err.getvalue() == ''
        assert private.stdout == b'private-out\n'
        assert private.stderr == b'private-err\n'
        scope.replay()
        stdout, stderr = out.getvalue(), err.getvalue()
        for marker in ['python-start \u96ea\n', 'manifest-out\n', 'git-out\n', 'python-end\n']:
            assert stdout.count(marker) == 1
        positions = [
            stdout.index(s)
            for s in ['python-start \u96ea\n', 'manifest-out\n', 'git-out\n', 'python-end\n']
        ]
        assert positions == sorted(positions)
        assert stderr.index('manifest-err\n') < stderr.index('git-err\n')
        assert stderr.index('git-err\n') < stderr.index('python-warning\n')
        assert 'private-out\n' not in stdout
        assert 'private-err\n' not in stderr
        assert "b'private-out\\n'" in stdout
        assert 'git-err\n' not in stdout
        assert 'git-out\n' not in stderr
        assert (sys.stdout, sys.stderr, Path.cwd(), dict(os.environ)) == original
    finally:
        logger.setLevel(old_level)
        controller.set()
        scope.close()


def test_git_inherits_foreground_console(tmp_path, monkeypatch):
    from west import _execution

    project = project_at(tmp_path / 'foreground')
    controller = threading.Event()
    scope = _execution.ProjectContext(controller, buffered=False)
    calls = []
    original = subprocess.Popen

    def popen(*args, **kwargs):
        calls.append(kwargs.copy())
        return original(*args, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', popen)
    try:
        with scope.activate():
            result = project.git(['rev-parse', 'HEAD'], capture_stdout=True)
        assert len(result.stdout.strip()) == 40
        assert len(calls) == 1
        assert not calls[0].get('start_new_session', False)
        assert not calls[0].get('creationflags', 0)
    finally:
        controller.set()
        scope.close()


def test_cancelled_context_does_not_start_git(tmp_path, monkeypatch):
    from west import _execution

    project = project_at(tmp_path / 'cancelled')
    cancelled = threading.Event()
    scope = _execution.ProjectContext(cancelled, buffered=False)
    cancelled.set()

    def unexpected_popen(*args, **kwargs):
        pytest.fail('cancelled work started a Git process')

    monkeypatch.setattr(subprocess, 'Popen', unexpected_popen)
    with scope.activate(), pytest.raises(CancelledError):
        project.git(['rev-parse', 'HEAD'], capture_stdout=True)
    scope.close()


def test_buffered_output_preserves_order_between_streams(tmp_path):
    from west import _execution

    events = []

    class Stream(io.StringIO):
        def write(self, text):
            events.append(text)
            return super().write(text)

    project = project_at(tmp_path / 'ordered')
    controller = threading.Event()
    scope = _execution.ProjectContext(controller, buffered=True, stdout=Stream(), stderr=Stream())
    command = OutputCommand('output', 'output', 'output')
    try:
        with scope.activate():
            command.wrn('before-git')
            project.git(['-c', "alias.emit=!printf 'git-output\\n'", 'emit'])
            command.wrn('after-git')
        assert events == []
        scope.replay()
        output = ''.join(events)
        assert output.index('before-git') < output.index('git-output')
        assert output.index('git-output') < output.index('after-git')
    finally:
        controller.set()
        scope.close()


def test_buffered_git_output_spools_in_bounded_memory(tmp_path):
    from west import _execution

    project = project_at(tmp_path / 'large-output')
    size = 32 * 1024 * 1024
    (Path(project.abspath) / 'output.bin').write_bytes(b'x' * size)

    class Discard(io.StringIO):
        def write(self, text):
            return len(text)

    scope = _execution.ProjectContext(
        threading.Event(), buffered=True, stdout=Discard(), stderr=Discard()
    )
    try:
        tracemalloc.start()
        with scope.activate():
            project.git(['-c', 'alias.emit=!cat output.bin', 'emit'])
        scope.replay()
        _, peak = tracemalloc.get_traced_memory()
        assert peak < 4 * 1024 * 1024
    finally:
        tracemalloc.stop()
        scope.close()


def test_ordered_replay_flushes_real_stream_buffers(tmp_path):
    from west import _execution

    merged = bytearray()

    class Raw(io.RawIOBase):
        def writable(self):
            return True

        def write(self, data):
            merged.extend(data)
            return len(data)

    project = project_at(tmp_path / 'merged-output')
    with (
        io.TextIOWrapper(io.BufferedWriter(Raw()), encoding='utf-8', newline='') as stdout,
        io.TextIOWrapper(io.BufferedWriter(Raw()), encoding='utf-8', newline='') as stderr,
    ):
        scope = _execution.ProjectContext(
            threading.Event(), buffered=True, stdout=stdout, stderr=stderr
        )
        command = OutputCommand('output', 'output', 'output')
        try:
            with scope.activate():
                command.inf('before-git')
                project.git(['-c', "alias.emit=!printf 'from-git\\n' >&2", 'emit'])
                command.inf('after-git')
            scope.replay()
            assert merged == b'before-git\nfrom-git\nafter-git\n'
        finally:
            scope.close()


def test_python_text_and_raw_child_bytes_keep_distinct_encodings(tmp_path):
    from west import _execution

    project = project_at(tmp_path / 'raw')
    data = io.BytesIO()
    out = io.TextIOWrapper(data, encoding='ascii', errors='backslashreplace')
    controller = threading.Event()
    scope = _execution.ProjectContext(controller, buffered=True, stdout=out, stderr=io.StringIO())
    command = OutputCommand('output', 'output', 'output')
    try:
        with scope.activate():
            command.inf('python \u96ea')
            project.git(['-c', "alias.emit=!printf '\\377child\\n'", 'emit'])
            command.inf('after')
        scope.replay()
        out.flush()
        newline = os.linesep.encode('ascii')
        assert data.getvalue() == b'python \\u96ea' + newline + b'\xffchild\nafter' + newline
    finally:
        controller.set()
        scope.close()


@pytest.mark.parametrize('strip', [False, True])
def test_colorama_text_only_replay_uses_original_rendering_policy(tmp_path, strip):
    from west import _execution

    project = project_at(tmp_path / 'color')
    out = io.StringIO()
    wrapped = AnsiToWin32(out, convert=False, strip=strip).stream
    controller = threading.Event()
    scope = _execution.ProjectContext(
        controller, buffered=True, stdout=wrapped, stderr=io.StringIO()
    )
    command = OutputCommand('output', 'output', 'output')
    try:
        with scope.activate():
            command.banner('West color')
            project.git(['-c', 'color.ui=always', 'log', '-1', '--format=%CredGit color%Creset'])
        scope.replay()
        assert 'West color' in out.getvalue()
        assert 'Git color' in out.getvalue()
        assert ('\x1b[' in out.getvalue()) is not strip
    finally:
        controller.set()
        scope.close()


@pytest.mark.parametrize('strip', [False, True])
def test_colorama_binary_destination_retains_explicit_git_bytes(tmp_path, monkeypatch, strip):
    from west import _execution

    project = project_at(tmp_path / 'binary color')
    argv = ['-c', 'color.ui=always', 'log', '-1', '--format=%CredGit color%Creset']
    expected = project.git(argv, capture_stdout=True, capture_stderr=True).stdout
    data = io.BytesIO()
    text = io.TextIOWrapper(data, encoding='ascii', errors='backslashreplace')
    wrapped = AnsiToWin32(text, convert=False, strip=strip).stream
    controller = threading.Event()
    scope = _execution.ProjectContext(
        controller, buffered=True, stdout=wrapped, stderr=io.StringIO()
    )
    command = OutputCommand('output', 'output', 'output')
    monkeypatch.setattr(OutputCommand, 'color_ui', property(lambda self: False))
    try:
        with scope.activate():
            command.banner('West color disabled')
            project.git(argv)
        scope.replay()
        text.flush()
        assert b'\x1b[' in expected
        assert data.getvalue().count(expected) == 1
        assert b'\x1b[92m' not in data.getvalue()
    finally:
        controller.set()
        scope.close()


def test_unbuffered_context_has_no_spools_and_keeps_git_capture(tmp_path, monkeypatch, capfd):
    from west import _execution

    project = project_at(tmp_path / 'live')
    out, err = io.StringIO(), io.StringIO()
    controller = threading.Event()

    def no_spool(*args, **kwargs):
        pytest.fail('unbuffered output must not allocate a spool')

    monkeypatch.setattr('tempfile.TemporaryFile', no_spool)
    scope = _execution.ProjectContext(controller, buffered=False, stdout=out, stderr=err)
    try:
        with scope.activate():
            OutputCommand('output', 'output', 'output').inf('live-python')
            assert out.getvalue() == 'live-python\n'
            project.git(['-c', "alias.emit=!printf 'live-child\\n'", 'emit'])
            captured = project.git(['rev-parse', 'HEAD'], capture_stdout=True)
            failed = project.git(
                ['rev-parse', '--verify', 'no-such-reference'],
                capture_stdout=True,
                capture_stderr=True,
                check=False,
            )
        assert 'live-child\n' in capfd.readouterr().out
        assert len(captured.stdout.strip()) == 40
        assert failed.returncode != 0 and failed.stderr
        scope.replay()
        assert out.getvalue() == 'live-python\n'
    finally:
        controller.set()
        scope.close()


@pytest.mark.parametrize('failure', ['create', 'replay'])
def test_output_io_failure_propagates_and_closes_owned_spools(tmp_path, monkeypatch, failure):
    from west import _execution

    command = Update()
    command.args = SimpleNamespace(jobs=2, buffered=None)
    command.updated = set()
    command.auto_cache = command.name_cache = command.path_cache = None
    command._topdir = os.fspath(tmp_path)
    command._git_ver = (2, 40, 0)
    projects = [Project(name, 'unused', topdir=tmp_path) for name in ['a', 'b', 'queued']]
    files, starts = [], []
    real_tempfile = tempfile.TemporaryFile

    def temporary_file(*args, **kwargs):
        if failure == 'create' and len(files) == 2:
            raise OSError('injected output create failure')
        file = real_tempfile(*args, **kwargs)
        files.append(file)
        return file

    class Destination(io.StringIO):
        def write(self, text):
            if failure == 'replay' and 'work::' in text:
                raise OSError('injected output replay failure')
            return super().write(text)

    def update(project):
        starts.append(project.name)
        command.inf(f'work::{project.name}')

    monkeypatch.setattr(tempfile, 'TemporaryFile', temporary_file)
    monkeypatch.setattr(command, 'update', update)
    monkeypatch.setattr(sys, 'stdout', Destination())
    original = sys.stdout, sys.stderr, Path.cwd(), dict(os.environ)
    with pytest.raises(OSError, match=f'injected output {failure} failure'):
        command._update_projects(projects)
    assert files and all(file.closed for file in files)
    assert 'queued' not in starts
    assert _execution.current_context() is None
    assert not any(t.name.startswith('west-update') for t in threading.enumerate())
    assert (sys.stdout, sys.stderr, Path.cwd(), dict(os.environ)) == original


def test_cleanup_reporting_preserves_primary_fatal_error(tmp_path, monkeypatch):
    command = Update()
    command.args = SimpleNamespace(jobs=2, buffered=None)
    command.updated = set()
    command.auto_cache = command.name_cache = command.path_cache = None
    command._topdir = os.fspath(tmp_path)
    command._git_ver = (2, 40, 0)
    projects = [Project(name, 'unused', topdir=tmp_path) for name in ['a', 'b']]

    class Output(io.StringIO):
        def write(self, text):
            if 'work::' in text:
                raise OSError('injected cleanup replay failure')
            return super().write(text)

    class Error(io.StringIO):
        def write(self, text):
            raise BrokenPipeError('injected closed error stream')

    def update(project):
        command.inf(f'work::{project.name}')
        raise SystemExit(7)

    monkeypatch.setattr(command, 'update', update)
    monkeypatch.setattr(sys, 'stdout', Output())
    monkeypatch.setattr(sys, 'stderr', Error())
    with pytest.raises(SystemExit) as raised:
        command._update_projects(projects)
    assert raised.value.code == 7
    assert not command.updated
    assert not any(t.name.startswith('west-update') for t in threading.enumerate())


@pytest.mark.parametrize('producer', ['python', 'git'])
def test_colorama_sequences_survive_replay_chunk_boundaries(tmp_path, producer):
    from west import _execution

    payload = 'x' * 65534 + '\x1b[31mred\x1b[0m\n'
    out = io.StringIO()
    wrapped = AnsiToWin32(out, convert=False, strip=True).stream
    controller = threading.Event()
    scope = _execution.ProjectContext(
        controller, buffered=True, stdout=wrapped, stderr=io.StringIO()
    )
    try:
        with scope.activate():
            if producer == 'python':
                OutputCommand('output', 'output', 'output').inf(payload, end='')
            else:
                project = project_at(tmp_path / 'ansi')
                (Path(project.abspath) / 'ansi.txt').write_bytes(payload.encode('ascii'))
                project.git(['-c', 'alias.emit=!cat ansi.txt', 'emit'])
        scope.replay()
        assert out.getvalue() == 'x' * 65534 + 'red\n'
    finally:
        controller.set()
        scope.close()


def test_scope_is_reset_after_fatal_error():
    from west import _execution

    controller = threading.Event()
    scope = _execution.ProjectContext(
        controller, buffered=True, stdout=io.StringIO(), stderr=io.StringIO()
    )
    try:
        with pytest.raises(SystemExit) as raised, scope.activate():
            raise SystemExit(7)
        assert raised.value.code == 7
        assert _execution.current_context() is None
    finally:
        controller.set()
        scope.close()


@pytest.mark.parametrize('failure', ['spool-write', 'spool-read', 'replay-write'])
def test_project_spool_detects_short_or_incomplete_io(failure):
    from west import _execution

    class ShortWrite(io.BytesIO):
        def write(self, data):
            return super().write(data[:-1])

    class EmptyRead(io.BytesIO):
        def read(self, size=-1):
            return b''

    if failure == 'spool-write':
        with ShortWrite() as file:
            output = _execution._ProjectOutput(file)
            with pytest.raises(OSError, match='short write to project output spool'):
                output.write('marker')
            assert len(file.getvalue()) == _execution._OUTPUT_RECORD.size - 1
    elif failure == 'spool-read':
        with EmptyRead() as file:
            output = _execution._ProjectOutput(file)
            output.write_bytes(b'marker')
            destination = io.StringIO()
            with pytest.raises(OSError, match='incomplete project output spool'):
                output.replay(destination)
            assert destination.getvalue() == ''
    else:
        with io.BytesIO() as file, io.TextIOWrapper(ShortWrite(), encoding='ascii') as destination:
            output = _execution._ProjectOutput(file)
            output.write_bytes(b'marker')
            with pytest.raises(OSError, match='short write replaying project output'):
                output.replay(destination)
            assert destination.buffer.getvalue() == b'marke'


@pytest.mark.skipif(sys.platform != 'win32', reason='legacy Windows console conversion')
def test_raw_replay_uses_legacy_console_conversion(monkeypatch):
    from west import _execution

    converted = []
    with io.BytesIO() as file, io.TextIOWrapper(io.BytesIO(), encoding='ascii') as destination:
        wrapped = AnsiToWin32(destination, convert=False, strip=False).stream
        renderer = AnsiToWin32(destination, convert=False, strip=True)

        def write(text):
            converted.append(text)
            renderer.write(text)

        def legacy_renderer(stream, strip):
            assert stream is wrapped and strip is False
            return SimpleNamespace(convert=True, write=write)

        monkeypatch.setattr(_execution, 'AnsiToWin32', legacy_renderer)
        output = _execution._ProjectOutput(file)
        output.write_bytes(b'\x1b[31mlegacy marker\x1b[0m\n')
        output.replay(wrapped)
        assert converted == ['\x1b[31mlegacy marker\x1b[0m\n']
        assert destination.buffer.getvalue() == b'legacy marker' + os.linesep.encode('ascii')
