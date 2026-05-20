# Copyright (c) 2019, Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

import configparser
import os
import pathlib
import textwrap
from typing import Any

import pytest
from conftest import WINDOWS, chdir, cmd, cmd_raises, tmp_west_topdir, update_env

from west import configuration as wconfig
from west.configuration import MalformedConfig
from west.util import PathType, WestNotFound

SYSTEM = wconfig.ConfigFile.SYSTEM
GLOBAL = wconfig.ConfigFile.GLOBAL
LOCAL = wconfig.ConfigFile.LOCAL
ALL = wconfig.ConfigFile.ALL

west_env = {
    SYSTEM: 'WEST_CONFIG_SYSTEM',
    GLOBAL: 'WEST_CONFIG_GLOBAL',
    LOCAL: 'WEST_CONFIG_LOCAL',
}

west_flag = {
    SYSTEM: '--system',
    GLOBAL: '--global',
    LOCAL: '--local',
}


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
        wconfig.read_config(configfile=f, config=cp, topdir=topdir)
    return cp


def update_testcfg(
    section: str,
    key: str,
    value: Any,
    configfile: wconfig.ConfigFile = LOCAL,
    topdir: PathType | None = None,
) -> None:
    c = wconfig.Configuration(topdir)
    c.set(option=f'{section}.{key}', value=value, configfile=configfile)


def delete_testcfg(
    section: str,
    key: str,
    configfile: wconfig.ConfigFile | None = None,
    topdir: PathType | None = None,
) -> None:
    c = wconfig.Configuration(topdir)
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

    # Check that we can create multiple variables per section.
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


@pytest.mark.parametrize("location", [LOCAL, GLOBAL, SYSTEM])
def test_config_list_paths_env(location):
    '''Test that --list-paths considers the env variables'''
    flag = west_flag[location]
    env_var = west_env[location]

    # create the config
    cmd(f'config {flag} pytest.key val')

    # check that the config is listed now
    stdout = cmd(f'config {flag} --list-paths')
    config_path = pathlib.Path(os.environ[env_var])
    assert f'{config_path}' == stdout.rstrip()

    # config is only listed if it exists
    config_path.unlink()
    stdout = cmd(f'config {flag} --list-paths')
    assert '' == stdout.rstrip()


def test_config_list_paths():
    _, err_msg = cmd_raises('config --list-paths pytest.foo', SystemExit)
    assert '--list-paths cannot be combined with name argument' in err_msg

    WEST_CONFIG_LOCAL = os.environ['WEST_CONFIG_LOCAL']
    WEST_CONFIG_GLOBAL = os.environ['WEST_CONFIG_GLOBAL']
    WEST_CONFIG_SYSTEM = os.environ['WEST_CONFIG_SYSTEM']

    # Fixture is pristine?
    stdout = cmd('config --list-paths')
    assert stdout.splitlines() == []

    # create the configs
    cmd('config --local pytest.key val')
    cmd('config --global pytest.key val')
    cmd('config --system pytest.key val')

    # list the configs
    stdout = cmd('config --list-paths')
    assert (
        stdout.splitlines()
        == textwrap.dedent(f'''\
        {WEST_CONFIG_SYSTEM}
        {WEST_CONFIG_GLOBAL}
        {WEST_CONFIG_LOCAL}
        ''').splitlines()
    )

    # do not list any configs if no config files currently exist
    # (Note: even no local config exists, same as outside any west workspace)
    pathlib.Path(WEST_CONFIG_SYSTEM).unlink()
    pathlib.Path(WEST_CONFIG_GLOBAL).unlink()
    pathlib.Path(WEST_CONFIG_LOCAL).unlink()
    stdout = cmd('config --list-paths')
    assert stdout.splitlines() == []

    # list local config as it exists (default value, not overriden by fixture env)
    del os.environ['WEST_CONFIG_LOCAL']
    default_config = pathlib.Path('.west') / 'config'
    defconfig_abs_str = str(default_config.absolute())
    # Cheap fake workspace, enough to fool west for this test
    default_config.parent.mkdir()
    default_config.write_text(
        textwrap.dedent('''\
        [manifest]
        path = any
        file = west.yml
    ''')
    )
    stdout = cmd('config --list-paths')
    assert stdout.splitlines() == [defconfig_abs_str]

    # Now point a relative WEST_CONFIG_x to the same file and check it
    # is anchored to the west topdir (referring to the same file twice
    # could break other west features but must not break the low-level
    # --list-paths)
    assert not default_config.is_absolute()
    os.mkdir('sub0')
    for e in ['WEST_CONFIG_GLOBAL', 'WEST_CONFIG_SYSTEM']:
        with update_env({e: str(default_config)}):
            for d in ['.', 'sub0', '.west']:
                with chdir(d):
                    stdout = cmd('config --list-paths')
                    assert stdout.splitlines() == [defconfig_abs_str, defconfig_abs_str]

            # Leave the fake west workspace
            with chdir('..'):
                with pytest.raises(WestNotFound) as WNE:
                    stdout = cmd('config --list-paths')
                assert 'topdir' in str(WNE)


def test_config_list_search_paths_all():
    _, err_msg = cmd_raises('config --list-search-paths pytest.foo', SystemExit)
    assert '--list-search-paths cannot be combined with name argument' in err_msg

    WEST_CONFIG_SYSTEM = os.getenv('WEST_CONFIG_SYSTEM')
    WEST_CONFIG_GLOBAL = os.getenv('WEST_CONFIG_GLOBAL')
    WEST_CONFIG_LOCAL = os.getenv('WEST_CONFIG_LOCAL')

    # one config file set via env variables
    stdout = cmd('config --list-search-paths')
    assert stdout.splitlines() == [WEST_CONFIG_SYSTEM, WEST_CONFIG_GLOBAL, WEST_CONFIG_LOCAL]

    # multiple config files are set via env variables
    conf_dir = (pathlib.Path('some') / 'conf').resolve()
    config_s1 = conf_dir / "s1"
    config_s2 = conf_dir / "s2"
    config_g1 = conf_dir / "g1"
    config_g2 = conf_dir / "g2"
    config_l1 = conf_dir / "l1"
    config_l2 = conf_dir / "l2"
    env = {
        'WEST_CONFIG_SYSTEM': f'{config_s1}{os.pathsep}{config_s2}',
        'WEST_CONFIG_GLOBAL': f'{config_g1}{os.pathsep}{config_g2}',
        'WEST_CONFIG_LOCAL': f'{config_l1}{os.pathsep}{config_l2}',
    }
    with update_env(env):
        stdout = cmd('config --list-search-paths')
        search_paths = stdout.splitlines()
        assert search_paths == [
            str(config_s1),
            str(config_s2),
            str(config_g1),
            str(config_g2),
            str(config_l1),
            str(config_l2),
        ]

    # unset all west config env variables
    env = {
        'WEST_CONFIG_SYSTEM': None,
        'WEST_CONFIG_GLOBAL': None,
        'WEST_CONFIG_LOCAL': None,
    }
    with update_env(env):
        # outside west topdir: show system and global config search paths
        stdout = cmd('config --list-search-paths')
        search_paths = stdout.splitlines()
        assert len(search_paths) == 2

        # inside west topdir: show system, global and local config search paths
        west_topdir = pathlib.Path('.')
        with tmp_west_topdir(west_topdir):
            stdout = cmd('config --list-search-paths')
            search_paths = stdout.splitlines()
            assert len(search_paths) == 3
            local_path = (west_topdir / '.west' / 'config').resolve()
            assert search_paths[2] == str(local_path)


@pytest.mark.parametrize("location", [LOCAL, GLOBAL, SYSTEM])
def test_config_list_search_paths(location):
    flag = '' if location == ALL else west_flag[location]
    env_var = west_env[location] if flag else None

    west_topdir = pathlib.Path('.')
    config1 = (west_topdir / 'some' / 'config 1').resolve()
    config2 = pathlib.Path('relative') / 'c 2'
    config2_abs = config2.resolve()
    with tmp_west_topdir(west_topdir):
        env = {env_var: f'{config1}{os.pathsep}{config2}'}
        # env variable contains two config files
        with update_env(env):
            stdout = cmd(f'config {flag} --list-search-paths')
            assert stdout.splitlines() == [str(config1), str(config2_abs)]
        # if no env var is set it should list one default search path
        with update_env({env_var: None}):
            stdout = cmd(f'config {flag} --list-search-paths')
            assert len(stdout.splitlines()) == 1


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

    assert not os.path.isfile(wconfig._location(SYSTEM)[0])
    assert not os.path.isfile(wconfig._location(GLOBAL)[0])
    assert not os.path.isfile(wconfig._location(LOCAL)[0])

    update_testcfg('pytest', 'key', 'val', configfile=SYSTEM)

    assert os.path.isfile(wconfig._location(SYSTEM)[0])
    assert not os.path.isfile(wconfig._location(GLOBAL)[0])
    assert not os.path.isfile(wconfig._location(LOCAL)[0])
    assert cfg(f=ALL)['pytest']['key'] == 'val'
    assert cfg(f=SYSTEM)['pytest']['key'] == 'val'
    assert 'pytest' not in cfg(f=GLOBAL)
    assert 'pytest' not in cfg(f=LOCAL)


def test_global_creation():
    # Like test_system_creation, for global config options.

    assert not os.path.isfile(wconfig._location(SYSTEM)[0])
    assert not os.path.isfile(wconfig._location(GLOBAL)[0])
    assert not os.path.isfile(wconfig._location(LOCAL)[0])

    update_testcfg('pytest', 'key', 'val', configfile=GLOBAL)

    assert not os.path.isfile(wconfig._location(SYSTEM)[0])
    assert os.path.isfile(wconfig._location(GLOBAL)[0])
    assert not os.path.isfile(wconfig._location(LOCAL)[0])
    assert cfg(f=ALL)['pytest']['key'] == 'val'
    assert 'pytest' not in cfg(f=SYSTEM)
    assert cfg(f=GLOBAL)['pytest']['key'] == 'val'
    assert 'pytest' not in cfg(f=LOCAL)


def test_local_creation():
    # Like test_system_creation, for local config options.

    assert not os.path.isfile(wconfig._location(SYSTEM)[0])
    assert not os.path.isfile(wconfig._location(GLOBAL)[0])
    assert not os.path.isfile(wconfig._location(LOCAL)[0])

    update_testcfg('pytest', 'key', 'val', configfile=LOCAL)

    assert not os.path.isfile(wconfig._location(SYSTEM)[0])
    assert not os.path.isfile(wconfig._location(GLOBAL)[0])
    assert os.path.isfile(wconfig._location(LOCAL)[0])
    assert cfg(f=ALL)['pytest']['key'] == 'val'
    assert 'pytest' not in cfg(f=SYSTEM)
    assert 'pytest' not in cfg(f=GLOBAL)
    assert cfg(f=LOCAL)['pytest']['key'] == 'val'


def test_local_creation_with_topdir():
    # Like test_local_creation, with a specified topdir.

    system = pathlib.Path(wconfig._location(SYSTEM)[0])
    glbl = pathlib.Path(wconfig._location(GLOBAL)[0])
    local = pathlib.Path(wconfig._location(LOCAL)[0])

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
    with update_env({'WEST_CONFIG_LOCAL': None}):
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
    cmd([
        'config',
        '--global',
        '-a',
        'build.cmake-args',
        '--',
        ' -DEXTRA_CFLAGS=\'-Wextra -g0\' -DFOO=BAR',
    ])

    assert (
        cfg(f=GLOBAL)['build']['cmake-args']
        == '-DCONF_FILE=foo.conf -DEXTRA_CFLAGS=\'-Wextra -g0\' -DFOO=BAR'
    )


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
        wconfig.delete_config('pytest', 'key', configfile=None)

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
        wconfig.delete_config('pytest', 'key', configfile=[GLOBAL, LOCAL])
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


@pytest.mark.skipif(WINDOWS, reason="chmod is limited on Windows")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root user can always read files",
)
def test_config_non_readable_file(config_tmpdir):
    # test to read a config file without read permission
    cwd = pathlib.Path.cwd()

    # create a readable file
    config_readable = cwd / 'readable'
    config_readable.touch()

    # create a non-readable file
    config_non_readable = cwd / 'non-readable'
    config_non_readable.touch()
    config_non_readable.chmod(0o000)

    # trying to use a non-readable config should result in according error
    with update_env({'WEST_CONFIG_GLOBAL': f'{config_readable}{os.pathsep}{config_non_readable}'}):
        _, stderr = cmd_raises('config --global some.section', MalformedConfig)
    expected = f"Error while reading one of '{[str(config_readable), str(config_non_readable)]}'"
    assert expected in stderr


def test_config_multiple(config_tmpdir):
    # Verify that local settings take precedence over global ones,
    # but that both values are still available, and that setting
    # either doesn't affect system settings.
    def write_config(config_file, section, key1, value1, key2, value2):
        config_file.parent.mkdir(exist_ok=True)

        content = textwrap.dedent(f'''
        [{section}]
        {key1} = {value1}
        {key2} = {value2}
        ''')

        with open(config_file, 'w') as conf:
            conf.write(content)

    # helper function to assert multiple config values
    def run_and_assert(expected_values: dict[str, dict[str, str]]):
        for scope, meta in expected_values.items():
            for flags, expected in meta.items():
                stdout = cmd(f'config --{scope} {flags}').rstrip()
                if type(expected) is list:
                    stdout = stdout.splitlines()
                assert stdout == expected, f"{scope} {flags}: {expected} =! {stdout}"

    # config file paths
    config_dir = pathlib.Path(config_tmpdir) / 'configs'
    config_s1 = config_dir / 'system 1'
    config_s2 = config_dir / 'system 2'
    config_g1 = config_dir / 'global 1'
    config_g2 = config_dir / 'global 2'
    config_l1 = config_dir / 'local 1'
    config_l2 = config_dir / 'local 2'

    # create some configs with
    # - some individual option per config file (s1/s2/g1/g2/l1/l2))
    # - the same option (s/g/l) defined in multiple configs
    write_config(config_s1, 'sec', 's', '1 !"$&/()=?', 's1', '1 !"$&/()=?')
    write_config(config_s2, 'sec', 's', '2', 's2', '2')
    write_config(config_g1, 'sec', 'g', '1', 'g1', '1')
    write_config(config_g2, 'sec', 'g', '2', 'g2', '2')
    write_config(config_l1, 'sec', 'l', '1', 'l1', '1')
    write_config(config_l2, 'sec', 'l', '2', 'l2', '2')

    # specify multiple configs for each config level (separated by os.pathsep)
    os.environ["WEST_CONFIG_GLOBAL"] = f'{config_g1}{os.pathsep}{config_g2}'
    os.environ["WEST_CONFIG_SYSTEM"] = f'{config_s1}{os.pathsep}{config_s2}'
    os.environ["WEST_CONFIG_LOCAL"] = f'{config_l1}{os.pathsep}{config_l2}'

    # check options from individual files and that options from latter configs override
    expected = {
        'system': {'sec.s1': '1 !"$&/()=?', 'sec.s2': '2', 'sec.s': '2'},
        'global': {'sec.g1': '1', 'sec.g2': '2', 'sec.g': '2'},
        'local': {'sec.l1': '1', 'sec.l2': '2', 'sec.l': '2'},
    }
    run_and_assert(expected)

    # check that list-paths gives correct output
    expected = {
        'system': {'--list-paths': [str(config_s1), str(config_s2)]},
        'global': {'--list-paths': [str(config_g1), str(config_g2)]},
        'local': {'--list-paths': [str(config_l1), str(config_l2)]},
    }
    run_and_assert(expected)

    # writing not possible if multiple configs are used
    _, stderr = cmd_raises('config --local sec.l3 3', ValueError)
    assert f'Cannot set value if multiple configs in use: {[config_l1, config_l2]}' in stderr


@pytest.mark.parametrize("location", [LOCAL, GLOBAL, SYSTEM])
def test_config_multiple_write(location):
    # write to a config with a single config file must work, even if other
    # locations have multiple configs in use
    flag = west_flag[location]
    env_var = west_env[location]

    configs_dir = pathlib.Path("configs")
    config1 = (configs_dir / 'config 1').resolve()
    config2 = (configs_dir / 'config 2').resolve()
    config3 = (configs_dir / 'config 3').resolve()

    env = {west_env[location]: f'{config1}'}
    other_locations = [c for c in [LOCAL, GLOBAL, SYSTEM] if c != location]
    for loc in other_locations:
        env[west_env[loc]] = f'{config2}{os.pathsep}{config3}'

    with update_env(env):
        cmd(f'config {flag} key.value {env_var}')
        stdout = cmd(f'config {flag} key.value')
        assert [env_var] == stdout.rstrip().splitlines()


@pytest.mark.parametrize("location", [LOCAL, GLOBAL, SYSTEM])
def test_config_multiple_relative(location):
    # specify multiple configs for each config level (separated by os.pathsep).
    # The paths may be relative relative paths, which are always anchored to
    # west topdir. For the test, the cwd is changed to another cwd to ensure
    # that relative paths are anchored correctly.
    flag = west_flag[location]
    env_var = west_env[location]

    msg = "'{file}' is relative but 'west topdir' is not defined"

    # create some configs
    configs_dir = pathlib.Path('config')
    configs_dir.mkdir()
    config1 = (configs_dir / 'config 1').resolve()
    config2 = (configs_dir / 'config 2').resolve()
    config1.touch()
    config2.touch()

    west_topdir = pathlib.Path.cwd()
    cwd = west_topdir / 'any' / 'other cwd'
    cwd.mkdir(parents=True)
    with chdir(cwd):
        config2_rel = config2.relative_to(west_topdir)
        command = f'config {flag} --list-paths'
        env_value = f'{config1}{os.pathsep}{config2_rel}'
        with update_env({env_var: env_value}):
            # cannot anchor relative path if no west topdir exists
            exc, _ = cmd_raises(command, WestNotFound)
            assert msg.format(file=config2_rel) in str(exc.value)

            # relative paths are anchored to west topdir
            with tmp_west_topdir(west_topdir):
                stdout = cmd(command)
                assert [str(config1), str(config2)] == stdout.rstrip().splitlines()

        # if a wrong separator is used, no config file must be found
        wrong_sep = ':' if WINDOWS else ';'
        env_value = f'{config1}{wrong_sep}{config2_rel}'
        with update_env({env_var: env_value}):
            # no path is listed
            stdout = cmd(command)
            assert not stdout


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
    assert sorted_list() == ['pytest.bar=what', 'pytest.foo=who']

    cmd('config --global pytest.baz where')
    assert sorted_list() == ['pytest.bar=what', 'pytest.baz=where', 'pytest.foo=who']
    assert sorted_list('--system') == []
    assert sorted_list('--global') == ['pytest.baz=where']
    assert sorted_list('--local') == ['pytest.bar=what', 'pytest.foo=who']


def test_round_trip():
    cmd('config pytest.foo bar,baz')
    assert cmd('config pytest.foo').strip() == 'bar,baz'
