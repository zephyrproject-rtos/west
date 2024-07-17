# Copyright (c) 2024, Basalte bv
#
# SPDX-License-Identifier: Apache-2.0

import os
import subprocess

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

def test_alias_help():
    cmd('config alias.test topdir')

    help_out = cmd('help test')

    assert "An alias that expands to: topdir" in help_out
    assert cmd('-h test') == help_out

def test_alias_recursive_commands():
    list_format = '{revision} TESTALIAS {name}'
    cmd(['config', 'alias.test1', f'list -f "{list_format}"'])
    cmd('config alias.test2 test1')

    assert cmd('test2') == cmd(['list', '-f', list_format])

def test_alias_infinite_recursion():
    cmd('config alias.test1 test2')
    cmd('config alias.test2 test3')
    cmd('config alias.test3 test1')

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        cmd('test1', stderr=subprocess.STDOUT)

    assert 'unknown command "test1";' in str(excinfo.value.stdout)

def test_alias_empty():
    cmd(['config', 'alias.empty', ''])

    # help command shouldn't fail
    cmd('help')

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        cmd('empty', stderr=subprocess.STDOUT)

    assert 'empty alias "empty"' in str(excinfo.value.stdout)

def test_alias_early_args():
    cmd('config alias.test1 topdir')

    # An alias with an early command argument shouldn't fail
    assert "Replacing alias test1 with ['topdir']" in cmd('-v test1')

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
