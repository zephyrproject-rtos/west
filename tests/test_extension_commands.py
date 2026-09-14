# Copyright (c) 2025 Basalte bv
#
# SPDX-License-Identifier: Apache-2.0

import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml
from conftest import (
    GIT,
    WINDOWS,
    add_commit,
    cmd,
    cmd_raises,
    cmd_subprocess,
    yaml_editor,
)


def _yaml_get_proj(mf: dict, projname: str):
    _l = [p for p in mf["manifest"]['projects'] if p["name"] == projname]
    assert len(_l) == 1
    return _l[0]


# The west command "test-extension" comes from the "west_update_tmpdir" fixture in conftest.py


def test_extension_commands_basic(west_update_tmpdir):
    # Test basic extension command loading and structure
    ext_output = cmd('test-extension')
    assert 'Testing test command 1' in ext_output


def test_extension_commands_disabled(west_update_tmpdir):
    # Test that extension commands can be disabled via config
    cmd('config commands.allow_extensions false')
    err_info, _ = cmd_raises('test-extension', SystemExit)
    assert 'unknown command "test-extension"' in err_info.value.code


def test_extension_command_missing_file(west_update_tmpdir):
    # Test handling of extension commands with missing python files
    net_tools_path = west_update_tmpdir / 'net-tools'
    add_commit(
        net_tools_path,
        'add broken extension command',
        files={
            'scripts/west-commands.yml': textwrap.dedent('''\
                west-commands:
                  - file: scripts/nonexistent.py
                    commands:
                      - name: broken-cmd
                        class: BrokenCommand
                        help: this will fail
                '''),
        },
    )

    cmd_raises('broken-cmd', FileNotFoundError)


def test_extension_command_invalid_yaml(west_update_tmpdir):
    # Test handling of invalid YAML in west-commands file
    net_tools_path = west_update_tmpdir / 'net-tools'
    add_commit(
        net_tools_path,
        'add invalid yaml',
        files={
            'scripts/west-commands.yml': '[[[ this is not valid YAML at all',
        },
    )

    # Calling a built-in command should already fail
    _, err_msg = cmd_raises('help', SystemExit)
    assert 'could not load extension command(s)' in err_msg


def test_extension_command_invalid_schema(west_update_tmpdir):
    # Test handling of YAML that doesn't match the schema
    net_tools_path = west_update_tmpdir / 'net-tools'
    add_commit(
        net_tools_path,
        'add invalid schema',
        files={
            'scripts/west-commands.yml': textwrap.dedent('''\
                west-commands:
                  - commands:
                      - name: bad-cmd
                '''),
        },
    )

    _, err_msg = cmd_raises('bad-cmd', SystemExit)
    assert 'could not load extension command(s)' in err_msg


def test_extension_command_missing_attribute(west_update_tmpdir):
    # Test handling of extension command with missing class attribute
    net_tools_path = west_update_tmpdir / 'net-tools'
    add_commit(
        net_tools_path,
        'add python file without class',
        files={
            'scripts/no-class.py': textwrap.dedent('''\
                # This file doesn't have the expected class
                def some_function():
                    pass
                '''),
            'scripts/west-commands.yml': textwrap.dedent('''\
                west-commands:
                  - file: scripts/no-class.py
                    commands:
                      - name: no-class-cmd
                        class: MissingClass
                        help: this will fail
                '''),
        },
    )

    _, err_msg = cmd_raises('no-class-cmd', SystemExit)
    assert 'no attribute MissingClass' in err_msg


def test_extension_command_constructor_error(west_update_tmpdir):
    # Test handling of extension command whose constructor raises an exception
    net_tools_path = west_update_tmpdir / 'net-tools'
    add_commit(
        net_tools_path,
        'add command with broken constructor',
        files={
            'scripts/broken-ctor.py': textwrap.dedent('''\
                from west.commands import WestCommand
                class BrokenConstructor(WestCommand):
                    def __init__(self):
                        raise ValueError("Constructor intentionally broken")
                    def do_add_parser(self, parser_adder):
                        pass
                    def do_run(self, args, unknown):
                        pass
                '''),
            'scripts/west-commands.yml': textwrap.dedent('''\
                west-commands:
                  - file: scripts/broken-ctor.py
                    commands:
                      - name: broken-ctor
                        class: BrokenConstructor
                        help: broken constructor
                '''),
        },
    )
    _, err_msg = cmd_raises('broken-ctor', SystemExit)
    assert 'command constructor threw an exception' in err_msg


def test_extension_command_import_error(west_update_tmpdir):
    # Test handling of extension command with import errors
    net_tools_path = west_update_tmpdir / 'net-tools'
    add_commit(
        net_tools_path,
        'add file with import error',
        files={
            'scripts/import-error.py': textwrap.dedent('''\
                from nonexistent_module import something
                from west.commands import WestCommand
                class TestCommand(WestCommand):
                    pass
                '''),
            'scripts/west-commands.yml': textwrap.dedent('''\
                west-commands:
                  - file: scripts/import-error.py
                    commands:
                      - name: import-error
                        class: TestCommand
                        help: import error
                '''),
        },
    )

    _, err_msg = cmd_raises('import-error', SystemExit)
    assert 'could not import' in err_msg


def test_extension_command_directory_escape(west_update_tmpdir):
    # Test that extension commands can't escape project directory
    net_tools_path = west_update_tmpdir / 'net-tools'
    add_commit(
        net_tools_path,
        'add escaping west-commands file',
        files={
            'scripts/west-commands.yml': textwrap.dedent('''\
                west-commands:
                  - file: ../../zephyr/evil.py
                    commands:
                      - name: evil
                        class: Evil
                        help: escape attempt
                '''),
        },
    )

    _, err_msg = cmd_raises('evil', SystemExit)
    assert 'escapes project path' in err_msg


def test_extension_command_default_class_name(west_update_tmpdir):
    # Test that class name defaults to command name if not specified
    net_tools_path = west_update_tmpdir / 'net-tools'
    add_commit(
        net_tools_path,
        'add command with default class name',
        files={
            'scripts/test.py': textwrap.dedent('''\
                from west.commands import WestCommand
                class mycommand(WestCommand):
                    def __init__(self):
                        super().__init__('mycommand', 'help text', 'description')
                    def do_add_parser(self, parser_adder):
                        return parser_adder.add_parser(self.name)
                    def do_run(self, args, unknown):
                        print('default class name works')
                '''),
            'scripts/west-commands.yml': textwrap.dedent('''\
                west-commands:
                  - file: scripts/test.py
                    commands:
                      - name: mycommand
                        help: test default class name
                '''),
        },
    )

    ext_output = cmd('mycommand')
    assert 'default class name works' in ext_output


def test_extension_command_multiple_commands_same_file(west_update_tmpdir):
    # Test multiple commands defined in the same python file
    net_tools_path = west_update_tmpdir / 'net-tools'
    add_commit(
        net_tools_path,
        'add multiple commands',
        files={
            'scripts/multi.py': textwrap.dedent('''\
                from west.commands import WestCommand

                class FirstCommand(WestCommand):
                    def __init__(self):
                        super().__init__('first', 'first help', 'first description')
                    def do_add_parser(self, parser_adder):
                        return parser_adder.add_parser(self.name)
                    def do_run(self, args, unknown):
                        print('first command')

                class SecondCommand(WestCommand):
                    def __init__(self):
                        super().__init__('second', 'second help', 'second description')
                    def do_add_parser(self, parser_adder):
                        return parser_adder.add_parser(self.name)
                    def do_run(self, args, unknown):
                        print('second command')
                '''),
            'scripts/west-commands.yml': textwrap.dedent('''\
                west-commands:
                  - file: scripts/multi.py
                    commands:
                      - name: first
                        class: FirstCommand
                        help: first command help
                      - name: second
                        class: SecondCommand
                        help: second command help
                '''),
        },
    )

    ext_output = cmd('first')
    assert 'first command' in ext_output
    ext_output = cmd('second')
    assert 'second command' in ext_output


def test_call_imported_project_submanifest_commands_from_project_subdirectory(repos_tmpdir):
    # If net-tools imports mf_subdir/west.yml and that manifest declares
    # self: west-commands: scripts/west-commands.yml, then file paths in that
    # west-commands YAML are resolved relative to the imported manifest root.
    # The same paths must work whether a project is imported or initialized directly;
    # importing must never break anything.
    manifest_path = repos_tmpdir / 'repos' / 'zephyr'
    net_tools_path = repos_tmpdir / 'repos' / 'net-tools'

    MF_SUB_WEST_YML = textwrap.dedent(
        '''\
        manifest:
          self:
            west-commands: scripts/west-commands-from-subdir.yml
        '''
    )
    MF_SUB_COMMANDS_YML = textwrap.dedent(
        '''\
        west-commands:
          - file: test/west-commands/subdir_command.py
            commands:
              - name: imported-command-from-subdir
                class: ImportedCommandFromProjectSubdir
                help: imported extension help
        '''
    )
    MF_SUB_WEST_PY = textwrap.dedent(
        '''\
        from west.commands import WestCommand

        class ImportedCommandFromProjectSubdir(WestCommand):
            def __init__(self):
                super().__init__(
                    'imported-command-from-subdir',
                    'imported command from subdir help',
                    'imported command from subdir description',
                )

            def do_add_parser(self, parser_adder):
                return parser_adder.add_parser(self.name)

            def do_run(self, args, unknown):
                print('imported command from subdir works')
        '''
    )

    add_commit(
        net_tools_path,
        'add imported submanifest extension command',
        files={
            'mf_subdir/west.yml': MF_SUB_WEST_YML,
            'mf_subdir/scripts/west-commands-from-subdir.yml': MF_SUB_COMMANDS_YML,
            'mf_subdir/test/west-commands/subdir_command.py': MF_SUB_WEST_PY,
        },
    )

    with yaml_editor(manifest_path / 'west.yml') as mf:
        net_tools_project = next(p for p in mf['manifest']['projects'] if p['name'] == 'net-tools')
        net_tools_project['import'] = 'mf_subdir/west.yml'
    subprocess.check_call(
        [
            GIT,
            '-C',
            str(manifest_path),
            'commit',
            '-m',
            'import mf_subdir/west.yml',
            'west.yml',
        ]
    )

    workspace = repos_tmpdir / 'workspace'
    cmd(['init', '-m', str(manifest_path), str(workspace)])
    cmd('update', cwd=workspace)

    # First, make sure extensions work fine without imports. Otherwise
    # the next assert covers too much code at once which makes failures
    # difficult to interpret.
    ext_output = cmd('test-extension', cwd=workspace)
    assert 'Testing test command 1' in ext_output, 'No-import extension failed'

    ext_output = cmd('imported-command-from-subdir', cwd=workspace)
    assert 'imported command from subdir works' in ext_output


def test_call_imported_project_submanifest_commands_from_project_subdirectory_special_chars(
    repos_tmpdir,
):
    # Same as the forward-slash test above, but use non-portable separators in
    # the manifest file paths. See test_extension_special_chars() for more
    # details.
    manifest_path = repos_tmpdir / 'repos' / 'zephyr'
    net_tools_path = repos_tmpdir / 'repos' / 'net-tools'

    _WEIRD_CMDS_PATH = r'scripts/\winsubA\\\west-commands-from-subdir-windows.yml'
    MF_SUB_WEST_YML = textwrap.dedent(
        f'''\
        manifest:
          self:
            west-commands: {_WEIRD_CMDS_PATH}
        '''
    )
    _WEIRD_EXT_PATH = r'test\\/winsubB\\\west-commands\subdir_command_windows.py'
    MF_SUB_COMMANDS_YML = textwrap.dedent(
        f'''\
        west-commands:
          - file: {_WEIRD_EXT_PATH}
            commands:
              - name: imported-command-from-subdir-windows
                class: ImportedCommandFromProjectSubdirWindows
                help: imported command from subdir with windows paths help
        '''
    )
    MF_SUB_WEST_PY = textwrap.dedent(
        '''\
        from west.commands import WestCommand

        class ImportedCommandFromProjectSubdirWindows(WestCommand):
            def __init__(self):
                super().__init__(
                    'imported-command-from-subdir-windows',
                    'imported command from subdir with windows paths help',
                    'imported command from subdir with windows paths description',
                )

            def do_add_parser(self, parser_adder):
                return parser_adder.add_parser(self.name)

            def do_run(self, args, unknown):
                print('imported command from subdir with windows paths works')
        '''
    )

    add_commit(
        net_tools_path,
        'add imported submanifest extension command with windows paths',
        files={
            Path('mf_subdir') / 'west.yml': MF_SUB_WEST_YML,
            Path('mf_subdir') / _WEIRD_CMDS_PATH: MF_SUB_COMMANDS_YML,
            Path('mf_subdir') / _WEIRD_EXT_PATH: MF_SUB_WEST_PY,
        },
    )

    with yaml_editor(manifest_path / 'west.yml') as mf:
        net_tools_project = next(p for p in mf['manifest']['projects'] if p['name'] == 'net-tools')
        net_tools_project['import'] = r'mf_subdir/west.yml'
    subprocess.check_call(
        [
            GIT,
            '-C',
            str(manifest_path),
            'commit',
            '-m',
            'import mf_subdir\\west.yml',
            'west.yml',
        ]
    )

    workspace = repos_tmpdir / 'workspace'
    cmd(['init', '-m', str(manifest_path), str(workspace)])
    cmd('update', cwd=workspace)

    # First, make sure extensions work fine without imports. Otherwise
    # the next assert covers too much code at once which makes failures
    # difficult to interpret.
    ext_output = cmd('test-extension', cwd=workspace)
    assert 'Testing test command 1' in ext_output, 'No-import extension failed.'

    ext_output = cmd('imported-command-from-subdir-windows', cwd=workspace)
    assert 'imported command from subdir with windows paths works' in ext_output

    # Check how west-commands: gets printed back in `west manifest --resolve`
    _resolved_mf = cmd(['manifest', '--resolve'], cwd=workspace)
    resolved_mf = yaml.safe_load(_resolved_mf)
    net_tools = _yaml_get_proj(resolved_mf, "net-tools")
    # FIXME #961: this is inconsistent with the other _special_chars() test
    # below which does NOT normalize as_posix() on Windows!
    if WINDOWS:
        # All normalized to forward slashes a/b/c/d on Windows
        expected = r'mf_subdir/' + Path(_WEIRD_CMDS_PATH).as_posix()
    else:
        # Untouched on Un*x
        expected = r'mf_subdir/' + _WEIRD_CMDS_PATH
    print()
    assert net_tools["west-commands"][1] == expected


def test_extension_special_chars(west_update_tmpdir):
    # Detect any unexpected changes in the way we've been handling backslashes and other
    # special characters. Changes in how we handle such edge cases may or may not be desired
    # (and this test may be updated accordingly), but we never want these changes to come as
    # a surprise and we want to keep control over them.

    ext_proj = 'net-tools'
    ext_proj_p = Path(ext_proj)

    # Rename scripts/test.py to something strange.
    # The actual location is purposely different on Windows
    weird_ext_py = r'scripts///win subdir\\\test.py'
    with yaml_editor(ext_proj_p / 'scripts' / 'west-commands.yml') as cmds:
        assert cmds["west-commands"][0]["file"] == 'scripts/test.py'
        cmds["west-commands"][0]["file"] = weird_ext_py
    if WINDOWS:
        (ext_proj_p / 'scripts' / 'win subdir').mkdir()
    (ext_proj_p / 'scripts' / 'test.py').rename(ext_proj_p / weird_ext_py)

    # Just for the logs
    subprocess.check_call([GIT, '-C', ext_proj, 'add', weird_ext_py])
    print(cmd('diff --manifest'))

    # Does the extension still work
    ext_output = cmd('test-extension')
    assert 'Testing test command 1' in ext_output

    # Now also rename the project's 'scripts/west-commands.yml' to something strange
    weird_cmds = r'scripts///win subdir\\\w-cmds.yml'
    with yaml_editor('zephyr/west.yml') as _mf:
        _ext_p_yml = _yaml_get_proj(_mf, ext_proj)
        assert _ext_p_yml["west-commands"] == 'scripts/west-commands.yml'
        _ext_p_yml["west-commands"] = weird_cmds
    (ext_proj_p / 'scripts' / 'west-commands.yml').rename(ext_proj_p / weird_cmds)

    # Just for the logs
    subprocess.check_call([GIT, '-C', ext_proj, 'add', weird_cmds])
    print(cmd('diff --manifest'))

    # Does the extension still work
    ext_output = cmd('test-extension')
    assert 'Testing test command 1' in ext_output

    # Test how west-commands gets printed back in `west manifest --resolve`
    resolved_mf = cmd('manifest --resolve')
    resolved_mf = yaml.safe_load(resolved_mf)
    ext_proj_yaml = _yaml_get_proj(resolved_mf, ext_proj)
    assert ext_proj_yaml["west-commands"] == weird_cmds

    ######  self: west-commands #####

    # self: west-commands: follows a slightly different code path.
    # Move the extension away from the project and into self.
    Path('zephyr', 'scripts').mkdir()
    if WINDOWS:
        Path('zephyr', 'scripts', 'win subdir').mkdir()
    (ext_proj_p / weird_ext_py).rename(Path('zephyr', weird_ext_py))

    (ext_proj_p / weird_cmds).rename(Path('zephyr', weird_cmds))

    # The extension is now missing from ext_proj. That's OK, it's supported.
    with yaml_editor('zephyr/west.yml') as _mf:
        _mf["manifest"]["self"]["west-commands"] = weird_cmds

    ext_output = cmd('test-extension')
    assert 'Testing test command 1' in ext_output

    # Test how west-commands gets printed back in `west manifest --resolve`
    resolved_mf = cmd('manifest --resolve')
    resolved_mf = yaml.safe_load(resolved_mf)
    assert resolved_mf["manifest"]["self"]["west-commands"] == weird_cmds


#
# Executable (exec) extension commands
#


# A Python script that echoes the WEST_* context and its forwarded arguments.
# It is run through an interpreter, so it works on every platform.
_ENV_ECHO_PY = textwrap.dedent('''\
    import os
    import sys

    print("hello from exec extension")
    print("command=" + os.environ.get("WEST_COMMAND", ""))
    print("topdir=" + os.environ.get("WEST_TOPDIR", ""))
    print("project=" + os.environ.get("WEST_PROJECT_PATH", ""))
    _mf = os.environ.get("WEST_MANIFEST_PATH", "")
    print("have_manifest=" + ("yes" if os.path.isfile(_mf) else "no"))
    print("args=" + " ".join(sys.argv[1:]))
    ''')

# YAML scalar for the interpreter that runs this test's Python. Single-quoted
# so backslashes and spaces in the path survive on Windows.
_PY = f"'{sys.executable}'"


def _add_exec_extension(west_update_tmpdir, files, *, make_executable=None):
    # Commit 'files' to the net-tools project and mark 'make_executable'
    # (a project-relative path) as executable in the working tree.
    net_tools_path = west_update_tmpdir / 'net-tools'
    add_commit(net_tools_path, 'add exec extension', files=files)
    if make_executable is not None:
        script_path = net_tools_path / make_executable
        mode = os.stat(script_path).st_mode
        os.chmod(script_path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return net_tools_path


def test_exec_extension_interpreter(west_update_tmpdir):
    # An interpreter-backed executable extension: the file is run as an
    # argument to the interpreter and exposes workspace context through the
    # WEST_* environment variables. This works on every platform.
    _add_exec_extension(
        west_update_tmpdir,
        files={
            'scripts/echo.py': _ENV_ECHO_PY,
            'scripts/west-commands.yml': textwrap.dedent(f'''\
                west-commands:
                  - file: scripts/echo.py
                    commands:
                      - name: greet
                        exec: {_PY}
                        help: greet through an interpreter
                '''),
        },
    )

    out = cmd_subprocess(['greet', 'foo', 'bar'], cwd=west_update_tmpdir)
    assert 'hello from exec extension' in out
    assert f'topdir={west_update_tmpdir}' in out
    assert 'project=net-tools' in out
    assert 'have_manifest=yes' in out
    # Everything after the command name is forwarded verbatim, including
    # arguments west knows nothing about.
    assert 'args=foo bar' in out


def test_exec_extension_interpreter_list(west_update_tmpdir):
    # 'exec' may be a list, e.g. to pass interpreter options.
    _add_exec_extension(
        west_update_tmpdir,
        files={
            'scripts/echo.py': _ENV_ECHO_PY,
            'scripts/west-commands.yml': textwrap.dedent(f'''\
                west-commands:
                  - file: scripts/echo.py
                    commands:
                      - name: greet
                        exec: [{_PY}, '-B']
                        help: greet through an interpreter with options
                '''),
        },
    )

    out = cmd_subprocess(['greet'], cwd=west_update_tmpdir)
    assert 'hello from exec extension' in out


@pytest.mark.skipif(WINDOWS, reason='direct execution needs a real executable on Windows')
def test_exec_extension_direct(west_update_tmpdir):
    # 'exec: true' runs the file directly (no interpreter). On POSIX a
    # shebang + executable bit makes a shell script directly runnable.
    _add_exec_extension(
        west_update_tmpdir,
        files={
            'scripts/greet.sh': textwrap.dedent('''\
                #!/bin/sh
                echo "hello from direct exec"
                echo "command=$WEST_COMMAND"
                echo "topdir=$WEST_TOPDIR"
                echo "args=$*"
                '''),
            'scripts/west-commands.yml': textwrap.dedent('''\
                west-commands:
                  - file: scripts/greet.sh
                    commands:
                      - name: greet
                        exec: true
                        help: greet from a shell script
                '''),
        },
        make_executable='scripts/greet.sh',
    )

    out = cmd_subprocess(['greet', 'foo', 'bar'], cwd=west_update_tmpdir)
    assert 'hello from direct exec' in out
    assert f'topdir={west_update_tmpdir}' in out
    assert 'args=foo bar' in out


def test_exec_extension_dispatch_on_command_name(west_update_tmpdir):
    # A single file registered under multiple names can tell which
    # subcommand was invoked through WEST_COMMAND.
    _add_exec_extension(
        west_update_tmpdir,
        files={
            'scripts/echo.py': _ENV_ECHO_PY,
            'scripts/west-commands.yml': textwrap.dedent(f'''\
                west-commands:
                  - file: scripts/echo.py
                    commands:
                      - name: sub-one
                        exec: {_PY}
                        help: first subcommand
                      - name: sub-two
                        exec: {_PY}
                        help: second subcommand
                '''),
        },
    )

    assert 'command=sub-one' in cmd_subprocess(['sub-one'], cwd=west_update_tmpdir)
    assert 'command=sub-two' in cmd_subprocess(['sub-two'], cwd=west_update_tmpdir)


def test_exec_extension_forwards_options(west_update_tmpdir):
    # Options that would otherwise look like west arguments (including -h)
    # must be forwarded to the executable, not intercepted by west.
    _add_exec_extension(
        west_update_tmpdir,
        files={
            'scripts/echo.py': _ENV_ECHO_PY,
            'scripts/west-commands.yml': textwrap.dedent(f'''\
                west-commands:
                  - file: scripts/echo.py
                    commands:
                      - name: echo-args
                        exec: {_PY}
                        help: echo forwarded arguments
                '''),
        },
    )

    out = cmd_subprocess(['echo-args', '--help', '-v', '--unknown'], cwd=west_update_tmpdir)
    assert 'args=--help -v --unknown' in out


def test_exec_extension_exit_code(west_update_tmpdir):
    # West propagates the executable's exit code.
    _add_exec_extension(
        west_update_tmpdir,
        files={
            'scripts/fail.py': 'import sys\nsys.exit(3)\n',
            'scripts/west-commands.yml': textwrap.dedent(f'''\
                west-commands:
                  - file: scripts/fail.py
                    commands:
                      - name: fail-cmd
                        exec: {_PY}
                        help: always fails
                '''),
        },
    )

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        cmd_subprocess(['fail-cmd'], cwd=west_update_tmpdir)
    assert exc_info.value.returncode == 3


@pytest.mark.skipif(WINDOWS, reason='file executable bit is a POSIX concept')
def test_exec_extension_not_executable(west_update_tmpdir):
    # 'exec: true' with a non-executable file produces a clear error.
    _add_exec_extension(
        west_update_tmpdir,
        files={
            'scripts/not-exec.sh': '#!/bin/sh\necho nope\n',
            'scripts/west-commands.yml': textwrap.dedent('''\
                west-commands:
                  - file: scripts/not-exec.sh
                    commands:
                      - name: not-exec
                        exec: true
                        help: not executable
                '''),
        },
        # Intentionally do not mark it executable.
    )

    _, err_msg = cmd_raises('not-exec', SystemExit)
    assert 'is not executable' in err_msg


@pytest.mark.parametrize('exec_value', ['true', _PY])
def test_exec_extension_file_not_found(west_update_tmpdir, exec_value):
    # A missing backing file reports "not found" rather than the misleading
    # "not executable", both when run directly and through an interpreter.
    # This check is platform-independent.
    _add_exec_extension(
        west_update_tmpdir,
        files={
            'scripts/west-commands.yml': textwrap.dedent(f'''\
                west-commands:
                  - file: scripts/missing
                    commands:
                      - name: missing-cmd
                        exec: {exec_value}
                        help: missing file
                '''),
        },
    )

    _, err_msg = cmd_raises('missing-cmd', SystemExit)
    assert 'not found' in err_msg


def test_exec_extension_class_and_exec_conflict(west_update_tmpdir):
    # 'class' and 'exec' are mutually exclusive on a command.
    _add_exec_extension(
        west_update_tmpdir,
        files={
            'scripts/echo.py': _ENV_ECHO_PY,
            'scripts/west-commands.yml': textwrap.dedent(f'''\
                west-commands:
                  - file: scripts/echo.py
                    commands:
                      - name: conflict-cmd
                        class: SomeClass
                        exec: {_PY}
                        help: invalid
                '''),
        },
    )

    _, err_msg = cmd_raises('conflict-cmd', SystemExit)
    assert "sets both 'class' and 'exec'" in err_msg


def test_exec_extension_invalid_exec_value(west_update_tmpdir):
    # 'exec' must be true, a string, or a list of strings.
    _add_exec_extension(
        west_update_tmpdir,
        files={
            'scripts/echo.py': _ENV_ECHO_PY,
            'scripts/west-commands.yml': textwrap.dedent('''\
                west-commands:
                  - file: scripts/echo.py
                    commands:
                      - name: bad-exec
                        exec: 123
                        help: invalid
                '''),
        },
    )

    _, err_msg = cmd_raises('bad-exec', SystemExit)
    assert "invalid 'exec' value" in err_msg


def test_exec_extension_directory_escape(west_update_tmpdir):
    # The file must not escape the project directory.
    _add_exec_extension(
        west_update_tmpdir,
        files={
            'scripts/west-commands.yml': textwrap.dedent('''\
                west-commands:
                  - file: ../../zephyr/evil.sh
                    commands:
                      - name: evil-exec
                        exec: true
                        help: escape attempt
                '''),
        },
    )

    _, err_msg = cmd_raises('evil-exec', SystemExit)
    assert 'escapes project path' in err_msg
