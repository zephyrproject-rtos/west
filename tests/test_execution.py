# Copyright (c) 2026 NXP

import ctypes
import io
import logging
import os
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from colorama.ansitowin32 import AnsiToWin32
from conftest import create_repo

from west.app.main import LogHandler
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


def test_buffered_producers_keep_stream_ownership(tmp_path, monkeypatch):
    from west import _execution

    project = project_at(tmp_path / 'repo space \u96ea')
    out, err = io.StringIO(), io.StringIO()
    controller = _execution.ProcessController()
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
        controller.cancel()
        scope.close()
        controller.close()


def test_python_text_and_raw_child_bytes_keep_distinct_encodings(tmp_path):
    from west import _execution

    project = project_at(tmp_path / 'raw')
    data = io.BytesIO()
    out = io.TextIOWrapper(data, encoding='ascii', errors='backslashreplace')
    controller = _execution.ProcessController()
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
        controller.cancel()
        scope.close()
        controller.close()


@pytest.mark.parametrize('strip', [False, True])
def test_colorama_text_only_replay_uses_original_rendering_policy(tmp_path, strip):
    from west import _execution

    project = project_at(tmp_path / 'color')
    out = io.StringIO()
    wrapped = AnsiToWin32(out, convert=False, strip=strip).stream
    controller = _execution.ProcessController()
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
        controller.cancel()
        scope.close()
        controller.close()


@pytest.mark.parametrize('strip', [False, True])
def test_colorama_binary_destination_retains_explicit_git_bytes(tmp_path, monkeypatch, strip):
    from west import _execution

    project = project_at(tmp_path / 'binary color')
    argv = ['-c', 'color.ui=always', 'log', '-1', '--format=%CredGit color%Creset']
    expected = project.git(argv, capture_stdout=True, capture_stderr=True).stdout
    data = io.BytesIO()
    text = io.TextIOWrapper(data, encoding='ascii', errors='backslashreplace')
    wrapped = AnsiToWin32(text, convert=False, strip=strip).stream
    controller = _execution.ProcessController()
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
        controller.cancel()
        scope.close()
        controller.close()


def test_unbuffered_context_has_no_spools_and_keeps_git_capture(tmp_path, monkeypatch, capfd):
    from west import _execution

    project = project_at(tmp_path / 'live')
    out, err = io.StringIO(), io.StringIO()
    controller = _execution.ProcessController()

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
        controller.cancel()
        scope.close()
        controller.close()


@pytest.mark.parametrize('producer', ['python', 'git'])
def test_colorama_sequences_survive_replay_chunk_boundaries(tmp_path, producer):
    from west import _execution

    payload = 'x' * 65534 + '\x1b[31mred\x1b[0m\n'
    out = io.StringIO()
    wrapped = AnsiToWin32(out, convert=False, strip=True).stream
    controller = _execution.ProcessController()
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
        controller.cancel()
        scope.close()
        controller.close()


def test_scope_is_reset_after_fatal_error():
    from west import _execution

    controller = _execution.ProcessController()
    scope = _execution.ProjectContext(
        controller, buffered=True, stdout=io.StringIO(), stderr=io.StringIO()
    )
    try:
        with pytest.raises(SystemExit) as raised, scope.activate():
            raise SystemExit(7)
        assert raised.value.code == 7
        assert _execution.current_context() is None
    finally:
        controller.cancel()
        scope.close()
        controller.close()


@pytest.mark.parametrize('buffered', [False, True])
@pytest.mark.parametrize('delay_registration', [False, True])
def test_cancel_reaps_owned_git_tree_and_rejects_later_spawns(
    tmp_path, monkeypatch, buffered, delay_registration
):
    from west import _execution

    if delay_registration and sys.platform != 'win32':
        pytest.skip('Windows launch-to-job-assignment regression')
    project = project_at(tmp_path / 'children')
    pid_file = tmp_path / 'ready.pid'
    release = tmp_path / 'release'
    helper = tmp_path / 'blocked child.py'
    helper.write_text(
        'import os, sys, time\n'
        'from pathlib import Path\n'
        'Path(sys.argv[1]).write_text(str(os.getpid()), encoding="ascii")\n'
        'while not Path(sys.argv[2]).exists():\n'
        '    time.sleep(0.02)\n',
        encoding='ascii',
    )
    controller = _execution.ProcessController()
    scope = _execution.ProjectContext(
        controller, buffered=buffered, stdout=io.StringIO(), stderr=io.StringIO()
    )
    popen = subprocess.Popen
    owned = []

    def record_popen(args, **kwargs):
        process = popen(args, **kwargs)
        if args[0] == 'git':
            owned.append(process)
            if delay_registration and not kwargs.get('creationflags', 0) & 0x4:
                wait_until(
                    lambda: pid_file.exists() and bool(pid_file.read_text(encoding='ascii')),
                    timeout=30,
                )
        return process

    monkeypatch.setattr(subprocess, 'Popen', record_popen)
    sentinel = popen(
        [getattr(sys, '_base_executable', sys.executable), '-c', 'import time; time.sleep(120)']
    )
    executor = ThreadPoolExecutor(max_workers=1)

    def run_child():
        with scope.activate():
            alias = '!' + shlex.join(
                [sys.executable, os.fspath(helper), os.fspath(pid_file), os.fspath(release)]
            )
            project.git(['-c', f'alias.block={alias}', 'block'])

    future = executor.submit(run_child)

    def child_ready():
        if future.done():
            future.result()
            pytest.fail('Git child exited before readiness')
        return pid_file.exists() and bool(pid_file.read_text(encoding='ascii'))

    try:
        wait_until(child_ready, timeout=30)
        child_pid = int(pid_file.read_text(encoding='ascii'))
        assert process_active(child_pid)
        assert owned
        start = time.monotonic()
        controller.cancel()
        with pytest.raises((subprocess.CalledProcessError, CancelledError)):
            future.result(timeout=8)
        wait_until(lambda: not process_active(child_pid), timeout=1)
        assert time.monotonic() - start < 10
        assert all(process.poll() is not None for process in owned)
        assert sentinel.poll() is None
        with scope.activate(), pytest.raises(CancelledError):
            project.git(['--version'])
        assert len(owned) == 1
    finally:
        release.touch()
        try:
            controller.cancel()
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            scope.close()
            controller.close()
            sentinel.terminate()
            sentinel.wait(timeout=5)


@pytest.fixture
def windows_job_api(monkeypatch):
    if sys.platform != 'win32':
        pytest.skip('Windows job-object error handling')
    from west import _execution

    api = Mock()
    api.CreateJobObjectW.return_value = 100
    api.OpenProcess.return_value = 101
    api.AssignProcessToJobObject.return_value = True
    api.CreateToolhelp32Snapshot.return_value = 102
    api.OpenThread.return_value = 103
    api.GetProcessIdOfThread.return_value = 42
    api.ResumeThread.return_value = 1
    api.TerminateJobObject.return_value = True
    api.CloseHandle.return_value = True

    def first(snapshot, pointer):
        entry = ctypes.cast(pointer, ctypes.POINTER(_execution._ThreadEntry)).contents
        assert entry.dwSize == ctypes.sizeof(_execution._ThreadEntry)
        entry.th32OwnerProcessID = 42
        entry.th32ThreadID = 7
        return True

    def exhausted(*args):
        ctypes.set_last_error(18)
        return False

    api.Thread32First.side_effect = first
    api.Thread32Next.side_effect = exhausted
    monkeypatch.setattr(ctypes, 'WinDLL', Mock(return_value=api))
    return api


def windows_call_failure(result=0):
    def failed(*args):
        ctypes.set_last_error(5)
        return result

    return failed


@pytest.mark.parametrize(
    'failure', ['open-running', 'open-exited', 'assign-running', 'assign-exited']
)
def test_windows_job_assignment_failure_keeps_exit_distinction(windows_job_api, failure):
    from west import _execution

    api = windows_job_api
    process = Mock(pid=42)
    process.poll.return_value = 0 if failure.endswith('exited') else None
    if failure.startswith('open'):
        api.OpenProcess.side_effect = windows_call_failure()
    else:
        api.AssignProcessToJobObject.side_effect = windows_call_failure()
    job = _execution._WindowsJob()
    try:
        if failure.endswith('running'):
            with pytest.raises(OSError) as raised:
                job.add(process)
            assert raised.value.winerror == 5
        else:
            job.add(process)
        if failure.startswith('open'):
            api.AssignProcessToJobObject.assert_not_called()
            api.CloseHandle.assert_not_called()
        else:
            api.CloseHandle.assert_called_once_with(101)
        api.ResumeThread.assert_not_called()
    finally:
        job.close()


@pytest.mark.parametrize(
    'failure, diagnostic',
    [
        ('snapshot', None),
        ('enumeration', None),
        ('no-thread', 'cannot identify the suspended Git thread'),
        ('multiple-threads', 'cannot identify the suspended Git thread'),
        ('open-thread', None),
        ('owner', 'thread ownership changed'),
        ('exited', 'process exited before resuming'),
        ('resume-error', None),
        ('resume-zero', 'unexpected Git thread suspend count: 0'),
        ('resume-extra', 'unexpected Git thread suspend count: 2'),
    ],
)
def test_windows_job_resume_rejects_unsafe_state(windows_job_api, failure, diagnostic):
    from west import _execution

    api = windows_job_api
    process = Mock(pid=42)
    process.poll.return_value = None
    if failure == 'snapshot':
        api.CreateToolhelp32Snapshot.side_effect = windows_call_failure(ctypes.c_void_p(-1).value)
    elif failure == 'enumeration':
        api.Thread32Next.side_effect = windows_call_failure()
    elif failure == 'no-thread':

        def no_threads(*args):
            ctypes.set_last_error(18)
            return False

        api.Thread32First.side_effect = no_threads
    elif failure == 'multiple-threads':
        calls = 0

        def next_thread(snapshot, pointer):
            nonlocal calls
            calls += 1
            if calls == 1:
                entry = ctypes.cast(pointer, ctypes.POINTER(_execution._ThreadEntry)).contents
                entry.th32ThreadID = 8
                return True
            ctypes.set_last_error(18)
            return False

        api.Thread32Next.side_effect = next_thread
    elif failure == 'open-thread':
        api.OpenThread.side_effect = windows_call_failure()
    elif failure == 'owner':
        api.GetProcessIdOfThread.return_value = 43
    elif failure == 'exited':
        process.poll.return_value = 0
    elif failure == 'resume-error':
        api.ResumeThread.side_effect = windows_call_failure(0xFFFFFFFF)
    else:
        api.ResumeThread.return_value = 0 if failure == 'resume-zero' else 2
    job = _execution._WindowsJob()
    try:
        with pytest.raises(OSError) as raised:
            job.resume(process)
        if diagnostic:
            assert diagnostic in str(raised.value)
        else:
            assert raised.value.winerror == 5
        if failure.startswith('resume-'):
            api.ResumeThread.assert_called_once_with(103)
        else:
            api.ResumeThread.assert_not_called()
        handles = [item.args[0] for item in api.CloseHandle.call_args_list]
        if failure == 'snapshot':
            assert handles == []
        elif failure in ('enumeration', 'no-thread', 'multiple-threads', 'open-thread'):
            assert handles == [102]
        else:
            assert handles == [102, 103]
    finally:
        job.close()


@pytest.mark.parametrize('failure', ['create', 'terminate', 'close', 'handle', 'handle-primary'])
def test_windows_job_resource_failures_remain_visible(windows_job_api, failure):
    from west import _execution

    api = windows_job_api
    if failure == 'create':
        api.CreateJobObjectW.side_effect = windows_call_failure()
        with pytest.raises(OSError) as raised:
            _execution._WindowsJob()
        assert raised.value.winerror == 5
        api.CloseHandle.assert_not_called()
        return
    job = _execution._WindowsJob()
    try:
        if failure == 'handle-primary':
            primary = RuntimeError('primary handle operation failed')
            api.CloseHandle.side_effect = windows_call_failure()
            with pytest.raises(RuntimeError) as raised:
                try:
                    raise primary
                finally:
                    job._close_handle(101)
            assert raised.value is primary
            assert raised.value.__cause__.winerror == 5
        else:
            with pytest.raises(OSError) as raised:
                if failure == 'terminate':
                    api.TerminateJobObject.side_effect = windows_call_failure()
                    job.terminate()
                else:
                    api.CloseHandle.side_effect = windows_call_failure()
                    job.close() if failure == 'close' else job._close_handle(101)
            assert raised.value.winerror == 5
        assert job._handle == 100
    finally:
        api.CloseHandle.side_effect = None
        job.close()


@pytest.mark.parametrize('failure', ['job', 'kill', 'wait'])
def test_windows_cancellation_reports_and_reaps_owned_children(windows_job_api, failure):
    from west import _execution

    api = windows_job_api
    controller = _execution.ProcessController()
    child = Mock(pid=42)
    child.poll.return_value = None
    child.wait.return_value = 0
    controller._children.add(child)
    if failure == 'job':
        api.TerminateJobObject.side_effect = windows_call_failure()
    elif failure == 'kill':
        child.kill.side_effect = OSError('owned child kill failed')
    else:
        child.wait.side_effect = subprocess.TimeoutExpired(['git'], 2)
    try:
        with pytest.raises(OSError, match='could not stop owned Git children'):
            controller.cancel()
        child.kill.assert_called_once_with()
        child.wait.assert_called_once_with(timeout=2)
        assert controller._children == ({child} if failure == 'wait' else set())
        with pytest.raises(CancelledError):
            controller.check()
    finally:
        api.TerminateJobObject.side_effect = None
        child.kill.side_effect = child.wait.side_effect = None
        controller.cancel()
        controller.close()


@pytest.mark.parametrize('failure', ['signal', 'grace-timeout', 'reap'])
def test_posix_cancellation_escalation_and_errors(monkeypatch, failure):
    from west import _execution

    signals = []

    def killpg(pid, signum):
        signals.append((pid, signum))
        if failure == 'signal' and signum == 15:
            raise PermissionError('owned process group signal failed')

    monkeypatch.setattr(_execution, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(_execution, 'os', SimpleNamespace(killpg=killpg))
    monkeypatch.setattr(_execution, 'signal', SimpleNamespace(SIGTERM=15, SIGKILL=9))
    controller = _execution.ProcessController()
    child = Mock(pid=42)
    child.wait.return_value = 0
    if failure == 'grace-timeout':
        child.wait.side_effect = [subprocess.TimeoutExpired(['git'], 0.25), 0]
    elif failure == 'reap':
        child.wait.side_effect = [0, subprocess.TimeoutExpired(['git'], 2)]
    controller._children.add(child)
    try:
        if failure == 'grace-timeout':
            controller.cancel()
        else:
            with pytest.raises(OSError, match='could not stop owned Git children'):
                controller.cancel()
        assert signals == [(42, 15), (42, 9)]
        assert child.wait.call_count == 2
        assert 0 <= child.wait.call_args_list[0].kwargs['timeout'] <= 0.25
        assert child.wait.call_args_list[1] == call(timeout=2)
        assert controller._children == ({child} if failure == 'reap' else set())
        with pytest.raises(CancelledError):
            controller.check()
    finally:
        child.wait.side_effect = None
        controller._children.clear()
        controller.close()


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
            assert file.getvalue() == b'marke'
    elif failure == 'spool-read':
        with EmptyRead() as file:
            file.write(b'marker')
            output = _execution._ProjectOutput(file)
            destination = io.StringIO()
            with pytest.raises(OSError, match='incomplete project output spool'):
                output.replay(destination)
            assert destination.getvalue() == ''
    else:
        with io.BytesIO() as file, io.TextIOWrapper(ShortWrite(), encoding='ascii') as destination:
            file.write(b'marker')
            output = _execution._ProjectOutput(file)
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
        file.write(b'\x1b[31mlegacy marker\x1b[0m\n')
        output = _execution._ProjectOutput(file)
        output.replay(wrapped)
        assert converted == ['\x1b[31mlegacy marker\x1b[0m\n']
        assert destination.buffer.getvalue() == b'legacy marker' + os.linesep.encode('ascii')
