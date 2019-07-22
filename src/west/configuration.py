# Copyright (c) 2018, 2019, Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

'''West configuration file handling.

West follows Git-like conventions for configuration file locations.
There are three types of configuration file: system-wide files apply
to all users on the current machine, global files apply to the current
user, and local files apply to the current west installation.

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

- Linux, macOS, Windows: ``<installation-root-directory>/.west/config``

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


# Configuration values.
#
# Initially empty, populated in read_config(). Always having this available is
# nice in case something checks configuration values before the configuration
# file has been read (e.g. the log.py functions, to check color settings, and
# tests).
config = configparser.ConfigParser(allow_no_value=True)

class ConfigFile(Enum):
    '''Types of west configuration file.

    Enumeration members:

    - SYSTEM: system level configuration shared by all users
    - GLOBAL: global or user-wide configuration
    - LOCAL: per-installation configuration
    - ALL: all three of the above, where applicable
    '''
    ALL = 1
    SYSTEM = 2
    GLOBAL = 3
    LOCAL = 4

def read_config(configfile=None, config=config, config_file=None):
    '''Read configuration files into *config*.

    :param configfile: a `west.configuration.ConfigFile`
    :param config: configuration object to read into
    :param config_file: deprecated alternative spelling for *configfile*

    Reads the files given by *configfile*, storing the values into
    the configparser.ConfigParser object *config*. If *config* is not
    given, the global `west.configuration.config` object is used.

    If *configfile* is given, only the files implied by its value are
    read. If not given, ``ConfigFile.ALL`` is used.
    '''
    if configfile is not None and config_file is not None:
        raise ValueError('use "configfile" or "config_file"; not both')
    if configfile is None:
        configfile = ConfigFile.ALL
    if config_file is not None:
        configfile = config_file
    config.read(_gather_configs(configfile), encoding='utf-8')


def update_config(section, key, value, configfile=ConfigFile.LOCAL):
    '''Sets ``section.key`` to *value* in the given configuration file.

    :param section: config section; will be created if it does not exist
    :param key: key to set in the given section
    :param value: value to set the key to
    :param configfile: `west.configuration.ConfigFile`, must not be ALL

    The destination file to write is given by *configfile*. The
    default value (``ConfigFile.LOCAL``) writes to the per-installation
    file .west/config. This function must therefore be called from a
    west installation if this default is used, or WestNotFound will be
    raised.
    '''
    if configfile == ConfigFile.ALL:
        # Not possible to update ConfigFile.ALL, needs specific conf file here.
        raise ValueError('invalid configfile: {}'.format(configfile))

    filename = _ensure_config(configfile)
    updater = configobj.ConfigObj(filename)
    if section not in updater:
        updater[section] = {}
    updater[section][key] = value
    updater.write()

def delete_config(section, key, configfile=None):
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

    Deleting the only key in a section deletes the entire section.

    If the option is not set, KeyError is raised.'''
    stop = False
    if configfile is None:
        to_check = [_location(x) for x in
                    [ConfigFile.LOCAL, ConfigFile.GLOBAL]]
        stop = True
    elif configfile == ConfigFile.ALL:
        to_check = [_location(x) for x in
                    [ConfigFile.SYSTEM, ConfigFile.GLOBAL, ConfigFile.LOCAL]]
    elif isinstance(configfile, ConfigFile):
        to_check = [_location(configfile)]
    else:
        to_check = [_location(x) for x in configfile]

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
        raise KeyError('{}.{}'.format(section, key))

def _location(cfg):
    # Making this a function that gets called each time you ask for a
    # configuration file makes it respect updated environment
    # variables (such as XDG_CONFIG_HOME, PROGRAMDATA) if they're set
    # during the program lifetime. It also makes it easier to
    # monkey-patch for testing :).
    env = os.environ

    if cfg == ConfigFile.ALL:
        raise ValueError('ConfigFile.ALL has no location')
    elif cfg == ConfigFile.SYSTEM:
        if 'WEST_CONFIG_SYSTEM' in os.environ:
            return os.environ['WEST_CONFIG_SYSTEM']

        plat = platform.system()
        if plat == 'Linux':
            return '/etc/westconfig'
        elif plat == 'Darwin':
            return '/usr/local/etc/westconfig'
        elif plat == 'Windows':
            return os.path.expandvars('%PROGRAMDATA%\\west\\config')
        elif 'BSD' in plat:
            return '/etc/westconfig'
        else:
            raise ValueError('unsupported platform ' + plat)
    elif cfg == ConfigFile.GLOBAL:
        if 'WEST_CONFIG_GLOBAL' in os.environ:
            return os.environ['WEST_CONFIG_GLOBAL']
        elif platform.system() == 'Linux' and 'XDG_CONFIG_HOME' in env:
            return os.path.join(env['XDG_CONFIG_HOME'], 'west', 'config')
        else:
            return canon_path(
                os.path.join(os.path.expanduser('~'), '.westconfig'))
    elif cfg == ConfigFile.LOCAL:
        if 'WEST_CONFIG_LOCAL' in os.environ:
            return os.environ['WEST_CONFIG_LOCAL']
        else:
            # Might raise WestNotFound!
            return os.path.join(west_dir(), 'config')
    else:
        raise ValueError('invalid configuration file {}'.format(cfg))

def _gather_configs(cfg):
    # Find the paths to the given configuration files, in increasing
    # precedence order.
    ret = []

    if cfg == ConfigFile.ALL or cfg == ConfigFile.SYSTEM:
        ret.append(_location(ConfigFile.SYSTEM))
    if cfg == ConfigFile.ALL or cfg == ConfigFile.GLOBAL:
        ret.append(_location(ConfigFile.GLOBAL))
    if cfg == ConfigFile.ALL or cfg == ConfigFile.LOCAL:
        try:
            ret.append(_location(ConfigFile.LOCAL))
        except WestNotFound:
            pass

    return ret


def _ensure_config(configfile):
    # Ensure the given configfile exists, returning its path. May
    # raise permissions errors, WestNotFound, etc.
    #
    # Uses pathlib as this is hard to implement correctly without it.
    loc = _location(configfile)
    path = pathlib.Path(loc)

    if path.is_file():
        return loc

    # Create the directory. We can't use
    #     path.parent.mkdir(..., exist_ok=True)
    # in Python 3.4, so roughly emulate its behavior.
    try:
        path.parent.mkdir(parents=True)
    except FileExistsError:
        pass

    path.touch(exist_ok=True)
    return canon_path(str(path))
