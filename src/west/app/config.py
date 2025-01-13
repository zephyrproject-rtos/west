# Copyright (c) 2019, Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

'''West config commands'''

import argparse

from west.commands import CommandError, WestCommand
from west.configuration import ConfigFile

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

To append to a value for <name>, type:
    west config -a <name> <value>
A value must exist in the selected configuration file in order to be able
to append to it. The existing value can be empty.
Examples:
    west config -a build.cmake-args -- " -DEXTRA_CFLAGS='-Wextra -g0' -DFOO=BAR"
    west config -a manifest.group-filter ,+optional

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
precedence order, and writes (including appends) use the local file.'''

ALL = ConfigFile.ALL
SYSTEM = ConfigFile.SYSTEM
GLOBAL = ConfigFile.GLOBAL
LOCAL = ConfigFile.LOCAL


class Config(WestCommand):
    def __init__(self):
        super().__init__(
            'config',
            'get or set config file values',
            CONFIG_DESCRIPTION,
            requires_workspace=False)

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(
            self.name,
            help=self.help,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            description=self.description,
            epilog=CONFIG_EPILOG)

        group = parser.add_argument_group(
            "action to perform (give at most one)"
        ).add_mutually_exclusive_group()

        group.add_argument('-l', '--list', action='store_true',
                           help='list all options and their values')
        group.add_argument('-d', '--delete', action='store_true',
                           help='delete an option in one config file')
        group.add_argument('-D', '--delete-all', action='store_true',
                           help="delete an option everywhere it's set")
        group.add_argument('-a', '--append', action='store_true',
                           help='append to an existing value')

        group = parser.add_argument_group(
            "configuration file to use (give at most one)"
        ).add_mutually_exclusive_group()

        group.add_argument('--system', dest='configfile',
                           action='store_const', const=SYSTEM,
                           help='system-wide file')
        group.add_argument('--global', dest='configfile',
                           action='store_const', const=GLOBAL,
                           help='global (user-wide) file')
        group.add_argument('--local', dest='configfile',
                           action='store_const', const=LOCAL,
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
        elif not args.name:
            self.parser.error('missing argument name '
                              '(to list all options and values, use -l)')
        elif args.append:
            if args.value is None:
                self.parser.error('-a requires both name and value')

        if args.list:
            self.list(args)
        elif delete:
            self.delete(args)
        elif args.value is None:
            self.read(args)
        elif args.append:
            self.append(args)
        else:
            self.write(args)

    def list(self, args):
        what = args.configfile or ALL
        for option, value in self.config.items(configfile=what):
            self.inf(f'{option}={value}')

    def delete(self, args):
        self.check_config(args.name)
        if args.delete_all:
            configfiles = [ALL]
        elif args.configfile:
            configfiles = [args.configfile]
        else:
            # local or global, whichever comes first
            configfiles = [LOCAL, GLOBAL]

        for i, configfile in enumerate(configfiles):
            try:
                self.config.delete(args.name, configfile=configfile)
                return
            except KeyError as err:
                if i == len(configfiles) - 1:
                    self.dbg(
                        f'{args.name} was not set in requested location(s)')
                    raise CommandError(returncode=1) from err
            except PermissionError as pe:
                self._perm_error(pe, configfile, args.name)

    def check_config(self, option):
        if '.' not in option:
            self.die(f'invalid configuration option "{option}"; '
                     'expected "section.key" format')

    def read(self, args):
        self.check_config(args.name)
        value = self.config.get(args.name, configfile=args.configfile or ALL)
        if value is not None:
            self.inf(value)
        else:
            self.err(f'{args.name} is unset')
            raise CommandError(returncode=1)

    def append(self, args):
        self.check_config(args.name)
        where = args.configfile or LOCAL
        value = self.config.get(args.name, configfile=where)
        if value is None:
            self.die(f'option {args.name} not found in the {where.name.lower()} '
                     'configuration file')
        args.value = value + args.value
        self.write(args)

    def write(self, args):
        self.check_config(args.name)
        what = args.configfile or LOCAL
        try:
            self.config.set(args.name, args.value, configfile=what)
        except PermissionError as pe:
            self._perm_error(pe, what, args.name)

    def _perm_error(self, pe, what, name):
        rootp = ('; are you root/administrator?' if what in [SYSTEM, ALL]
                 else '')
        self.die(f"can't update {name}: "
                 f"permission denied when writing {pe.filename}{rootp}")
