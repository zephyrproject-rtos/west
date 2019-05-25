# Copyright (c) 2019, Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

'''West config commands'''

import argparse
import configparser
import platform

from west import log
from west import configuration
from west.configuration import ConfigFile
from west.commands import WestCommand

CONFIG_DESCRIPTION = '''\
West configuration file handling.

This command allows getting or setting configuration options in the
per-installation configuration file, west user configuration file, or the
system wide configuration file.

System-wide:

    Linux:   /etc/westconfig
    Mac OS:  /usr/local/etc/westconfig
    Windows: %PROGRAMDATA%\\west\\config

User-specific:

    Linux:   $XDG_CONFIG_HOME/west/config (only getting)
             ~/.westconfig
    Mac OS:  ~/.westconfig
    Windows: %HOME%\\.westconfig

    ($XDG_CONFIG_DIR defaults to ~/.config/ if unset.)

Per-installation specific file:

    <West base directory>/.west/config

To get a value for <name>, type:
    west config <name>

To set a value for <name>, type:
    west config <name> <value>
'''


class Config(WestCommand):
    def __init__(self):
        super().__init__(
            'config',
            'get or set configuration settings in west config files',
            CONFIG_DESCRIPTION)

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(
            self.name,
            help=self.help,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            description=self.description)

        parser.add_argument(
            'name',
            help='''name is section and key, separated by a dot; e.g. 'foo.bar'
            sets section 'foo', key 'bar' ''')

        parser.add_argument(
            'value',
            nargs='?',
            help='value to set in config file for the given name')

        mx_group = parser.add_mutually_exclusive_group()
        if platform.system() == 'Windows':
            mx_group.add_argument('--global',
                                  dest='configfile',
                                  action='store_const',
                                  const=ConfigFile.GLOBAL,
                                  help='''Use global %%HOME%%\\.westconfig for
                                  values''')
        else:
            mx_group.add_argument('--global',
                                  dest='configfile',
                                  action='store_const',
                                  const=ConfigFile.GLOBAL,
                                  help='Use global ~/.westconfig for values')
        mx_group.add_argument('--local',
                              dest='configfile',
                              action='store_const',
                              const=ConfigFile.LOCAL,
                              help='''Use project specific .west/config for
                              values''')
        mx_group.add_argument('--system',
                              dest='configfile',
                              action='store_const',
                              const=ConfigFile.SYSTEM,
                              help='''Use system specific west config for
                              values''')
        return parser

    def do_run(self, args, user_args):
        config_settings = configparser.ConfigParser()
        configfile = args.configfile or ConfigFile.ALL

        name_list = args.name.split(".", 1)

        if len(name_list) != 2:
            log.die('missing key, please invoke as: west config '
                    '<section>.<key>', exit_code=3)

        section = name_list[0]
        key = name_list[1]

        if args.value is None:
            configuration.read_config(configfile, config_settings)
            value = config_settings.get(section, key, fallback=None)
            if value is not None:
                log.inf(value)
        else:
            if configfile == ConfigFile.ALL:
                # No file given, thus writing defaults to LOCAL
                configfile = ConfigFile.LOCAL
            try:
                configuration.update_config(section, key, args.value,
                                            configfile)
            except PermissionError as pe:
                log.die("can't set {}.{}: permission denied when writing {}{}".
                        format(section, key, pe.filename,
                               ('; are you root/administrator?'
                                if configfile == ConfigFile.SYSTEM else '')))
