# Copyright (c) 2019, Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

import configparser
import os
import subprocess
from unittest.mock import patch

import pytest

from west import configuration as config
from west.util import canon_path

from conftest import cmd

SYSTEM = config.ConfigFile.SYSTEM
GLOBAL = config.ConfigFile.GLOBAL
LOCAL = config.ConfigFile.LOCAL
ALL = config.ConfigFile.ALL

def cfg(f=ALL):
    # Load a fresh configuration object at the given level, and return it.
    cp = configparser.ConfigParser(allow_no_value=True)
    config.read_config(config_file=f, config=cp)
    return cp

def tstloc(cfg):
    # Monkeypatch for the config file location. assumes we are called
    # in a tmpdir.
    #
    # Important: If you call cmd('config ...'), the subprocess isn't affected,
    #            so the location is determined by config_tmpdir().
    if cfg == SYSTEM:
        return os.path.join(os.getcwd(), 'system', 'config')
    elif cfg == GLOBAL:
        return os.path.join(os.getcwd(), 'global', 'config')
    elif cfg == LOCAL:
        return os.path.join(os.getcwd(), 'local', 'config')
    else:
        raise ValueError('cfg: {}'.format(cfg))

@pytest.fixture(autouse=True)
def config_tmpdir(tmpdir):
    # Fixture for running from a temporary directory with a .west
    # inside. We also:
    #
    # - ensure we're being run under tox, to avoid messing with
    #   the user's actual configuration files
    # - ensure configuration files point where they should inside
    #   the temporary tox directories, for the same reason
    # - ensure ~ exists (since the test environment
    #   doesn't let us run in the true user $HOME).
    # - set WEST_CONFIG_SYSTEM to lie inside the tmpdir (to avoid
    #   interacting with the real system file)
    # - set ZEPHYR_BASE (to avoid complaints in subcommand stderr)
    #
    # Using this makes the tests run faster than if we used
    # west_init_tmpdir from conftest.py, and also ensures that the
    # configuration code doesn't depend on features like the existence
    # of a manifest file, helping separate concerns.
    assert 'TOXTEMPDIR' in os.environ, 'you must run tests using tox'
    toxtmp = os.environ['TOXTEMPDIR']
    toxhome = canon_path(os.path.join(toxtmp, 'pytest-home'))
    global_loc = canon_path(config._location(GLOBAL))
    assert canon_path(os.path.expanduser('~')) == toxhome
    assert global_loc == os.path.join(toxhome, '.westconfig')
    os.makedirs(toxhome, exist_ok=True)
    if os.path.exists(global_loc):
        os.remove(global_loc)
    tmpdir.mkdir('.west')
    tmpdir.chdir()

    # Make sure the 'pytest' section is not present. If it is,
    # something is wrong in either the test environment (e.g. the user
    # has a system file with a 'pytest' section in it) or the tests
    # (if we're not setting ourselves up properly)
    os.environ['ZEPHYR_BASE'] = str(tmpdir.join('no-zephyr-here'))
    os.environ['WEST_CONFIG_SYSTEM'] = str(tmpdir.join('config.system'))
    if 'pytest' in cfg():
        del os.environ['ZEPHYR_BASE']
        assert False, 'bad fixture setup'
    yield tmpdir
    del os.environ['ZEPHYR_BASE']
    del os.environ['WEST_CONFIG_SYSTEM']

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

@patch('west.configuration._location', new=tstloc)
def test_config_system():
    # Basic test of system-level configuration.

    config.update_config('pytest', 'key', 'val', configfile=SYSTEM)
    assert cfg(f=ALL)['pytest']['key'] == 'val'
    assert cfg(f=SYSTEM)['pytest']['key'] == 'val'
    assert 'pytest' not in cfg(f=GLOBAL)
    assert 'pytest' not in cfg(f=LOCAL)

    config.update_config('pytest', 'key', 'val2', configfile=SYSTEM)
    assert cfg(f=SYSTEM)['pytest']['key'] == 'val2'

@patch('west.configuration._location', new=tstloc)
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

@patch('west.configuration._location', new=tstloc)
def test_system_creation():
    # Test that the system file -- and just that file -- is created on
    # demand.

    assert not os.path.isfile(tstloc(SYSTEM))
    assert not os.path.isfile(tstloc(GLOBAL))
    assert not os.path.isfile(tstloc(LOCAL))

    config.update_config('pytest', 'key', 'val', configfile=SYSTEM)

    assert os.path.isfile(tstloc(SYSTEM))
    assert not os.path.isfile(tstloc(GLOBAL))
    assert not os.path.isfile(tstloc(LOCAL))
    assert cfg(f=ALL)['pytest']['key'] == 'val'
    assert cfg(f=SYSTEM)['pytest']['key'] == 'val'
    assert 'pytest' not in cfg(f=GLOBAL)
    assert 'pytest' not in cfg(f=LOCAL)

@patch('west.configuration._location', new=tstloc)
def test_global_creation():
    # Like test_system_creation, for global config options.

    assert not os.path.isfile(tstloc(SYSTEM))
    assert not os.path.isfile(tstloc(GLOBAL))
    assert not os.path.isfile(tstloc(LOCAL))

    config.update_config('pytest', 'key', 'val', configfile=GLOBAL)

    assert not os.path.isfile(tstloc(SYSTEM))
    assert os.path.isfile(tstloc(GLOBAL))
    assert not os.path.isfile(tstloc(LOCAL))
    assert cfg(f=ALL)['pytest']['key'] == 'val'
    assert 'pytest' not in cfg(f=SYSTEM)
    assert cfg(f=GLOBAL)['pytest']['key'] == 'val'
    assert 'pytest' not in cfg(f=LOCAL)

@patch('west.configuration._location', new=tstloc)
def test_local_creation():
    # Like test_system_creation, for local config options.

    assert not os.path.isfile(tstloc(SYSTEM))
    assert not os.path.isfile(tstloc(GLOBAL))
    assert not os.path.isfile(tstloc(LOCAL))

    config.update_config('pytest', 'key', 'val', configfile=LOCAL)

    assert not os.path.isfile(tstloc(SYSTEM))
    assert not os.path.isfile(tstloc(GLOBAL))
    assert os.path.isfile(tstloc(LOCAL))
    assert cfg(f=ALL)['pytest']['key'] == 'val'
    assert 'pytest' not in cfg(f=SYSTEM)
    assert 'pytest' not in cfg(f=GLOBAL)
    assert cfg(f=LOCAL)['pytest']['key'] == 'val'

@patch('west.configuration._location', new=tstloc)
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

@patch('west.configuration._location', new=tstloc)
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

@patch('west.configuration._location', new=tstloc)
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

@patch('west.configuration._location', new=tstloc)
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

@patch('west.configuration._location', new=tstloc)
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

@patch('west.configuration._location', new=tstloc)
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

@patch('west.configuration._location', new=tstloc)
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

@patch('west.configuration._location', new=tstloc)
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

def test_env_overrides():
    # Test that the WEST_CONFIG_SYSTEM etc. overrides work as
    # expected.
    #
    # We are *not* using tstloc() and want to call cmd(), but
    # we don't want to set the global or local environment variables
    # in os.environ, or we'd have to be careful to clean them up,
    # since they would affect other test cases. Use a copy instead.
    # (Our autouse fixture cleans up the system variable.)

    # Our test fixture already set up a system variable; test it.
    assert not os.path.isfile(os.environ['WEST_CONFIG_SYSTEM'])
    cmd('config --system pytest.foo bar')
    assert os.path.isfile(os.environ['WEST_CONFIG_SYSTEM'])
    assert cfg(f=SYSTEM)['pytest']['foo'] == 'bar'

    # Copy the environment to make sure global and local settings
    # take effect there, and only there.
    env = os.environ.copy()
    env['WEST_CONFIG_GLOBAL'] = os.path.abspath('config.global')
    env['WEST_CONFIG_LOCAL'] = os.path.abspath('config.local')

    config.update_config('pytest', 'foo', 'global-not-in-env',
                         configfile=GLOBAL)
    assert not os.path.isfile(env['WEST_CONFIG_GLOBAL'])
    assert cfg(f=ALL)['pytest']['foo'] == 'global-not-in-env'

    cmd('config --global pytest.foo global-in-env', env=env)
    assert os.path.isfile(env['WEST_CONFIG_GLOBAL'])
    assert cmd('config pytest.foo', env=env).rstrip() == 'global-in-env'

    config.update_config('pytest', 'foo', 'local-not-in-env',
                         configfile=LOCAL)
    assert not os.path.isfile(env['WEST_CONFIG_LOCAL'])
    assert cfg(f=ALL)['pytest']['foo'] == 'local-not-in-env'
    cmd('config --local pytest.foo local-in-env', env=env)
    assert os.path.isfile(env['WEST_CONFIG_LOCAL'])
    assert cmd('config pytest.foo', env=env).rstrip() == 'local-in-env'
