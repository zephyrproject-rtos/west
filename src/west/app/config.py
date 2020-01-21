# Copyright (c) 2019, Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

'''West config commands'''

import argparse
import configparser

from west import log
from west.configuration import read_config, update_config, delete_config, \
    ConfigFile
from west.commands import WestCommand, CommandError

CONFIG_DESCRIPTION = '''\
West configuration file handling.

West follows Git-like conventions for configuration file locations.
There are three types of configuration file: system-wide files apply
to all users on the current machine, global files apply to the current
user, and local files apply to the current west workspace.

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

- Linux, macOS, Windows: <workspace-root-directory>/.west/config

You can override these files' locations with the WEST_CONFIG_SYSTEM,
WEST_CONFIG_GLOBAL, and WEST_CONFIG_LOCAL environment variables.

Configuration values from later configuration files override configuration
from earlier ones. Local values have highest precedence, and system values
lowest.

To get a value for <name>, type:
    west config <name>

To set a value for <name>, type:
    west config <name> <value>

To list all options and their values:
    west config -l

To delete <name> in the local or global file (wherever it's set
first, not in both; if set locally, global values become visible):
    west config -d <name>

To delete <name> in the global file only:
    west config -d --global <name>

To delete <name> everywhere it's set, including the system file:
    west config -D <name>
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
            parser.error(f"argument {option_string}: "
                         f"not allowed with argument {previous}")

        setattr(namespace, self.dest, values[option_string])

class Config(WestCommand):
    def __init__(self):
        super().__init__(
            'config',
            'get or set configuration settings in west config files',
            CONFIG_DESCRIPTION,
            requires_workspace=False)

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(
            self.name,
            help=self.help,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            description=self.description,
            epilog=CONFIG_EPILOG)

        parser.add_argument('-l', '--list', action='store_true',
                            help='list all options and their values')
        parser.add_argument('-d', '--delete', action='store_true',
                            help='delete an option in one config file')
        parser.add_argument('-D', '--delete-all', action='store_true',
                            help="delete an option everywhere it's set")

        group = parser.add_argument_group(
            'configuration file to use (give at most one)')
        group.add_argument('--system', dest='configfile', nargs=0, action=Once,
                           help='system-wide file')
        group.add_argument('--global', dest='configfile', nargs=0, action=Once,
                           help='global (user-wide) file')
        group.add_argument('--local', dest='configfile', nargs=0, action=Once,
                           help="this workspace's file")

        parser.add_argument('name', nargs='?',
                            help='''config option in section.key format;
                            e.g. "foo.bar" is section "foo", key "bar"''')
        parser.add_argument('value', nargs='?', help='value to set "name" to')

        return parser

    def do_run(self, args, user_args):
        delete = args.delete or args.delete_all
        if args.list:
            if args.name:
                self.parser.error('-l cannot be combined with name argument')
            elif delete:
                self.parser.error('-l cannot be combined with -d or -D')
        elif not args.name:
            self.parser.error('missing argument name '
                              '(to list all options and values, use -l)')
        elif args.delete and args.delete_all:
            self.parser.error('-d cannot be combined with -D')

        if args.list:
            self.list(args)
        elif delete:
            self.delete(args)
        elif args.value is None:
            self.read(args)
        else:
            self.write(args)

    def list(self, args):
        cfg = configparser.ConfigParser()
        what = args.configfile or ALL
        read_config(configfile=what, config=cfg)
        for s in cfg.sections():
            for k, v in cfg[s].items():
                log.inf(f'{s}.{k}={v}')

    def delete(self, args):
        section, key = self._sk(args)
        if args.delete_all:
            what = ALL
        elif args.configfile:
            what = args.configfile
        else:
            what = None   # local or global, whichever comes first

        try:
            delete_config(section, key, configfile=what)
        except KeyError:
            log.dbg(f'{args.name} was not set in requested location(s)')
            raise CommandError(returncode=1)
        except PermissionError as pe:
            self._perm_error(pe, what, section, key)

    def read(self, args):
        section, key = self._sk(args)
        cfg = configparser.ConfigParser()
        read_config(configfile=args.configfile or ALL, config=cfg)
        value = cfg.get(section, key, fallback=None)
        if value is not None:
            log.inf(value)
        else:
            log.dbg(f'{args.name} is unset')
            raise CommandError(returncode=1)

    def write(self, args):
        section, key = self._sk(args)
        what = args.configfile or LOCAL
        try:
            update_config(section, key, args.value, configfile=what)
        except PermissionError as pe:
            self._perm_error(pe, what, section, key)

    def _sk(self, args):
        name_list = args.name.split(".", 1)
        if len(name_list) != 2:
            self.parser.error(f"name '{args.name}' should be in the form "
                              "<section>.<key>")
        return name_list[0], name_list[1]

    def _perm_error(self, pe, what, section, key):
        rootp = ('; are you root/administrator?' if what in [SYSTEM, ALL]
                 else '')
        log.die(f"can't update {section}.{key}: "
                f"permission denied when writing {pe.filename}{rootp}")
