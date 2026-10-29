# Copyright 2026 NXP

import json
import os
import shlex
import signal
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

import pytest
import yaml
from conftest import GIT, create_repo, create_workspace
from test_execution import process_active, wait_until

from west.app import main as west_main

CLI_DRIVER = '''
import ctypes, json, os, signal, subprocess, sys, threading
from pathlib import Path
from west.app.main import main
from west.app.project import Update
signal.signal(signal.SIGINT, signal.default_int_handler)
if sys.platform == 'win32':
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    if not kernel.SetConsoleCtrlHandler(None, False):
        raise ctypes.WinError(ctypes.get_last_error())
real_popen = subprocess.Popen
lock = threading.Lock()
def recorded(args, **kwargs):
    child = real_popen(args, **kwargs)
    if args[0] == 'git':
        with lock, Path(os.environ['WEST_TEST_CHILDREN']).open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'pid': child.pid, 'argv': args}) + '\\n')
    return child
subprocess.Popen = recorded
real_update = Update.update
def recorded_update(self, project):
    with lock, Path(os.environ['WEST_TEST_STARTS']).open('a', encoding='utf-8') as stream:
        stream.write(project.name + '\\n')
    return real_update(self, project)
Update.update = recorded_update
try:
    main(sys.argv[1:])
except SystemExit as error:
    Path(os.environ['WEST_TEST_EXIT']).write_text(str(error.code), encoding='ascii')
    raise
'''

WINDOWS_INTERRUPT = '''
import ctypes, sys, time
from pathlib import Path
ready, trigger, sent = map(Path, sys.argv[2:])
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.FreeConsole()
if not kernel.AttachConsole(int(sys.argv[1])):
    raise ctypes.WinError(ctypes.get_last_error())
if not kernel.SetConsoleCtrlHandler(None, True):
    raise ctypes.WinError(ctypes.get_last_error())
ready.write_text('ready', encoding='ascii')
while not trigger.exists():
    time.sleep(0.02)
if not kernel.GenerateConsoleCtrlEvent(0, 0):
    raise ctypes.WinError(ctypes.get_last_error())
sent.write_text('sent', encoding='ascii')
time.sleep(0.1)
kernel.FreeConsole()
'''


@pytest.mark.parametrize('buffered', [False, True])
@pytest.mark.parametrize(
    'shell', ['direct', 'cmd', 'powershell'] if sys.platform == 'win32' else ['direct']
)
def test_cli_terminal_interrupt_reaches_git_and_stops_pending_work(tmp_path, buffered, shell):
    remote = tmp_path / 'remote'
    create_repo(remote)
    workspace = tmp_path / 'workspace'
    create_workspace(workspace)
    manifest = {
        'manifest': {
            'projects': [{'name': name, 'url': os.fspath(remote)} for name in ['a', 'b', 'queued']]
        }
    }
    (workspace / 'mp' / 'west.yml').write_text(yaml.safe_dump(manifest), encoding='utf-8')
    for name in ['a', 'b', 'queued']:
        subprocess.check_call([GIT, 'clone', '--', remote, workspace / name])
        (workspace / name / '.git' / 'unrelated.lock').write_bytes(b'not owned by West\n')
    ready = tmp_path / 'ready'
    ready.mkdir()
    release = tmp_path / 'release-cli'
    spools = tmp_path / 'spools'
    spools.mkdir()
    children = tmp_path / 'children.jsonl'
    starts = tmp_path / 'starts'
    exit_status = tmp_path / 'exit-status'
    driver = tmp_path / 'west interrupt driver.py'
    driver.write_text(CLI_DRIVER, encoding='utf-8')
    helper = tmp_path / 'prepared reference transaction.py'
    helper.write_text(
        'import ctypes, os, sys, time\n'
        'from pathlib import Path\n'
        'updates = sys.stdin.read()\n'
        'if not updates.strip():\n'
        '    sys.exit(0)\n'
        'if sys.platform == "win32":\n'
        '    kernel = ctypes.WinDLL("kernel32", use_last_error=True)\n'
        '    if not kernel.SetConsoleCtrlHandler(None, False):\n'
        '        raise ctypes.WinError(ctypes.get_last_error())\n'
        'ready, release = Path(sys.argv[1]), Path(sys.argv[2])\n'
        'ready.joinpath(str(os.getpid())).write_text(updates, encoding="ascii")\n'
        'while not release.exists():\n'
        '    time.sleep(0.02)\n',
        encoding='ascii',
    )
    executable = (
        getattr(sys, '_base_executable', sys.executable)
        if sys.platform == 'win32'
        else sys.executable
    )
    transaction = shlex.join([executable, os.fspath(helper), os.fspath(ready), os.fspath(release)])
    for name in ['a', 'b', 'queued']:
        hook = workspace / name / '.git' / 'hooks' / 'reference-transaction'
        hook.write_text(
            f'#!/bin/sh\nif test "$1" = prepared; then\n  exec {transaction}\nfi\n',
            encoding='utf-8',
        )
        hook.chmod(0o755)
    argv = [
        executable,
        os.fspath(driver),
        'update',
        '-j',
        '2',
        '--fetch=always',
    ]
    if not buffered:
        argv.append('--no-buffered')
    if shell == 'cmd':
        # cmd.exe needs literal outer quotes, not list2cmdline's escaped quotes.
        command = subprocess.list2cmdline(argv)
        argv = subprocess.list2cmdline([os.environ['COMSPEC']]) + f' /d /s /c "{command}"'
    elif shell == 'powershell':
        command = '& ' + ' '.join("'" + arg.replace("'", "''") + "'" for arg in argv)
        argv = [
            'powershell.exe',
            '-NoProfile',
            '-NonInteractive',
            '-Command',
            command + '; exit $LASTEXITCODE',
        ]
    env = dict(os.environ)
    env.update(
        PYTHONPATH=os.pathsep.join(
            [
                os.fspath(Path(west_main.__file__).parents[2]),
                sysconfig.get_path('purelib'),
                env.get('PYTHONPATH', ''),
            ]
        ),
        WEST_TEST_CHILDREN=os.fspath(children),
        WEST_TEST_STARTS=os.fspath(starts),
        WEST_TEST_EXIT=os.fspath(exit_status),
        TEMP=os.fspath(spools),
        TMP=os.fspath(spools),
        TMPDIR=os.fspath(spools),
        GIT_TERMINAL_PROMPT='0',
    )
    creation = (
        {'creationflags': subprocess.CREATE_NEW_CONSOLE}
        if sys.platform == 'win32'
        else {'start_new_session': True}
    )
    if sys.platform == 'win32':
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        creation['startupinfo'] = startup
    sentinel = subprocess.Popen(
        [getattr(sys, '_base_executable', sys.executable), '-c', 'import time; time.sleep(120)']
    )
    try:
        process = subprocess.Popen(
            argv, cwd=workspace, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **creation
        )
    except OSError:
        sentinel.terminate()
        sentinel.wait(timeout=5)
        raise
    injector = None
    inject_ready, inject_trigger, inject_sent = [
        tmp_path / name for name in ['inject-ready', 'inject-trigger', 'inject-sent']
    ]

    def both_ready():
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(f'CLI exited before readiness: {stdout!r} {stderr!r}')
        return len(list(ready.iterdir())) == 2

    try:
        wait_until(both_ready, timeout=120)
        helper_pids = [int(p.name) for p in ready.iterdir()]
        assert sorted(starts.read_text(encoding='utf-8').splitlines()) == ['a', 'b']
        held_locks = []
        for name in ['a', 'b']:
            locks = [
                lock
                for lock in (workspace / name / '.git').rglob('*.lock')
                if lock.name != 'unrelated.lock'
            ]
            assert locks, f'{name}: Git must hold a prepared reference transaction'
            held_locks.extend(locks)
        if sys.platform == 'win32':
            injector = subprocess.Popen(
                [
                    executable,
                    '-c',
                    WINDOWS_INTERRUPT,
                    str(process.pid),
                    os.fspath(inject_ready),
                    os.fspath(inject_trigger),
                    os.fspath(inject_sent),
                ],
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            def injector_ready():
                if injector.poll() is not None:
                    stdout, stderr = injector.communicate()
                    pytest.fail(
                        f'interrupt injector exited before readiness: {stdout!r} {stderr!r}'
                    )
                return inject_ready.exists()

            wait_until(injector_ready, timeout=30)
        start = time.monotonic()
        if sys.platform == 'win32':
            inject_trigger.touch()
        else:
            os.killpg(process.pid, signal.SIGINT)
        _, stderr = process.communicate(timeout=max(0.1, 10 - (time.monotonic() - start)))
        assert b'forcefully stopped' not in stderr
        assert b'Git lock files may remain' not in stderr
        if sys.platform == 'win32':
            assert inject_sent.is_file()
            injector.communicate(timeout=3)
            assert injector.returncode == 0
            assert int(exit_status.read_text(encoding='ascii')) & 0xFFFFFFFF == 0xC000013A
            if shell == 'direct':
                assert process.returncode & 0xFFFFFFFF == 0xC000013A
        else:
            assert process.returncode == -signal.SIGINT
        owned = [json.loads(line) for line in children.read_text(encoding='utf-8').splitlines()]
        fetches = [p['pid'] for p in owned if p['argv'][1] == 'fetch']
        assert len(fetches) == 2
        # Helpers can close inherited pipes just before their process exits.
        wait_until(
            lambda: all(not process_active(pid) for pid in fetches + helper_pids),
            timeout=max(0.1, 10 - (time.monotonic() - start)),
        )
        assert time.monotonic() - start < 10
        assert all(not lock.exists() for lock in held_locks)
        assert len(list(ready.iterdir())) == 2
        assert sorted(starts.read_text(encoding='utf-8').splitlines()) == ['a', 'b']
        for name in ['a', 'b', 'queued']:
            assert (workspace / name / '.git' / 'unrelated.lock').read_bytes() == (
                b'not owned by West\n'
            )
        assert not list(spools.iterdir())
        assert sentinel.poll() is None
    finally:
        release.touch()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
        finally:
            if injector is not None:
                inject_trigger.touch()
                try:
                    injector.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    injector.kill()
                    injector.communicate(timeout=5)
            sentinel.terminate()
            sentinel.wait(timeout=5)


def test_cli_terminal_interrupt_waits_for_helper_exit(tmp_path, monkeypatch):
    real_process_active = process_active
    delayed = set()

    def process_still_exiting(pid):
        if not delayed and (tmp_path / 'ready' / str(pid)).is_file():
            delayed.add(pid)
            return True
        return real_process_active(pid)

    monkeypatch.setitem(globals(), 'process_active', process_still_exiting)
    test_cli_terminal_interrupt_reaches_git_and_stops_pending_work(tmp_path, False, 'direct')
    assert len(delayed) == 1


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS controlling-terminal regression')
@pytest.mark.parametrize('buffered', [False, True])
def test_cli_terminal_interrupt_cleans_owned_resources(tmp_path, monkeypatch, buffered):
    import fcntl
    import pty
    import select
    import termios

    master, slave = pty.openpty()
    real_popen = subprocess.Popen
    real_killpg = os.killpg
    driver_pids = set()
    interrupts = []

    def claim_terminal():
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        os.tcsetpgrp(0, os.getpgrp())
        assert os.tcgetpgrp(0) == os.getpgrp()

    def terminal_popen(args, *popen_args, **kwargs):
        driver = (
            isinstance(args, list)
            and len(args) > 1
            and Path(args[1]).name == 'west interrupt driver.py'
        )
        if driver:
            kwargs['stdin'] = slave
            kwargs['preexec_fn'] = claim_terminal
        process = real_popen(args, *popen_args, **kwargs)
        if driver:
            driver_pids.add(process.pid)
        return process

    def terminal_killpg(pgid, signum):
        if pgid in driver_pids and signum == signal.SIGINT:
            interrupts.append(os.write(master, b'\x03'))
            if select.select([master], [], [], 1)[0]:
                os.read(master, 1024)
        else:
            real_killpg(pgid, signum)

    monkeypatch.setattr(subprocess, 'Popen', terminal_popen)
    monkeypatch.setattr(os, 'killpg', terminal_killpg)
    try:
        test_cli_terminal_interrupt_reaches_git_and_stops_pending_work(tmp_path, buffered, 'direct')
        assert interrupts == [1]
    finally:
        os.close(master)
        os.close(slave)
