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
    # Important: This is only safe to use to test the API in this process.
    #            DON'T call cmd('config ...'); subprocesses aren't patched,
    #            so the system location is the "real" one.
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
    if 'pytest' in cfg():
        del os.environ['ZEPHYR_BASE']
        assert False, 'bad fixture setup'
    yield tmpdir
    del os.environ['ZEPHYR_BASE']

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
def test_system_creation():
    # Test that the system file -- and just that file -- is created on
    # demand.
    #
    # Since we use tstloc(), we can't call cmd('config ...') here.
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
    #
    # Since we use tstloc(), we can't call cmd('config ...') here.
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
    #
    # Since we use tstloc(), we can't call cmd('config ...') here.
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
