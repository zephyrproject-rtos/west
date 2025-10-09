# Copyright (c) 2019, Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

import configparser
import os
import pathlib
from typing import Any

import pytest
from conftest import cmd, cmd_raises

from west import configuration as config
from west.util import PathType

SYSTEM = config.ConfigFile.SYSTEM
GLOBAL = config.ConfigFile.GLOBAL
LOCAL = config.ConfigFile.LOCAL
ALL = config.ConfigFile.ALL

@pytest.fixture(autouse=True)
def autouse_config_tmpdir(config_tmpdir):
    # Since this module tests west's configuration file features,
    # adding autouse=True to the config_tmpdir fixture saves typing
    # and is less error-prone than using it below in every test case.
    pass

def cfg(f=ALL, topdir=None):
    # Load a fresh configuration object at the given level, and return it.
    cp = configparser.ConfigParser(allow_no_value=True)
    # TODO: convert this mechanism without the global deprecated read_config
    with pytest.deprecated_call():
        config.read_config(configfile=f, config=cp, topdir=topdir)
    return cp

def update_testcfg(section: str, key: str, value: Any,
                   configfile: config.ConfigFile = LOCAL,
                   topdir: PathType | None = None) -> None:
    c = config.Configuration(topdir)
    c.set(option=f'{section}.{key}', value=value, configfile=configfile)

def delete_testcfg(section: str, key: str,
                   configfile: config.ConfigFile | None = None,
                   topdir: PathType | None = None) -> None:
    c = config.Configuration(topdir)
    c.delete(option=f'{section}.{key}', configfile=configfile)

def test_config_global():
    # Set a global config option via the command interface. Make sure
    # it can be read back using the API calls and at the command line
    # at ALL and GLOBAL locations only.
    cmd('config --global pytest.global foo')

    assert cfg(f=GLOBAL)['pytest']['global'] == 'foo'
    assert cfg(f=ALL)['pytest']['global'] == 'foo'
    assert 'pytest' not in cfg(f=SYSTEM)
    assert 'pytest' not in cfg(f=LOCAL)
    assert cmd('config pytest.global').rstrip() == 'foo'
    assert cmd('config --global pytest.global').rstrip() == 'foo'

    # Make sure we can change the value of an existing variable.
    cmd('config --global pytest.global bar')

    assert cfg(f=GLOBAL)['pytest']['global'] == 'bar'
    assert cfg(f=ALL)['pytest']['global'] == 'bar'
    assert cmd('config pytest.global').rstrip() == 'bar'
    assert cmd('config --global pytest.global').rstrip() == 'bar'

    # Sanity check that we can create multiple variables per section.
    # Just use the API here; there's coverage already that the command line
    # and API match.
    cmd('config --global pytest.global2 foo2')

    all, glb, lcl = cfg(f=ALL), cfg(f=GLOBAL), cfg(f=LOCAL)
    assert all['pytest']['global'] == 'bar'
    assert glb['pytest']['global'] == 'bar'
    assert 'pytest' not in lcl
    assert all['pytest']['global2'] == 'foo2'
    assert glb['pytest']['global2'] == 'foo2'
    assert 'pytest' not in lcl

def test_config_local():
    # test_config_system for local variables.
    cmd('config --local pytest.local foo')

    assert cfg(f=LOCAL)['pytest']['local'] == 'foo'
    assert cfg(f=ALL)['pytest']['local'] == 'foo'
    assert 'pytest' not in cfg(f=SYSTEM)
    assert 'pytest' not in cfg(f=GLOBAL)
    assert cmd('config pytest.local').rstrip() == 'foo'
    assert cmd('config --local pytest.local').rstrip() == 'foo'

    cmd('config --local pytest.local bar')

    assert cfg(f=LOCAL)['pytest']['local'] == 'bar'
    assert cfg(f=ALL)['pytest']['local'] == 'bar'
    assert 'pytest' not in cfg(f=SYSTEM)
    assert 'pytest' not in cfg(f=GLOBAL)
    assert cmd('config pytest.local').rstrip() == 'bar'
    assert cmd('config --local pytest.local').rstrip() == 'bar'

    cmd('config --local pytest.local2 foo2')

    all, glb, lcl = cfg(f=ALL), cfg(f=GLOBAL), cfg(f=LOCAL)
    assert all['pytest']['local'] == 'bar'
    assert 'pytest' not in glb
    assert lcl['pytest']['local'] == 'bar'
    assert all['pytest']['local2'] == 'foo2'
    assert 'pytest' not in glb
    assert lcl['pytest']['local2'] == 'foo2'

def test_config_system():
    # Basic test of system-level configuration.

    update_testcfg('pytest', 'key', 'val', configfile=SYSTEM)
    assert cfg(f=ALL)['pytest']['key'] == 'val'
    assert cfg(f=SYSTEM)['pytest']['key'] == 'val'
    assert 'pytest' not in cfg(f=GLOBAL)
    assert 'pytest' not in cfg(f=LOCAL)

    update_testcfg('pytest', 'key', 'val2', configfile=SYSTEM)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'val2'

def test_config_system_precedence():
    # Test precedence rules, including system level.

    update_testcfg('pytest', 'key', 'sys', configfile=SYSTEM)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'sys'
    assert cfg(f=ALL)['pytest']['key'] == 'sys'

    update_testcfg('pytest', 'key', 'glb', configfile=GLOBAL)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'sys'
    assert cfg(f=GLOBAL)['pytest']['key'] == 'glb'
    assert cfg(f=ALL)['pytest']['key'] == 'glb'

    update_testcfg('pytest', 'key', 'lcl', configfile=LOCAL)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'sys'
    assert cfg(f=GLOBAL)['pytest']['key'] == 'glb'
    assert cfg(f=LOCAL)['pytest']['key'] == 'lcl'
    assert cfg(f=ALL)['pytest']['key'] == 'lcl'

def test_system_creation():
    # Test that the system file -- and just that file -- is created on
    # demand.

    assert not os.path.isfile(config._location(SYSTEM))
    assert not os.path.isfile(config._location(GLOBAL))
    assert not os.path.isfile(config._location(LOCAL))

    update_testcfg('pytest', 'key', 'val', configfile=SYSTEM)

    assert os.path.isfile(config._location(SYSTEM))
    assert not os.path.isfile(config._location(GLOBAL))
    assert not os.path.isfile(config._location(LOCAL))
    assert cfg(f=ALL)['pytest']['key'] == 'val'
    assert cfg(f=SYSTEM)['pytest']['key'] == 'val'
    assert 'pytest' not in cfg(f=GLOBAL)
    assert 'pytest' not in cfg(f=LOCAL)

def test_global_creation():
    # Like test_system_creation, for global config options.

    assert not os.path.isfile(config._location(SYSTEM))
    assert not os.path.isfile(config._location(GLOBAL))
    assert not os.path.isfile(config._location(LOCAL))

    update_testcfg('pytest', 'key', 'val', configfile=GLOBAL)

    assert not os.path.isfile(config._location(SYSTEM))
    assert os.path.isfile(config._location(GLOBAL))
    assert not os.path.isfile(config._location(LOCAL))
    assert cfg(f=ALL)['pytest']['key'] == 'val'
    assert 'pytest' not in cfg(f=SYSTEM)
    assert cfg(f=GLOBAL)['pytest']['key'] == 'val'
    assert 'pytest' not in cfg(f=LOCAL)

def test_local_creation():
    # Like test_system_creation, for local config options.

    assert not os.path.isfile(config._location(SYSTEM))
    assert not os.path.isfile(config._location(GLOBAL))
    assert not os.path.isfile(config._location(LOCAL))

    update_testcfg('pytest', 'key', 'val', configfile=LOCAL)

    assert not os.path.isfile(config._location(SYSTEM))
    assert not os.path.isfile(config._location(GLOBAL))
    assert os.path.isfile(config._location(LOCAL))
    assert cfg(f=ALL)['pytest']['key'] == 'val'
    assert 'pytest' not in cfg(f=SYSTEM)
    assert 'pytest' not in cfg(f=GLOBAL)
    assert cfg(f=LOCAL)['pytest']['key'] == 'val'

def test_local_creation_with_topdir():
    # Like test_local_creation, with a specified topdir.

    system = pathlib.Path(config._location(SYSTEM))
    glbl = pathlib.Path(config._location(GLOBAL))
    local = pathlib.Path(config._location(LOCAL))

    topdir = pathlib.Path(os.getcwd()) / 'test-topdir'
    topdir_west = topdir / '.west'
    assert not topdir_west.exists()
    topdir_west.mkdir(parents=True)
    topdir_config = topdir_west / 'config'

    assert not system.exists()
    assert not glbl.exists()
    assert not local.exists()
    assert not topdir_config.exists()

    # The autouse fixture at the top of this file has set up an
    # environment variable for our local config file. Disable it
    # to make sure the API works with a 'real' topdir.
    del os.environ['WEST_CONFIG_LOCAL']

    # We should be able to write into our topdir's config file now.
    update_testcfg('pytest', 'key', 'val', configfile=LOCAL, topdir=str(topdir))
    assert not system.exists()
    assert not glbl.exists()
    assert not local.exists()
    assert topdir_config.exists()

    assert cfg(f=ALL, topdir=str(topdir))['pytest']['key'] == 'val'
    assert 'pytest' not in cfg(f=SYSTEM)
    assert 'pytest' not in cfg(f=GLOBAL)
    assert cfg(f=LOCAL, topdir=str(topdir))['pytest']['key'] == 'val'

def test_append():
    update_testcfg('pytest', 'key', 'system', configfile=SYSTEM)
    update_testcfg('pytest', 'key', 'global', configfile=GLOBAL)
    update_testcfg('pytest', 'key', 'local', configfile=LOCAL)
    # Appending with no configfile specified should modify the local one
    cmd('config -a pytest.key ,bar')

    # Only the local one will be modified
    assert cfg(f=SYSTEM)['pytest']['key'] == 'system'
    assert cfg(f=GLOBAL)['pytest']['key'] == 'global'
    assert cfg(f=LOCAL)['pytest']['key'] == 'local,bar'

    # Test a more complex one, and at a particular configfile level
    update_testcfg('build', 'cmake-args', '-DCONF_FILE=foo.conf', configfile=GLOBAL)
    assert cfg(f=GLOBAL)['build']['cmake-args'] == '-DCONF_FILE=foo.conf'

    # Use a list instead of a string to avoid one level of nested quoting
    cmd(['config', '--global', '-a', 'build.cmake-args', '--',
         ' -DEXTRA_CFLAGS=\'-Wextra -g0\' -DFOO=BAR'])

    assert cfg(f=GLOBAL)['build']['cmake-args'] == \
        '-DCONF_FILE=foo.conf -DEXTRA_CFLAGS=\'-Wextra -g0\' -DFOO=BAR'

def test_append_novalue():
    _, err_msg = cmd_raises('config -a pytest.foo', SystemExit)
    assert '-a requires both name and value' in err_msg

def test_append_notfound():
    update_testcfg('pytest', 'key', 'val', configfile=LOCAL)
    _, err_msg = cmd_raises('config -a pytest.foo bar', SystemExit)
    assert 'option pytest.foo not found in the local configuration file' in err_msg


def test_delete_basic():
    # Basic deletion test: write local, verify global and system deletions
    # don't work, then delete local does work.
    update_testcfg('pytest', 'key', 'val', configfile=LOCAL)
    assert cfg(f=ALL)['pytest']['key'] == 'val'
    with pytest.raises(KeyError):
        delete_testcfg('pytest', 'key', configfile=SYSTEM)
    with pytest.raises(KeyError):
        delete_testcfg('pytest', 'key', configfile=GLOBAL)
    delete_testcfg('pytest', 'key', configfile=LOCAL)
    assert 'pytest' not in cfg(f=ALL)

def test_delete_all():
    # Deleting ConfigFile.ALL should delete from everywhere.
    update_testcfg('pytest', 'key', 'system', configfile=SYSTEM)
    update_testcfg('pytest', 'key', 'global', configfile=GLOBAL)
    update_testcfg('pytest', 'key', 'local', configfile=LOCAL)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'system'
    assert cfg(f=GLOBAL)['pytest']['key'] == 'global'
    assert cfg(f=LOCAL)['pytest']['key'] == 'local'
    delete_testcfg('pytest', 'key', configfile=ALL)
    assert 'pytest' not in cfg(f=ALL)

def test_delete_none():
    # Deleting None should delete from lowest-precedence global or
    # local file only.
    # Only supported with the deprecated call
    update_testcfg('pytest', 'key', 'system', configfile=SYSTEM)
    update_testcfg('pytest', 'key', 'global', configfile=GLOBAL)
    update_testcfg('pytest', 'key', 'local', configfile=LOCAL)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'system'
    assert cfg(f=GLOBAL)['pytest']['key'] == 'global'
    assert cfg(f=LOCAL)['pytest']['key'] == 'local'
    delete_testcfg('pytest', 'key', configfile=None)
    assert cfg(f=ALL)['pytest']['key'] == 'global'
    delete_testcfg('pytest', 'key', configfile=None)
    assert cfg(f=ALL)['pytest']['key'] == 'system'
    with pytest.raises(KeyError), pytest.deprecated_call():
        config.delete_config('pytest', 'key', configfile=None)

    # Using the Configuration Class this does remove from system
    delete_testcfg('pytest', 'key', configfile=None)
    assert 'pytest' not in cfg(f=ALL)

def test_delete_list():
    # Test delete of a list of places.
    # Only supported with the deprecated call
    update_testcfg('pytest', 'key', 'system', configfile=SYSTEM)
    update_testcfg('pytest', 'key', 'global', configfile=GLOBAL)
    update_testcfg('pytest', 'key', 'local', configfile=LOCAL)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'system'
    assert cfg(f=GLOBAL)['pytest']['key'] == 'global'
    assert cfg(f=LOCAL)['pytest']['key'] == 'local'
    with pytest.deprecated_call():
        config.delete_config('pytest', 'key', configfile=[GLOBAL, LOCAL])
    assert cfg(f=SYSTEM)['pytest']['key'] == 'system'
    assert 'pytest' not in cfg(f=GLOBAL)
    assert 'pytest' not in cfg(f=LOCAL)

def test_delete_system():
    # Test SYSTEM-only delete.
    update_testcfg('pytest', 'key', 'system', configfile=SYSTEM)
    update_testcfg('pytest', 'key', 'global', configfile=GLOBAL)
    update_testcfg('pytest', 'key', 'local', configfile=LOCAL)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'system'
    assert cfg(f=GLOBAL)['pytest']['key'] == 'global'
    assert cfg(f=LOCAL)['pytest']['key'] == 'local'
    delete_testcfg('pytest', 'key', configfile=SYSTEM)
    assert 'pytest' not in cfg(f=SYSTEM)
    assert cfg(f=GLOBAL)['pytest']['key'] == 'global'
    assert cfg(f=LOCAL)['pytest']['key'] == 'local'

def test_delete_global():
    # Test GLOBAL-only delete.
    update_testcfg('pytest', 'key', 'system', configfile=SYSTEM)
    update_testcfg('pytest', 'key', 'global', configfile=GLOBAL)
    update_testcfg('pytest', 'key', 'local', configfile=LOCAL)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'system'
    assert cfg(f=GLOBAL)['pytest']['key'] == 'global'
    assert cfg(f=LOCAL)['pytest']['key'] == 'local'
    delete_testcfg('pytest', 'key', configfile=GLOBAL)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'system'
    assert 'pytest' not in cfg(f=GLOBAL)
    assert cfg(f=LOCAL)['pytest']['key'] == 'local'

def test_delete_local():
    # Test LOCAL-only delete.
    update_testcfg('pytest', 'key', 'system', configfile=SYSTEM)
    update_testcfg('pytest', 'key', 'global', configfile=GLOBAL)
    update_testcfg('pytest', 'key', 'local', configfile=LOCAL)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'system'
    assert cfg(f=GLOBAL)['pytest']['key'] == 'global'
    assert cfg(f=LOCAL)['pytest']['key'] == 'local'
    delete_testcfg('pytest', 'key', configfile=LOCAL)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'system'
    assert cfg(f=GLOBAL)['pytest']['key'] == 'global'
    assert 'pytest' not in cfg(f=LOCAL)

def test_delete_local_with_topdir():
    # Test LOCAL-only delete with specified topdir.
    update_testcfg('pytest', 'key', 'system', configfile=SYSTEM)
    update_testcfg('pytest', 'key', 'global', configfile=GLOBAL)
    update_testcfg('pytest', 'key', 'local', configfile=LOCAL)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'system'
    assert cfg(f=GLOBAL)['pytest']['key'] == 'global'
    assert cfg(f=LOCAL)['pytest']['key'] == 'local'
    delete_testcfg('pytest', 'key', configfile=LOCAL)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'system'
    assert cfg(f=GLOBAL)['pytest']['key'] == 'global'
    assert 'pytest' not in cfg(f=LOCAL)

def test_delete_local_one():
    # Test LOCAL-only delete of one option doesn't affect the other.
    update_testcfg('pytest', 'key1', 'foo', configfile=LOCAL)
    update_testcfg('pytest', 'key2', 'bar', configfile=LOCAL)
    delete_testcfg('pytest', 'key1', configfile=LOCAL)
    assert 'pytest' in cfg(f=LOCAL)
    assert cfg(f=LOCAL)['pytest']['key2'] == 'bar'

def test_delete_cmd_all():
    # west config -D should delete from everywhere
    cmd('config --system pytest.key system')
    cmd('config --global pytest.key global')
    cmd('config --local pytest.key local')
    assert cfg(f=ALL)['pytest']['key'] == 'local'
    cmd('config -D pytest.key')
    assert 'pytest' not in cfg(f=ALL)
    with pytest.raises(SystemExit):
        cmd('config -D pytest.key')

def test_delete_cmd_none():
    # west config -d should delete from lowest-precedence global or
    # local file only.
    cmd('config --system pytest.key system')
    cmd('config --global pytest.key global')
    cmd('config --local pytest.key local')
    cmd('config -d pytest.key')
    assert cmd('config pytest.key').rstrip() == 'global'
    cmd('config -d pytest.key')
    assert cmd('config pytest.key').rstrip() == 'system'
    with pytest.raises(SystemExit):
        cmd('config -d pytest.key')

def test_delete_cmd_system():
    # west config -d --system should only delete from system
    cmd('config --system pytest.key system')
    cmd('config --global pytest.key global')
    cmd('config --local pytest.key local')
    cmd('config -d --system pytest.key')
    with pytest.raises(SystemExit):
        cmd('config --system pytest.key')
    assert cmd('config --global pytest.key').rstrip() == 'global'
    assert cmd('config --local pytest.key').rstrip() == 'local'

def test_delete_cmd_global():
    # west config -d --global should only delete from global
    cmd('config --system pytest.key system')
    cmd('config --global pytest.key global')
    cmd('config --local pytest.key local')
    cmd('config -d --global pytest.key')
    assert cmd('config --system pytest.key').rstrip() == 'system'
    with pytest.raises(SystemExit):
        cmd('config --global pytest.key')
    assert cmd('config --local pytest.key').rstrip() == 'local'

def test_delete_cmd_local():
    # west config -d --local should only delete from local
    cmd('config --system pytest.key system')
    cmd('config --global pytest.key global')
    cmd('config --local pytest.key local')
    cmd('config -d --local pytest.key')
    assert cmd('config --system pytest.key').rstrip() == 'system'
    assert cmd('config --global pytest.key').rstrip() == 'global'
    with pytest.raises(SystemExit):
        cmd('config --local pytest.key')

def test_delete_cmd_error():
    # Verify illegal combinations of flags error out.
    _, err_msg = cmd_raises('config -l -d pytest.key', SystemExit)
    assert 'argument -d/--delete: not allowed with argument -l/--list' in err_msg
    _, err_msg = cmd_raises('config -l -D pytest.key', SystemExit)
    assert 'argument -D/--delete-all: not allowed with argument -l/--list' in err_msg
    _, err_msg = cmd_raises('config -d -D pytest.key', SystemExit)
    assert 'argument -D/--delete-all: not allowed with argument -d/--delete' in err_msg

def test_default_config():
    # Writing to a value without a config destination should default
    # to --local.
    cmd('config pytest.local foo')

    assert cmd('config pytest.local').rstrip() == 'foo'
    assert cmd('config --local pytest.local').rstrip() == 'foo'
    assert cfg(f=ALL)['pytest']['local'] == 'foo'
    assert 'pytest' not in cfg(f=SYSTEM)
    assert 'pytest' not in cfg(f=GLOBAL)
    assert cfg(f=LOCAL)['pytest']['local'] == 'foo'

def test_config_precedence():
    # Verify that local settings take precedence over global ones,
    # but that both values are still available, and that setting
    # either doesn't affect system settings.
    cmd('config --global pytest.precedence global')
    cmd('config --local pytest.precedence local')

    assert cmd('config --global pytest.precedence').rstrip() == 'global'
    assert cmd('config --local pytest.precedence').rstrip() == 'local'
    assert cmd('config pytest.precedence').rstrip() == 'local'
    assert cfg(f=ALL)['pytest']['precedence'] == 'local'
    assert 'pytest' not in cfg(f=SYSTEM)
    assert cfg(f=GLOBAL)['pytest']['precedence'] == 'global'
    assert cfg(f=LOCAL)['pytest']['precedence'] == 'local'

def test_config_missing_key():
    _, err_msg = cmd_raises('config pytest', SystemExit)
    assert 'invalid configuration option "pytest"; expected "section.key" format' in err_msg


def test_unset_config():
    # Getting unset configuration options should raise an error.
    # With verbose output, the exact missing option should be printed.
    _, err_msg = cmd_raises('-v config pytest.missing', SystemExit)
    assert 'pytest.missing is unset' in err_msg

def test_no_args():
    _, err_msg = cmd_raises('config', SystemExit)
    assert 'missing argument name' in err_msg

def test_list():
    def sorted_list(other_args=''):
        return list(sorted(cmd('config -l ' + other_args).splitlines()))

    _, err_msg = cmd_raises('config -l pytest.foo', SystemExit)
    assert '-l cannot be combined with name argument' in err_msg

    assert cmd('config -l').strip() == ''

    cmd('config pytest.foo who')
    assert sorted_list() == ['pytest.foo=who']

    cmd('config pytest.bar what')
    assert sorted_list() == ['pytest.bar=what',
                             'pytest.foo=who']

    cmd('config --global pytest.baz where')
    assert sorted_list() == ['pytest.bar=what',
                             'pytest.baz=where',
                             'pytest.foo=who']
    assert sorted_list('--system') == []
    assert sorted_list('--global') == ['pytest.baz=where']
    assert sorted_list('--local') == ['pytest.bar=what',
                                      'pytest.foo=who']

def test_round_trip():
    cmd('config pytest.foo bar,baz')
    assert cmd('config pytest.foo').strip() == 'bar,baz'
