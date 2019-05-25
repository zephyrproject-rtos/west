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

from west.util import west_dir, WestNotFound, canon_path


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
    ALL = 1
    SYSTEM = 2
    GLOBAL = 3
    LOCAL = 4

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

    filename = _location(configfile)

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
        if platform.system() == 'Linux' and 'XDG_CONFIG_HOME' in env:
            return os.path.join(env['XDG_CONFIG_HOME'], 'west', 'config')
        else:
            return canon_path(
                os.path.join(os.path.expanduser('~'), '.westconfig'))
    elif cfg == ConfigFile.LOCAL:
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


def use_colors():
    # Convenience function for reading the color.ui setting
    return config.getboolean('color', 'ui', fallback=True)
