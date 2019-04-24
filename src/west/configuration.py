# Copyright (c) 2018, Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

'''West configuration file handling.'''

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

    - SYSTEM: the system-wide file shared by all users
    - GLOBAL: the "global" or user-wide file
    - LOCAL: the per-installation file
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


def read_config(config_file=ConfigFile.ALL, config=config):
    '''Reads all configuration files, making the configuration values available as
    a configparser.ConfigParser object in config.config. This object works
    similarly to a dictionary: config.config['foo']['bar'] gets the value for
    key 'bar' in section 'foo'.

    If config_file is given, then read only that particular file, file can be
    either 'ConfigFile.LOCAL', 'ConfigFile.GLOBAL', or 'ConfigFile.SYSTEM'.

    If config object is provided, then configuration values will be copied to
    there instead of the module global 'config' variable.

    Git conventions for configuration file locations are used. See the FILES
    section in the git-config(1) man page.

    The following configuration files are read.

    System-wide:

    - Linux: ``/etc/westconfig``
    - macOS: ``/usr/local/etc/westconfig``
    - Windows: ``%PROGRAMDATA%\\west\\config``

    "Global" or user-wide:

    - Linux: ``~/.westconfig`` or ``$XDG_CONFIG_HOME/west/config``
    - macOS: ``~/.westconfig``
    - Windows: ``.westconfig`` in the user's home directory, as determined
      by os.path.expanduser.

    Local (per-installation)

    - Linux, macOS, Windows: ``path/to/installation/.west/config``

    Configuration values from later configuration files override configuration
    from earlier ones. Local values have highest precedence, and system values
    lowest.'''

    # Gather (potential) configuration file paths
    files = []

    # System-wide and user-specific
    if config_file == ConfigFile.ALL or config_file == ConfigFile.SYSTEM:
        files.append(ConfigFile.SYSTEM.value)

    if config_file == ConfigFile.ALL and platform.system() == 'Linux':
        files.append(os.path.join(os.environ.get(
            'XDG_CONFIG_HOME',
            os.path.expanduser('~/.config')),
            'west', 'config'))

    if config_file == ConfigFile.ALL or config_file == ConfigFile.GLOBAL:
        files.append(ConfigFile.GLOBAL.value)

    # Repository-specific

    if config_file == ConfigFile.ALL or config_file == ConfigFile.LOCAL:
        try:
            files.append(os.path.join(west_dir(), ConfigFile.LOCAL.value))
        except WestNotFound:
            pass

    #
    # Parse all existing configuration files
    #
    config.read(files, encoding='utf-8')


def update_config(section, key, value, configfile=ConfigFile.LOCAL):
    '''
    Sets 'key' to 'value' in the given config 'section', creating the section
    if it does not exist.

    The destination file to write is given by 'configfile'.
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


def use_colors():
    # Convenience function for reading the color.ui setting
    return config.getboolean('color', 'ui', fallback=True)
