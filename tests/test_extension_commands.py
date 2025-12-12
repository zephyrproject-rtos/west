# Copyright (c) 2025 Basalte bv
#
# SPDX-License-Identifier: Apache-2.0

import textwrap

from conftest import add_commit, cmd, cmd_raises


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
