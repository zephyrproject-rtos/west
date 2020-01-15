# Copyright (c) 2018, 2019, Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

'''West configuration file handling.

West follows Git-like conventions for configuration file locations.
There are three types of configuration file: system-wide files apply
to all users on the current machine, global files apply to the current
user, and local files apply to the current west workspace.

System files:

- Linux: ``/etc/westconfig``
- macOS: ``/usr/local/etc/westconfig``
- Windows: ``%PROGRAMDATA%\\west\\config``

Global files:

- Linux: ``~/.westconfig`` or (if ``$XDG_CONFIG_HOME`` is set)
  ``$XDG_CONFIG_HOME/west/config``
- macOS: ``~/.westconfig``
- Windows: ``.westconfig`` in the user's home directory, as determined
  by os.path.expanduser.

Local files:

- Linux, macOS, Windows: ``<workspace-topdir>/.west/config``

You can override these files' locations with the ``WEST_CONFIG_SYSTEM``,
``WEST_CONFIG_GLOBAL``, and ``WEST_CONFIG_LOCAL`` environment variables.

Configuration values from later configuration files override configuration
from earlier ones. Local values have highest precedence, and system values
lowest.
'''

import configparser
import os
import pathlib
import platform
from enum import Enum

import configobj

from west.util import west_dir, WestNotFound, canon_path

def _configparser():            # for internal use
    return configparser.ConfigParser(allow_no_value=True)

# Configuration values.
#
# Initially empty, populated in read_config(). Always having this available is
# nice in case something checks configuration values before the configuration
# file has been read (e.g. the log.py functions, to check color settings, and
# tests).
config = _configparser()

class ConfigFile(Enum):
    '''Types of west configuration file.

    Enumeration members:

    - SYSTEM: system level configuration shared by all users
    - GLOBAL: global or user-wide configuration
    - LOCAL: per-workspace configuration
    - ALL: all three of the above, where applicable
    '''
    ALL = 1
    SYSTEM = 2
    GLOBAL = 3
    LOCAL = 4

def read_config(configfile=None, config=config, topdir=None,
                config_file=None):
    '''Read configuration files into *config*.

    Reads the files given by *configfile*, storing the values into the
    configparser.ConfigParser object *config*. If *config* is not
    given, the global `west.configuration.config` object is used.

    If *configfile* is given, only the files implied by its value are
    read. If not given, ``ConfigFile.ALL`` is used.

    If *configfile* requests local configuration options (i.e. if it
    is ``ConfigFile.LOCAL`` or ``ConfigFile.ALL``:

        - If *topdir* is given, topdir/.west/config is read

        - Next, if WEST_CONFIG_LOCAL is set in the environment, its
          contents (a file) are used.

        - Otherwise, the file system is searched for a local
          configuration file, and a failure to find one is ignored.

    :param configfile: a `west.configuration.ConfigFile`
    :param config: configuration object to read into
    :param topdir: west workspace root to read local options from
    :param config_file: deprecated alternative for *configfile*
    '''
    if configfile is not None and config_file is not None:
        raise ValueError('use "configfile" or "config_file"; not both')
    if configfile is None:
        configfile = ConfigFile.ALL
    if config_file is not None:
        configfile = config_file
    config.read(_gather_configs(configfile, topdir), encoding='utf-8')

def update_config(section, key, value, configfile=ConfigFile.LOCAL,
                  topdir=None):
    '''Sets ``section.key`` to *value* in the given configuration file.

    :param section: config section; will be created if it does not exist
    :param key: key to set in the given section
    :param value: value to set the key to
    :param configfile: `west.configuration.ConfigFile`, must not be ALL
    :param topdir: west workspace root to write local config options to

    The destination file to write is given by *configfile*. The
    default value (``ConfigFile.LOCAL``) writes to the local
    configuration file given by:

    - topdir/.west/config, if topdir is given, or
    - the value of 'WEST_CONFIG_LOCAL' in the environment, if set, or
    - the local configuration file in the west workspace
      found by searching the file system (raising WestNotFound if
      one is not found).
    '''
    if configfile == ConfigFile.ALL:
        # Not possible to update ConfigFile.ALL, needs specific conf file here.
        raise ValueError(f'invalid configfile: {configfile}')

    filename = _ensure_config(configfile, topdir)
    updater = configobj.ConfigObj(filename)
    if section not in updater:
        updater[section] = {}
    updater[section][key] = value
    updater.write()

def delete_config(section, key, configfile=None, topdir=None):
    '''Delete the option section.key from the given file or files.

    :param section: section whose key to delete
    :param key: key to delete
    :param configfile: If ConfigFile.ALL, delete section.key in all files
                       where it is set.
                       If None, delete only from the highest-precedence
                       global or local file where it is set, allowing
                       lower-precedence values to take effect again.
                       If a list of ConfigFile enumerators, delete
                       from those files.
                       Otherwise, delete from the given ConfigFile.
    :param topdir: west workspace root to delete local options from

    Deleting the only key in a section deletes the entire section.

    If the option is not set, KeyError is raised.

    If an option is to be deleted from the local configuration file,
    it is:

    - topdir/.west/config, if topdir is given, or
    - the value of 'WEST_CONFIG_LOCAL' in the environment, if set, or
    - the local configuration file in the west workspace
      found by searching the file system (raising WestNotFound if
      one is not found).
    '''
    stop = False
    if configfile is None:
        to_check = [_location(x, topdir=topdir) for x in
                    [ConfigFile.LOCAL, ConfigFile.GLOBAL]]
        stop = True
    elif configfile == ConfigFile.ALL:
        to_check = [_location(x, topdir=topdir) for x in
                    [ConfigFile.SYSTEM, ConfigFile.GLOBAL, ConfigFile.LOCAL]]
    elif isinstance(configfile, ConfigFile):
        to_check = [_location(configfile, topdir=topdir)]
    else:
        to_check = [_location(x, topdir=topdir) for x in configfile]

    found = False
    for path in to_check:
        cobj = configobj.ConfigObj(path)
        if section not in cobj or key not in cobj[section]:
            continue

        del cobj[section][key]
        if not cobj[section].items():
            del cobj[section]
        cobj.write()
        found = True
        if stop:
            break

    if not found:
        raise KeyError(f'{section}.{key}')

def _location(cfg, topdir=None):
    # Making this a function that gets called each time you ask for a
    # configuration file makes it respect updated environment
    # variables (such as XDG_CONFIG_HOME, PROGRAMDATA) if they're set
    # during the program lifetime.
    #
    # Its existence is also relied on in the test cases, to ensure
    # that the WEST_CONFIG_xyz variables are respected and we're not about
    # to clobber the user's own configuration files.
    env = os.environ

    if cfg == ConfigFile.ALL:
        raise ValueError('ConfigFile.ALL has no location')
    elif cfg == ConfigFile.SYSTEM:
        if 'WEST_CONFIG_SYSTEM' in env:
            return env['WEST_CONFIG_SYSTEM']

        plat = platform.system()
        if plat == 'Linux':
            return '/etc/westconfig'
        elif plat == 'Darwin':
            return '/usr/local/etc/westconfig'
        elif plat == 'Windows':
            return os.path.expandvars('%PROGRAMDATA%\\west\\config')
        elif 'BSD' in plat:
            return '/etc/westconfig'
        elif 'CYGWIN' in plat:
            # Cygwin can handle windows style paths, so make sure we
            # return one. We don't want to use os.path.join because
            # that uses '/' as separator character, and the ProgramData
            # variable is likely to be something like r'C:\ProgramData'.
            #
            # See https://github.com/zephyrproject-rtos/west/issues/300
            # for details.
            pd = pathlib.PureWindowsPath(os.environ['ProgramData'])
            return str(pd / 'west' / 'config')
        else:
            raise ValueError('unsupported platform ' + plat)
    elif cfg == ConfigFile.GLOBAL:
        if 'WEST_CONFIG_GLOBAL' in env:
            return env['WEST_CONFIG_GLOBAL']
        elif platform.system() == 'Linux' and 'XDG_CONFIG_HOME' in env:
            return os.path.join(env['XDG_CONFIG_HOME'], 'west', 'config')
        else:
            return canon_path(
                os.path.join(os.path.expanduser('~'), '.westconfig'))
    elif cfg == ConfigFile.LOCAL:
        if topdir:
            return os.path.join(topdir, '.west', 'config')
        elif 'WEST_CONFIG_LOCAL' in env:
            return env['WEST_CONFIG_LOCAL']
        else:
            # Might raise WestNotFound!
            return os.path.join(west_dir(), 'config')
    else:
        raise ValueError(f'invalid configuration file {cfg}')

def _gather_configs(cfg, topdir):
    # Find the paths to the given configuration files, in increasing
    # precedence order.
    ret = []

    if cfg == ConfigFile.ALL or cfg == ConfigFile.SYSTEM:
        ret.append(_location(ConfigFile.SYSTEM, topdir=topdir))
    if cfg == ConfigFile.ALL or cfg == ConfigFile.GLOBAL:
        ret.append(_location(ConfigFile.GLOBAL, topdir=topdir))
    if cfg == ConfigFile.ALL or cfg == ConfigFile.LOCAL:
        try:
            ret.append(_location(ConfigFile.LOCAL, topdir=topdir))
        except WestNotFound:
            pass

    return ret

def _ensure_config(configfile, topdir):
    # Ensure the given configfile exists, returning its path. May
    # raise permissions errors, WestNotFound, etc.
    loc = _location(configfile, topdir=topdir)
    path = pathlib.Path(loc)

    if path.is_file():
        return loc

    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    return canon_path(str(path))
