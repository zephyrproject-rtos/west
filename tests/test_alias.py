# Copyright (c) 2024, Basalte bv
#
# SPDX-License-Identifier: Apache-2.0

import pytest
from conftest import cmd, cmd_raises


@pytest.fixture(autouse=True)
def autouse_tmpdir(config_tmpdir, west_init_tmpdir):
    # Since this module tests west's configuration file features,
    # adding autouse=True to the config_tmpdir and west_init_tmpdir fixtures
    # saves typing and is less error-prone than using it below in every test case.
    pass


def test_alias_commands():
    cmd('config alias.test1 topdir')
    cmd('config --global alias.test2 topdir')
    cmd('config --system alias.test3 topdir')

    topdir_out = cmd('topdir')

    assert cmd('test1') == topdir_out
    assert cmd('test2') == topdir_out
    assert cmd('test3') == topdir_out


def test_alias_help():
    cmd('config alias.test topdir')

    help_out = cmd('help test')

    assert "An alias that expands to: topdir" in help_out
    assert cmd('-h test') == help_out
    # The long option must behave like the short one.
    assert cmd('--help test') == help_out


def test_alias_recursive_commands():
    list_format = '{revision} TESTALIAS {name}'
    cmd(['config', 'alias.test1', f'list -f "{list_format}"'])
    cmd('config alias.test2 test1')

    assert cmd('test2') == cmd(['list', '-f', list_format])


def test_alias_infinite_recursion():
    cmd('config alias.test1 test2')
    cmd('config alias.test2 test3')
    cmd('config alias.test3 test1')

    exc, _ = cmd_raises('test1', SystemExit)
    assert 'unknown command "test1";' in str(exc.value)


def test_alias_empty():
    cmd(['config', 'alias.empty', ''])

    # help command shouldn't fail
    cmd('help')

    exc, _ = cmd_raises('empty', SystemExit)
    assert 'empty alias "empty"' in str(exc.value)


def test_alias_early_args():
    cmd('config alias.test1 topdir')

    # An alias with an early command argument shouldn't fail
    assert "Replacing alias test1 with ['topdir']" in cmd('-v test1')
    assert "Replacing alias test1 with ['topdir']" in cmd('--verbose test1')


def test_alias_early_args_with_values():
    # Early args taking a value must not swallow the alias name, in any
    # of the forms the top level parser accepts.
    cmd('config alias.test1 topdir')

    topdir_out = cmd('topdir')

    assert cmd(['-z', '/some/path', 'test1']) == topdir_out
    assert cmd(['-z/some/path', 'test1']) == topdir_out
    assert cmd(['-z=/some/path', 'test1']) == topdir_out
    assert cmd(['--zephyr-base', '/some/path', 'test1']) == topdir_out
    assert cmd(['--zephyr-base=/some/path', 'test1']) == topdir_out


def test_alias_expands_to_early_arg():
    # An alias whose expansion starts with an early/global option (e.g. -v)
    # should apply that option to west itself instead of mistaking it for the
    # command name.
    cmd(['config', 'alias.test1', '-v topdir'])

    output = cmd('test1')

    # The '-v' from the alias enables debug output, proving it was handled as
    # an early arg (this line is not printed without increased verbosity).
    assert "Replacing alias test1 with ['-v', 'topdir']" in output
    # ... and the actual command still runs.
    assert cmd('topdir').strip() in output


def test_alias_expands_to_early_arg_recursive():
    # An early arg introduced by an alias must survive further alias
    # expansion, i.e. the expanded argv is re-parsed on every iteration.
    cmd(['config', 'alias.test1', '-v test2'])
    cmd(['config', 'alias.test2', 'topdir'])

    output = cmd('test1')

    assert "Replacing alias test1 with ['-v', 'test2']" in output
    assert "Replacing alias test2 with ['topdir']" in output
    assert cmd('topdir').strip() in output


def test_alias_early_arg_with_trailing_args():
    # User arguments given after the alias are preserved and appended after
    # the alias expansion (which itself starts with an early arg).
    cmd(['config', 'alias.test1', '-v list'])

    output = cmd(['test1', '-f', '{name}'])

    assert "Replacing alias test1 with ['-v', 'list']" in output
    assert cmd(['list', '-f', '{name}']).strip() in output


def test_alias_expands_to_early_arg_with_value():
    # Early args from an alias that take a value work too, and don't
    # stop the expansion from finding the command name.
    cmd(['config', 'alias.test1', '-z /some/path topdir'])
    cmd(['config', 'alias.test2', '--zephyr-base /some/path topdir'])

    topdir_out = cmd('topdir')

    assert cmd('test1') == topdir_out
    assert cmd('test2') == topdir_out


def test_alias_expands_to_version():
    # "-V" from an alias prints west's version instead of being
    # mistaken for a command name.
    cmd(['config', '--', 'alias.test1', '-V'])

    assert 'West version:' in cmd('test1')


def test_alias_expands_to_help():
    # "-h" from an alias asks for help, just like a "-h" typed by the user.
    cmd(['config', '--', 'alias.test1', '-h topdir'])

    assert cmd('test1') == cmd('help topdir')


def test_alias_command_with_arguments():
    list_format = '{revision} TESTALIAS {name}'
    cmd(['config', 'alias.revs', f'list -f "{list_format}"'])

    assert cmd('revs') == cmd(['list', '-f', list_format])


def test_alias_override():
    before = cmd('list')
    list_format = '{name} : {revision}'
    formatted = cmd(['list', '-f', list_format])

    cmd(['config', 'alias.list', f'list -f "{list_format}"'])

    after = cmd('list')

    assert before != after
    assert formatted == after
