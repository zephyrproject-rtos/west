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

Configuration values from later configuration files override configuration
from earlier ones. Local values have highest precedence, and system values
lowest.
'''

import configparser
import os
import platform
from enum import Enum
try:
    # Try to import configobj.
    # If not available we fallback to simple configparser
    import configobj
    use_configobj = True
except ImportError:
    use_configobj = False

from west.util import west_dir, WestNotFound


# Configuration values.
#
# Initially empty, populated in read_config(). Always having this available is
# nice in case something checks configuration values before the configuration
# file has been read (e.g. the log.py functions, to check color settings, and
# tests).
config = configparser.ConfigParser(allow_no_value=True)


class ConfigFile(Enum):
    '''Enum representing the possible types of configuration file.

    Enumerators:

    - SYSTEM: system level configuration shared by all users
    - GLOBAL: global or user-wide configuration
    - LOCAL: per-installation configuration
    - ALL: all three of the above, where applicable
    '''
    ALL = 0
    if platform.system() == 'Linux':
        SYSTEM = '/etc/westconfig'
    elif platform.system() == 'Darwin':  # Mac OS
        # This was seen on a local machine ($(prefix) = /usr/local)
        SYSTEM = '/usr/local/etc/westconfig'
    elif platform.system() == 'Windows':
        # Seen on a local machine
        SYSTEM = os.path.expandvars('%PROGRAMDATA%\\west\\config')
    GLOBAL = os.path.expanduser('~/.westconfig')
    LOCAL = 'config'


def read_config(configfile=None, config=config, config_file=None):
    '''Read configuration files into `config`.

    :param configfile: a `west.config.ConfigFile` enumerator
    :param config: configuration object to read into
    :param config_file: deprecated alternative spelling for ``configfile``

    Reads the files given by `configfile`, storing the values into
    the configparser.ConfigParser object `config`. If `config` is not
    given, the global `west.configuration.config` object is used.

    If `configfile` is given, only the files implied by its value are
    read. If not given, ``ConfigFile.ALL`` is used.'''
    if configfile is not None and config_file is not None:
        raise ValueError('use "configfile" or "config_file"; not both')
    if configfile is None:
        configfile = ConfigFile.ALL
    if config_file is not None:
        configfile = config_file
    config.read(_gather_configs(configfile), encoding='utf-8')


def update_config(section, key, value, configfile=ConfigFile.LOCAL):
    '''Sets ``section.key`` to ``value``.

    :param section: config section; will be created if it does not exist
    :param key: key to set in the given section
    :param value: value to set the key to
    :param configfile: `west.configuration.ConfigFile` enumerator
                       (must not be ConfigFile.ALL)

    The destination file to write is given by `configfile`. The
    default value (ConfigFile.LOCAL) writes to the per-installation
    file .west/config. This function must therefore be called from a
    west installation if this default is used, or WestNotFound will be
    raised.
    '''
    if configfile == ConfigFile.ALL:
        # Not possible to update ConfigFile.ALL, needs specific conf file here.
        raise ValueError('invalid configfile: {}'.format(configfile))

    if configfile == ConfigFile.LOCAL:
        filename = os.path.join(west_dir(), configfile.value)
    else:
        filename = configfile.value

    if use_configobj:
        updater = configobj.ConfigObj(filename)
    else:
        updater = configparser.ConfigParser()
        read_config(configfile, updater)

    if section not in updater:
        updater[section] = {}
    updater[section][key] = value

    if use_configobj:
        updater.write()
    else:
        with open(filename, 'w') as f:
            updater.write(f)


def _gather_configs(configfile):
    # Find the paths to the given configuration files, in increasing
    # precedence order.
    ret = []

    if configfile == ConfigFile.ALL or configfile == ConfigFile.SYSTEM:
        ret.append(ConfigFile.SYSTEM.value)

    if configfile == ConfigFile.ALL and platform.system() == 'Linux':
        ret.append(os.path.join(os.environ.get(
            'XDG_CONFIG_HOME',
            os.path.expanduser('~/.config')),
            'west', 'config'))

    if configfile == ConfigFile.ALL or configfile == ConfigFile.GLOBAL:
        ret.append(ConfigFile.GLOBAL.value)

    if configfile == ConfigFile.ALL or configfile == ConfigFile.LOCAL:
        try:
            ret.append(os.path.join(west_dir(), ConfigFile.LOCAL.value))
        except WestNotFound:
            pass

    return ret


def use_colors():
    # Convenience function for reading the color.ui setting
    return config.getboolean('color', 'ui', fallback=True)
