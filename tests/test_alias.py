# Copyright (c) 2024, Basalte bv

import os

import pytest

from conftest import cmd

assert 'TOXTEMPDIR' in os.environ, "you must run these tests using tox"

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

def test_alias_recursive_commands():
    cmd('config alias.test1 topdir')
    cmd('config alias.test2 test1')

    assert cmd('test2') == cmd('topdir')

def test_alias_command_with_arguments():
    list_format = '{revision}'
    cmd(f'config alias.revs "list -f \"{list_format}\""')

    assert cmd('revs') == cmd(f'list -f "{list_format}"')

def test_alias_override():
    before = cmd('list')
    list_format = '{name}:{revision}'
    formatted = cmd(f'list -f "{list_format}"')

    cmd(f'config alias.list "list -f \"{list_format}\""')

    after = cmd('list')

    assert before != after
    assert formatted == after
