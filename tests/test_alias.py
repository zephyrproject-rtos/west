# Copyright (c) 2024, Basalte bv
#
# SPDX-License-Identifier: Apache-2.0

import io

import pytest
from conftest import cmd, cmd_raises


@pytest.fixture(autouse=True)
def autouse_tmpdir(config_tmpdir, west_init_tmpdir):
    # Since this module tests west's configuration file features,
    # adding autouse=True to the config_tmpdir and west_init_tmpdir fixtures
    # saves typing and is less error-prone than using it below in every test case.
    pass


def test_alias_commands():
    cmd('config set alias.test1 topdir')
    cmd('config set --global alias.test2 topdir')
    cmd('config set --system alias.test3 topdir')

    topdir_out = cmd('topdir')

    assert cmd('test1') == topdir_out
    assert cmd('test2') == topdir_out
    assert cmd('test3') == topdir_out


def test_alias_help():
    cmd('config set alias.test topdir')

    # `west help <alias>` walks the alias chain to its target and shows
    # that command's help. For `alias.test=topdir`, the alias help
    # output must therefore equal the underlying `topdir --help` text
    # byte-for-byte.
    assert cmd('help test') == cmd('topdir --help')


def test_alias_recursive_commands():
    list_format = '{revision} TESTALIAS {name}'
    cmd(['config', 'set', 'alias.test1', f'list -f "{list_format}"'])
    cmd('config set alias.test2 test1')

    assert cmd('test2') == cmd(['list', '-f', list_format])


def test_alias_infinite_recursion():
    cmd('config set alias.test1 test2')
    cmd('config set alias.test2 test3')
    cmd('config set alias.test3 test1')

    stderr = io.StringIO()
    _, captured_stderr = cmd_raises('test1', SystemExit)
    stderr.write(captured_stderr)
    assert 'unknown command: test1' in stderr.getvalue()


def test_alias_empty():
    cmd(['config', 'set', 'alias.empty', ''])

    # help command shouldn't fail
    cmd('help')

    _, captured_stderr = cmd_raises('empty', SystemExit)
    assert 'alias "empty" is empty' in captured_stderr


def test_alias_early_args():
    cmd('config set alias.test1 topdir')

    # An alias invocation must succeed when verbose flags are passed
    # before the alias token (the alias resolver runs after top-level
    # flag parsing). The actual workspace topdir is the only thing
    # `topdir` ever prints.
    stderr = io.StringIO()
    assert cmd('-v test1', stderr=stderr).strip() != ''


def test_alias_command_with_arguments():
    list_format = '{revision} TESTALIAS {name}'
    cmd(['config', 'set', 'alias.revs', f'list -f "{list_format}"'])

    assert cmd('revs') == cmd(['list', '-f', list_format])


def test_alias_override():
    before = cmd('list')
    list_format = '{name} : {revision}'
    formatted = cmd(['list', '-f', list_format])

    cmd(['config', 'set', 'alias.list', f'list -f "{list_format}"'])

    after = cmd('list')

    assert before != after
    assert formatted == after
