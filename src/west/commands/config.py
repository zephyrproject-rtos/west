# Copyright (c) 2019, Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

'''West config commands'''

import argparse
import configparser

from west import log
from west import configuration
from west.configuration import ConfigFile
from west.commands import WestCommand, CommandError

CONFIG_DESCRIPTION = '''\
West configuration file handling.

West follows Git-like conventions for configuration file locations.
There are three types of configuration file: system-wide files apply
to all users on the current machine, global files apply to the current
user, and local files apply to the current west installation.

System files:

- Linux: /etc/westconfig
- macOS: /usr/local/etc/westconfig
- Windows: %PROGRAMDATA%\\west\\config

Global files:

- Linux: ~/.westconfig or (if $XDG_CONFIG_HOME is set)
  $XDG_CONFIG_HOME/west/config
- macOS: ~/.westconfig
- Windows: .westconfig in the user's home directory, as determined
  by os.path.expanduser.

Local files:

- Linux, macOS, Windows: <installation-root-directory>/.west/config

Configuration values from later configuration files override configuration
from earlier ones. Local values have highest precedence, and system values
lowest.

To get a value for <name>, type:
    west config <name>

To set a value for <name>, type:
    west config <name> <value>
'''

CONFIG_EPILOG = '''\
If the configuration file to use is not set, reads use all three in
precedence order, and writes use the local file.'''

ALL = ConfigFile.ALL
SYSTEM = ConfigFile.SYSTEM
GLOBAL = ConfigFile.GLOBAL
LOCAL = ConfigFile.LOCAL

class Once(argparse.Action):
    # For enforcing mutual exclusion of options by ensuring self.dest
    # can only be set once.
    #
    # This sets the 'configfile' attribute depending on the option string,
    # which must be --system, --global, or --local.

    def __call__(self, parser, namespace, ignored, option_string=None):
        values = {'--system': SYSTEM, '--global': GLOBAL, '--local': LOCAL}
        rev = {v: k for k, v in values.items()}

        if getattr(namespace, self.dest):
            previous = rev[getattr(namespace, self.dest)]
            parser.error("argument {}: not allowed with argument {}".
                         format(option_string, previous))

        setattr(namespace, self.dest, values[option_string])

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
            description=self.description,
            epilog=CONFIG_EPILOG)

        group = parser.add_argument_group(
            'configuration file to use (give at most one)')
        group.add_argument('--system', dest='configfile', nargs=0, action=Once,
                           help='system-wide file')
        group.add_argument('--global', dest='configfile', nargs=0, action=Once,
                           help='global (user-wide) file')
        group.add_argument('--local', dest='configfile', nargs=0, action=Once,
                           help="this installation's file")

        parser.add_argument('name',
                            help='''config option in section.key format;
                            e.g. "foo.bar" is section "foo", key "bar"''')
        parser.add_argument('value', nargs='?', help='value to set "name" to')

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
                log.dbg('{} is unset'.format(args.name))
                raise CommandError(returncode=1)
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
