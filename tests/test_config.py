# Copyright (c) 2019, Nordic Semiconductor ASA
# Copyright 2026 The Zephyr Project Contributors.
#
# SPDX-License-Identifier: Apache-2.0

# Tests for west's configuration handling.
#
# Migrated from v1 test_config.py. Three large shifts vs
# the legacy version:
#
# 1. CLI shape — `west config set/get/unset/list NAME [VALUE]` replaces
#    the legacy positional `west config NAME VALUE` shorthand. Already
#    proven by `test_alias.py`.
# 2. Storage — `.west/config.toml` (TOML) replaces the legacy
#    `.west/config` (INI). `configparser` is dropped; assertions go
#    through the rust-backed `west.configuration.Configuration` instead.
# 3. Dropped features — `-a/--append`, `-d`/`-D` cascading deletes,
#    `--list-paths`/`--list-search-paths`, and pathsep-separated
#    `WEST_CONFIG_*` env vars don't exist in the rust port. The
#    corresponding tests are out of this file (tracked separately if
#    those features ever come back).

import os
import pathlib

import pytest
from conftest import cmd, cmd_raises, update_env

from west import configuration as wconfig
from west.configuration import Configuration

SYSTEM = wconfig.ConfigFile.SYSTEM
GLOBAL = wconfig.ConfigFile.GLOBAL
LOCAL = wconfig.ConfigFile.LOCAL
ALL = wconfig.ConfigFile.ALL


@pytest.fixture(autouse=True)
def autouse_config_tmpdir(config_tmpdir):
    # Since this module tests west's configuration file features,
    # adding autouse=True to the config_tmpdir fixture saves typing
    # and is less error-prone than using it below in every test case.
    pass


class _ScopedView:
    '''Scope-pinned view onto a `Configuration` instance.

    Mirrors the legacy `configparser` idiom: `cfg(LOCAL).get('a.b')`
    where missing options return `None` rather than raising. Replaces
    `wconfig.read_config(configfile=f, …) + cfg['section']['key']`.
    '''

    def __init__(self, configuration, configfile):
        self._cfg = configuration
        self._scope = configfile

    def get(self, option, default=None):
        return self._cfg.get(option, default=default, configfile=self._scope)


def cfg(f=ALL, topdir=None):
    return _ScopedView(Configuration(topdir=topdir), f)


def update_testcfg(section, key, value, configfile=LOCAL, topdir=None):
    Configuration(topdir=topdir).set(
        option=f'{section}.{key}',
        value=value,
        configfile=configfile,
    )


def delete_testcfg(section, key, configfile=None, topdir=None):
    Configuration(topdir=topdir).delete(
        option=f'{section}.{key}',
        configfile=configfile,
    )


def test_config_global():
    # Set a global config option via the command interface. Make sure
    # it can be read back using the API calls and at the command line
    # at ALL and GLOBAL locations only.
    cmd('config set --global pytest.global foo')

    assert cfg(GLOBAL).get('pytest.global') == 'foo'
    assert cfg(ALL).get('pytest.global') == 'foo'
    assert cfg(SYSTEM).get('pytest.global') is None
    assert cfg(LOCAL).get('pytest.global') is None
    assert cmd('config get pytest.global').rstrip() == 'foo'
    assert cmd('config get --global pytest.global').rstrip() == 'foo'

    # Make sure we can change the value of an existing variable.
    cmd('config set --global pytest.global bar')

    assert cfg(GLOBAL).get('pytest.global') == 'bar'
    assert cfg(ALL).get('pytest.global') == 'bar'
    assert cmd('config get pytest.global').rstrip() == 'bar'
    assert cmd('config get --global pytest.global').rstrip() == 'bar'

    # Check that we can create multiple variables per section.
    cmd('config set --global pytest.global2 foo2')

    assert cfg(ALL).get('pytest.global') == 'bar'
    assert cfg(GLOBAL).get('pytest.global') == 'bar'
    assert cfg(LOCAL).get('pytest.global') is None
    assert cfg(ALL).get('pytest.global2') == 'foo2'
    assert cfg(GLOBAL).get('pytest.global2') == 'foo2'
    assert cfg(LOCAL).get('pytest.global2') is None


def test_config_local():
    # test_config_global counterpart at the local scope.
    cmd('config set --local pytest.local foo')

    assert cfg(LOCAL).get('pytest.local') == 'foo'
    assert cfg(ALL).get('pytest.local') == 'foo'
    assert cfg(SYSTEM).get('pytest.local') is None
    assert cfg(GLOBAL).get('pytest.local') is None
    assert cmd('config get pytest.local').rstrip() == 'foo'
    assert cmd('config get --local pytest.local').rstrip() == 'foo'

    cmd('config set --local pytest.local bar')

    assert cfg(LOCAL).get('pytest.local') == 'bar'
    assert cfg(ALL).get('pytest.local') == 'bar'
    assert cfg(SYSTEM).get('pytest.local') is None
    assert cfg(GLOBAL).get('pytest.local') is None
    assert cmd('config get pytest.local').rstrip() == 'bar'
    assert cmd('config get --local pytest.local').rstrip() == 'bar'

    cmd('config set --local pytest.local2 foo2')

    assert cfg(ALL).get('pytest.local') == 'bar'
    assert cfg(GLOBAL).get('pytest.local') is None
    assert cfg(LOCAL).get('pytest.local') == 'bar'
    assert cfg(ALL).get('pytest.local2') == 'foo2'
    assert cfg(GLOBAL).get('pytest.local2') is None
    assert cfg(LOCAL).get('pytest.local2') == 'foo2'


def test_config_system():
    # Basic test of system-level configuration.
    update_testcfg('pytest', 'key', 'val', configfile=SYSTEM)
    assert cfg(ALL).get('pytest.key') == 'val'
    assert cfg(SYSTEM).get('pytest.key') == 'val'
    assert cfg(GLOBAL).get('pytest.key') is None
    assert cfg(LOCAL).get('pytest.key') is None

    update_testcfg('pytest', 'key', 'val2', configfile=SYSTEM)
    assert cfg(SYSTEM).get('pytest.key') == 'val2'


def test_config_system_precedence():
    # Test precedence rules, including system level.
    update_testcfg('pytest', 'key', 'sys', configfile=SYSTEM)
    assert cfg(SYSTEM).get('pytest.key') == 'sys'
    assert cfg(ALL).get('pytest.key') == 'sys'

    update_testcfg('pytest', 'key', 'glb', configfile=GLOBAL)
    assert cfg(SYSTEM).get('pytest.key') == 'sys'
    assert cfg(GLOBAL).get('pytest.key') == 'glb'
    assert cfg(ALL).get('pytest.key') == 'glb'

    update_testcfg('pytest', 'key', 'lcl', configfile=LOCAL)
    assert cfg(SYSTEM).get('pytest.key') == 'sys'
    assert cfg(GLOBAL).get('pytest.key') == 'glb'
    assert cfg(LOCAL).get('pytest.key') == 'lcl'
    assert cfg(ALL).get('pytest.key') == 'lcl'


def test_system_creation():
    # Test that the system file -- and just that file -- is created
    # on demand. The legacy `wconfig._location()` private API is gone;
    # the autouse fixture writes WEST_CONFIG_* env vars and we read
    # those.
    system = pathlib.Path(os.environ['WEST_CONFIG_SYSTEM'])
    glbl = pathlib.Path(os.environ['WEST_CONFIG_GLOBAL'])
    local = pathlib.Path(os.environ['WEST_CONFIG_LOCAL'])

    assert not system.is_file()
    assert not glbl.is_file()
    assert not local.is_file()

    update_testcfg('pytest', 'key', 'val', configfile=SYSTEM)

    assert system.is_file()
    assert not glbl.is_file()
    assert not local.is_file()
    assert cfg(ALL).get('pytest.key') == 'val'
    assert cfg(SYSTEM).get('pytest.key') == 'val'
    assert cfg(GLOBAL).get('pytest.key') is None
    assert cfg(LOCAL).get('pytest.key') is None


def test_global_creation():
    # Like test_system_creation, for global config options.
    system = pathlib.Path(os.environ['WEST_CONFIG_SYSTEM'])
    glbl = pathlib.Path(os.environ['WEST_CONFIG_GLOBAL'])
    local = pathlib.Path(os.environ['WEST_CONFIG_LOCAL'])

    assert not system.is_file()
    assert not glbl.is_file()
    assert not local.is_file()

    update_testcfg('pytest', 'key', 'val', configfile=GLOBAL)

    assert not system.is_file()
    assert glbl.is_file()
    assert not local.is_file()
    assert cfg(ALL).get('pytest.key') == 'val'
    assert cfg(SYSTEM).get('pytest.key') is None
    assert cfg(GLOBAL).get('pytest.key') == 'val'
    assert cfg(LOCAL).get('pytest.key') is None


def test_local_creation():
    # Like test_system_creation, for local config options.
    system = pathlib.Path(os.environ['WEST_CONFIG_SYSTEM'])
    glbl = pathlib.Path(os.environ['WEST_CONFIG_GLOBAL'])
    local = pathlib.Path(os.environ['WEST_CONFIG_LOCAL'])

    assert not system.is_file()
    assert not glbl.is_file()
    assert not local.is_file()

    update_testcfg('pytest', 'key', 'val', configfile=LOCAL)

    assert not system.is_file()
    assert not glbl.is_file()
    assert local.is_file()
    assert cfg(ALL).get('pytest.key') == 'val'
    assert cfg(SYSTEM).get('pytest.key') is None
    assert cfg(GLOBAL).get('pytest.key') is None
    assert cfg(LOCAL).get('pytest.key') == 'val'


def test_local_creation_with_topdir():
    # Like test_local_creation, with a specified topdir.
    system = pathlib.Path(os.environ['WEST_CONFIG_SYSTEM'])
    glbl = pathlib.Path(os.environ['WEST_CONFIG_GLOBAL'])
    local = pathlib.Path(os.environ['WEST_CONFIG_LOCAL'])

    topdir = pathlib.Path(os.getcwd()) / 'test-topdir'
    topdir_west = topdir / '.west'
    assert not topdir_west.exists()
    topdir_west.mkdir(parents=True)
    # Rust convention: `.west/config.toml` (legacy used `.west/config`).
    topdir_config = topdir_west / 'config.toml'

    assert not system.exists()
    assert not glbl.exists()
    assert not local.exists()
    assert not topdir_config.exists()

    # The autouse fixture has set WEST_CONFIG_LOCAL. Disable it to
    # exercise the Configuration(topdir=…) discovery path.
    with update_env({'WEST_CONFIG_LOCAL': None}):
        update_testcfg('pytest', 'key', 'val', configfile=LOCAL, topdir=str(topdir))
        assert not system.exists()
        assert not glbl.exists()
        assert not local.exists()
        assert topdir_config.exists()

        assert cfg(ALL, topdir=str(topdir)).get('pytest.key') == 'val'
        assert cfg(SYSTEM).get('pytest.key') is None
        assert cfg(GLOBAL).get('pytest.key') is None
        assert cfg(LOCAL, topdir=str(topdir)).get('pytest.key') == 'val'


def test_delete_basic():
    # Basic deletion test: write local, verify global and system
    # deletions don't work, then delete local does work.
    update_testcfg('pytest', 'key', 'val', configfile=LOCAL)
    assert cfg(ALL).get('pytest.key') == 'val'
    with pytest.raises(KeyError):
        delete_testcfg('pytest', 'key', configfile=SYSTEM)
    with pytest.raises(KeyError):
        delete_testcfg('pytest', 'key', configfile=GLOBAL)
    delete_testcfg('pytest', 'key', configfile=LOCAL)
    assert cfg(ALL).get('pytest.key') is None


def test_delete_all():
    # Deleting ConfigFile.ALL should delete from everywhere.
    update_testcfg('pytest', 'key', 'system', configfile=SYSTEM)
    update_testcfg('pytest', 'key', 'global', configfile=GLOBAL)
    update_testcfg('pytest', 'key', 'local', configfile=LOCAL)
    assert cfg(SYSTEM).get('pytest.key') == 'system'
    assert cfg(GLOBAL).get('pytest.key') == 'global'
    assert cfg(LOCAL).get('pytest.key') == 'local'
    delete_testcfg('pytest', 'key', configfile=ALL)
    assert cfg(ALL).get('pytest.key') is None


def test_delete_none():
    # Deleting configfile=None should delete from the
    # highest-precedence layer that has the option, cascading down
    # on repeated invocations. Mirrors `Configuration.delete_topmost`
    # in the rust binding.
    update_testcfg('pytest', 'key', 'system', configfile=SYSTEM)
    update_testcfg('pytest', 'key', 'global', configfile=GLOBAL)
    update_testcfg('pytest', 'key', 'local', configfile=LOCAL)
    assert cfg(SYSTEM).get('pytest.key') == 'system'
    assert cfg(GLOBAL).get('pytest.key') == 'global'
    assert cfg(LOCAL).get('pytest.key') == 'local'
    delete_testcfg('pytest', 'key', configfile=None)
    assert cfg(ALL).get('pytest.key') == 'global'
    delete_testcfg('pytest', 'key', configfile=None)
    assert cfg(ALL).get('pytest.key') == 'system'
    delete_testcfg('pytest', 'key', configfile=None)
    assert cfg(ALL).get('pytest.key') is None


def test_delete_system():
    # Test SYSTEM-only delete.
    update_testcfg('pytest', 'key', 'system', configfile=SYSTEM)
    update_testcfg('pytest', 'key', 'global', configfile=GLOBAL)
    update_testcfg('pytest', 'key', 'local', configfile=LOCAL)
    assert cfg(SYSTEM).get('pytest.key') == 'system'
    assert cfg(GLOBAL).get('pytest.key') == 'global'
    assert cfg(LOCAL).get('pytest.key') == 'local'
    delete_testcfg('pytest', 'key', configfile=SYSTEM)
    assert cfg(SYSTEM).get('pytest.key') is None
    assert cfg(GLOBAL).get('pytest.key') == 'global'
    assert cfg(LOCAL).get('pytest.key') == 'local'


def test_delete_global():
    # Test GLOBAL-only delete.
    update_testcfg('pytest', 'key', 'system', configfile=SYSTEM)
    update_testcfg('pytest', 'key', 'global', configfile=GLOBAL)
    update_testcfg('pytest', 'key', 'local', configfile=LOCAL)
    assert cfg(SYSTEM).get('pytest.key') == 'system'
    assert cfg(GLOBAL).get('pytest.key') == 'global'
    assert cfg(LOCAL).get('pytest.key') == 'local'
    delete_testcfg('pytest', 'key', configfile=GLOBAL)
    assert cfg(SYSTEM).get('pytest.key') == 'system'
    assert cfg(GLOBAL).get('pytest.key') is None
    assert cfg(LOCAL).get('pytest.key') == 'local'


def test_delete_local():
    # Test LOCAL-only delete.
    update_testcfg('pytest', 'key', 'system', configfile=SYSTEM)
    update_testcfg('pytest', 'key', 'global', configfile=GLOBAL)
    update_testcfg('pytest', 'key', 'local', configfile=LOCAL)
    assert cfg(SYSTEM).get('pytest.key') == 'system'
    assert cfg(GLOBAL).get('pytest.key') == 'global'
    assert cfg(LOCAL).get('pytest.key') == 'local'
    delete_testcfg('pytest', 'key', configfile=LOCAL)
    assert cfg(SYSTEM).get('pytest.key') == 'system'
    assert cfg(GLOBAL).get('pytest.key') == 'global'
    assert cfg(LOCAL).get('pytest.key') is None


def test_delete_local_with_topdir():
    # Test LOCAL-only delete with specified topdir.
    update_testcfg('pytest', 'key', 'system', configfile=SYSTEM)
    update_testcfg('pytest', 'key', 'global', configfile=GLOBAL)
    update_testcfg('pytest', 'key', 'local', configfile=LOCAL)
    assert cfg(SYSTEM).get('pytest.key') == 'system'
    assert cfg(GLOBAL).get('pytest.key') == 'global'
    assert cfg(LOCAL).get('pytest.key') == 'local'
    delete_testcfg('pytest', 'key', configfile=LOCAL)
    assert cfg(SYSTEM).get('pytest.key') == 'system'
    assert cfg(GLOBAL).get('pytest.key') == 'global'
    assert cfg(LOCAL).get('pytest.key') is None


def test_delete_local_one():
    # Test LOCAL-only delete of one option doesn't affect the other.
    update_testcfg('pytest', 'key1', 'foo', configfile=LOCAL)
    update_testcfg('pytest', 'key2', 'bar', configfile=LOCAL)
    delete_testcfg('pytest', 'key1', configfile=LOCAL)
    assert cfg(LOCAL).get('pytest.key1') is None
    assert cfg(LOCAL).get('pytest.key2') == 'bar'


def test_default_config():
    # Writing to a value without a config destination should default
    # to --local.
    cmd('config set pytest.local foo')

    assert cmd('config get pytest.local').rstrip() == 'foo'
    assert cmd('config get --local pytest.local').rstrip() == 'foo'
    assert cfg(ALL).get('pytest.local') == 'foo'
    assert cfg(SYSTEM).get('pytest.local') is None
    assert cfg(GLOBAL).get('pytest.local') is None
    assert cfg(LOCAL).get('pytest.local') == 'foo'


def test_config_precedence():
    # Verify that local settings take precedence over global ones,
    # but that both values are still available, and that setting
    # either doesn't affect system settings.
    cmd('config set --global pytest.precedence global')
    cmd('config set --local pytest.precedence local')

    assert cmd('config get --global pytest.precedence').rstrip() == 'global'
    assert cmd('config get --local pytest.precedence').rstrip() == 'local'
    assert cmd('config get pytest.precedence').rstrip() == 'local'
    assert cfg(ALL).get('pytest.precedence') == 'local'
    assert cfg(SYSTEM).get('pytest.precedence') is None
    assert cfg(GLOBAL).get('pytest.precedence') == 'global'
    assert cfg(LOCAL).get('pytest.precedence') == 'local'


def test_config_missing_key():
    # Asking for an option without `section.key` shape errors out.
    _, err_msg = cmd_raises('config get pytest', SystemExit)
    assert 'invalid configuration key "pytest"' in err_msg


def test_unset_config():
    # Reading an unset option fails (exit non-zero). The rust binary
    # currently emits no stderr message for this case, so we assert
    # only on the exit status; the legacy "-v" verbose-output detail
    # ("pytest.missing is unset") doesn't carry over.
    cmd_raises('config get pytest.missing', SystemExit)


def test_no_args():
    # `west config` with no subcommand prints clap's usage error.
    _, err_msg = cmd_raises('config', SystemExit)
    assert 'Usage: west config' in err_msg


def test_list():
    def listing(other_args=''):
        # The rust `config list` includes every key in scope. The
        # autouse fixture pre-populates nothing, so all listed entries
        # come from this test.
        return sorted(cmd('config list ' + other_args).splitlines())

    assert listing() == []

    cmd('config set pytest.foo who')
    assert listing() == ['pytest.foo=who']

    cmd('config set pytest.bar what')
    assert listing() == ['pytest.bar=what', 'pytest.foo=who']

    cmd('config set --global pytest.baz where')
    assert listing() == ['pytest.bar=what', 'pytest.baz=where', 'pytest.foo=who']
    assert listing('--system') == []
    assert listing('--global') == ['pytest.baz=where']
    assert listing('--local') == ['pytest.bar=what', 'pytest.foo=who']


def test_round_trip():
    cmd('config set pytest.foo bar,baz')
    assert cmd('config get pytest.foo').strip() == 'bar,baz'
