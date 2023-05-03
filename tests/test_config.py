# Copyright (c) 2019, Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

import configparser
import os
import pathlib
import subprocess

import pytest

from west import configuration as config

from conftest import cmd

assert 'TOXTEMPDIR' in os.environ, "you must run these tests using tox"

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
    config.read_config(configfile=f, config=cp, topdir=topdir)
    return cp

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

    config.update_config('pytest', 'key', 'val', configfile=SYSTEM)
    assert cfg(f=ALL)['pytest']['key'] == 'val'
    assert cfg(f=SYSTEM)['pytest']['key'] == 'val'
    assert 'pytest' not in cfg(f=GLOBAL)
    assert 'pytest' not in cfg(f=LOCAL)

    config.update_config('pytest', 'key', 'val2', configfile=SYSTEM)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'val2'

def test_config_system_precedence():
    # Test precedence rules, including system level.

    config.update_config('pytest', 'key', 'sys', configfile=SYSTEM)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'sys'
    assert cfg(f=ALL)['pytest']['key'] == 'sys'

    config.update_config('pytest', 'key', 'glb', configfile=GLOBAL)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'sys'
    assert cfg(f=GLOBAL)['pytest']['key'] == 'glb'
    assert cfg(f=ALL)['pytest']['key'] == 'glb'

    config.update_config('pytest', 'key', 'lcl', configfile=LOCAL)
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

    config.update_config('pytest', 'key', 'val', configfile=SYSTEM)

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

    config.update_config('pytest', 'key', 'val', configfile=GLOBAL)

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

    config.update_config('pytest', 'key', 'val', configfile=LOCAL)

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
    config.update_config('pytest', 'key', 'val', configfile=LOCAL,
                         topdir=str(topdir))
    assert not system.exists()
    assert not glbl.exists()
    assert not local.exists()
    assert topdir_config.exists()

    assert cfg(f=ALL, topdir=str(topdir))['pytest']['key'] == 'val'
    assert 'pytest' not in cfg(f=SYSTEM)
    assert 'pytest' not in cfg(f=GLOBAL)
    assert cfg(f=LOCAL, topdir=str(topdir))['pytest']['key'] == 'val'

def test_delete_basic():
    # Basic deletion test: write local, verify global and system deletions
    # don't work, then delete local does work.
    config.update_config('pytest', 'key', 'val', configfile=LOCAL)
    assert cfg(f=ALL)['pytest']['key'] == 'val'
    with pytest.raises(KeyError):
        config.delete_config('pytest', 'key', configfile=SYSTEM)
    with pytest.raises(KeyError):
        config.delete_config('pytest', 'key', configfile=GLOBAL)
    config.delete_config('pytest', 'key', configfile=LOCAL)
    assert 'pytest' not in cfg(f=ALL)

def test_delete_all():
    # Deleting ConfigFile.ALL should delete from everywhere.
    config.update_config('pytest', 'key', 'system', configfile=SYSTEM)
    config.update_config('pytest', 'key', 'global', configfile=GLOBAL)
    config.update_config('pytest', 'key', 'local', configfile=LOCAL)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'system'
    assert cfg(f=GLOBAL)['pytest']['key'] == 'global'
    assert cfg(f=LOCAL)['pytest']['key'] == 'local'
    config.delete_config('pytest', 'key', configfile=ALL)
    assert 'pytest' not in cfg(f=ALL)

def test_delete_none():
    # Deleting None should delete from lowest-precedence global or
    # local file only.
    config.update_config('pytest', 'key', 'system', configfile=SYSTEM)
    config.update_config('pytest', 'key', 'global', configfile=GLOBAL)
    config.update_config('pytest', 'key', 'local', configfile=LOCAL)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'system'
    assert cfg(f=GLOBAL)['pytest']['key'] == 'global'
    assert cfg(f=LOCAL)['pytest']['key'] == 'local'
    config.delete_config('pytest', 'key', configfile=None)
    assert cfg(f=ALL)['pytest']['key'] == 'global'
    config.delete_config('pytest', 'key', configfile=None)
    assert cfg(f=ALL)['pytest']['key'] == 'system'
    with pytest.raises(KeyError):
        config.delete_config('pytest', 'key', configfile=None)

def test_delete_list():
    # Test delete of a list of places.
    config.update_config('pytest', 'key', 'system', configfile=SYSTEM)
    config.update_config('pytest', 'key', 'global', configfile=GLOBAL)
    config.update_config('pytest', 'key', 'local', configfile=LOCAL)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'system'
    assert cfg(f=GLOBAL)['pytest']['key'] == 'global'
    assert cfg(f=LOCAL)['pytest']['key'] == 'local'
    config.delete_config('pytest', 'key', configfile=[GLOBAL, LOCAL])
    assert cfg(f=SYSTEM)['pytest']['key'] == 'system'
    assert 'pytest' not in cfg(f=GLOBAL)
    assert 'pytest' not in cfg(f=LOCAL)

def test_delete_system():
    # Test SYSTEM-only delete.
    config.update_config('pytest', 'key', 'system', configfile=SYSTEM)
    config.update_config('pytest', 'key', 'global', configfile=GLOBAL)
    config.update_config('pytest', 'key', 'local', configfile=LOCAL)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'system'
    assert cfg(f=GLOBAL)['pytest']['key'] == 'global'
    assert cfg(f=LOCAL)['pytest']['key'] == 'local'
    config.delete_config('pytest', 'key', configfile=SYSTEM)
    assert 'pytest' not in cfg(f=SYSTEM)
    assert cfg(f=GLOBAL)['pytest']['key'] == 'global'
    assert cfg(f=LOCAL)['pytest']['key'] == 'local'

def test_delete_global():
    # Test GLOBAL-only delete.
    config.update_config('pytest', 'key', 'system', configfile=SYSTEM)
    config.update_config('pytest', 'key', 'global', configfile=GLOBAL)
    config.update_config('pytest', 'key', 'local', configfile=LOCAL)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'system'
    assert cfg(f=GLOBAL)['pytest']['key'] == 'global'
    assert cfg(f=LOCAL)['pytest']['key'] == 'local'
    config.delete_config('pytest', 'key', configfile=GLOBAL)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'system'
    assert 'pytest' not in cfg(f=GLOBAL)
    assert cfg(f=LOCAL)['pytest']['key'] == 'local'

def test_delete_local():
    # Test LOCAL-only delete.
    config.update_config('pytest', 'key', 'system', configfile=SYSTEM)
    config.update_config('pytest', 'key', 'global', configfile=GLOBAL)
    config.update_config('pytest', 'key', 'local', configfile=LOCAL)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'system'
    assert cfg(f=GLOBAL)['pytest']['key'] == 'global'
    assert cfg(f=LOCAL)['pytest']['key'] == 'local'
    config.delete_config('pytest', 'key', configfile=LOCAL)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'system'
    assert cfg(f=GLOBAL)['pytest']['key'] == 'global'
    assert 'pytest' not in cfg(f=LOCAL)

def test_delete_local_with_topdir():
    # Test LOCAL-only delete with specified topdir.
    config.update_config('pytest', 'key', 'system', configfile=SYSTEM)
    config.update_config('pytest', 'key', 'global', configfile=GLOBAL)
    config.update_config('pytest', 'key', 'local', configfile=LOCAL)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'system'
    assert cfg(f=GLOBAL)['pytest']['key'] == 'global'
    assert cfg(f=LOCAL)['pytest']['key'] == 'local'
    config.delete_config('pytest', 'key', configfile=LOCAL)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'system'
    assert cfg(f=GLOBAL)['pytest']['key'] == 'global'
    assert 'pytest' not in cfg(f=LOCAL)

def test_delete_local_one():
    # Test LOCAL-only delete of one option doesn't affect the other.
    config.update_config('pytest', 'key1', 'foo', configfile=LOCAL)
    config.update_config('pytest', 'key2', 'bar', configfile=LOCAL)
    config.delete_config('pytest', 'key1', configfile=LOCAL)
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
    with pytest.raises(subprocess.CalledProcessError):
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
    with pytest.raises(subprocess.CalledProcessError):
        cmd('config -d pytest.key')

def test_delete_cmd_system():
    # west config -d --system should only delete from system
    cmd('config --system pytest.key system')
    cmd('config --global pytest.key global')
    cmd('config --local pytest.key local')
    cmd('config -d --system pytest.key')
    with pytest.raises(subprocess.CalledProcessError):
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
    with pytest.raises(subprocess.CalledProcessError):
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
    with pytest.raises(subprocess.CalledProcessError):
        cmd('config --local pytest.key')

def test_delete_cmd_error():
    # Verify illegal combinations of flags error out.
    with pytest.raises(subprocess.CalledProcessError) as e:
        cmd('config -l -d pytest.key')
        assert '-l cannot be combined with -d or -D' in str(e)
    with pytest.raises(subprocess.CalledProcessError) as e:
        cmd('config -l -D pytest.key')
        assert '-l cannot be combined with -d or -D' in str(e)
    with pytest.raises(subprocess.CalledProcessError) as e:
        cmd('config -d -D pytest.key')
        assert '-d cannot be combined with -D' in str(e)

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
    with pytest.raises(subprocess.CalledProcessError) as e:
        cmd('config pytest')
        assert str(e) == 'west config: error: missing key, please invoke ' \
            'as: west config <section>.<key>\n'

def test_unset_config():
    # Getting unset configuration options should raise an error.
    # With verbose output, the exact missing option should be printed.
    with pytest.raises(subprocess.CalledProcessError) as e:
        cmd('-v config pytest.missing')
        assert 'pytest.missing is unset' in str(e)

def test_no_args():
    with pytest.raises(subprocess.CalledProcessError) as e:
        cmd('config')
        assert 'missing argument name' in str(e)

def test_list():
    def sorted_list(other_args=''):
        return list(sorted(cmd('config -l ' + other_args).splitlines()))

    with pytest.raises(subprocess.CalledProcessError) as e:
        cmd('config -l pytest.foo')
        assert '-l cannot be combined with name argument' in str(e)

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
