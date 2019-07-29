#!/usr/bin/env python3

# Copyright 2018 Open Source Foundries Limited.
# Copyright 2019 Foundries.io Limited.
# Copyright (c) 2019, Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

'''Zephyr RTOS meta-tool (west) main module
'''


import argparse
import colorama
from functools import partial
from io import StringIO
import itertools
import logging
import os
import shutil
import sys
from subprocess import CalledProcessError
import tempfile
import textwrap
import traceback

from west import log
from west import configuration as config
from west.commands import extension_commands, \
    CommandError, CommandContextError, ExtensionCommandError
from west.commands.project import List, ManifestCommand, Diff, Status, \
    SelfUpdate, ForAll, Init, Update
from west.commands.config import Config
from west.manifest import Manifest, MalformedConfig, MalformedManifest
from west.util import quote_sh_list, west_topdir, WestNotFound
from west.version import __version__

BUILTIN_COMMANDS = {
    'commands for managing multiple git repositories': [
        Init(),
        Update(),
        List(),
        ManifestCommand(),
        Diff(),
        Status(),
        ForAll(),
    ],

    'configuring west': [Config()],

    # None is for hidden commands we don't want to show to the user.
    None: [SelfUpdate()]
}

# Initialize the set with the virtual 'help' command so that an extension
# command cannot clash with it
BUILTIN_COMMAND_NAMES = set(['help'])
for group, commands in BUILTIN_COMMANDS.items():
    BUILTIN_COMMAND_NAMES.update(c.name for c in commands)


class WestHelpAction(argparse.Action):

    def __init__(self, option_strings, dest, **kwargs):
        kwargs['nargs'] = 0
        super(WestHelpAction, self).__init__(option_strings, dest,
                                             **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        parser.print_help(top_level=True)
        parser.exit()


class WestArgumentParser(argparse.ArgumentParser):
    # The argparse module is infuriatingly coy about its parser and
    # help formatting APIs, marking almost everything you need to
    # customize help output an "implementation detail". Even accessing
    # the parser's description and epilog attributes as we do here is
    # technically breaking the rules.
    #
    # Even though the implementation details have been pretty stable
    # since the module was first introduced in Python 3.2, let's avoid
    # possible headaches by overriding some "proper" argparse APIs
    # here instead of monkey-patching the module or breaking
    # abstraction barriers. This is duplicative but more future-proof.

    def __init__(self, *args, **kwargs):
        # The super constructor calls add_argument(), so this has to
        # come first as our override of that method relies on it.
        self.west_optionals = []
        self.west_extensions = None

        super(WestArgumentParser, self).__init__(*args, **kwargs)

    def print_help(self, file=None, top_level=False):
        print(self.format_help(top_level=top_level),
              file=file or sys.stdout)

    def format_help(self, top_level=False):
        # When top_level is True, we override the parent method to
        # produce more readable output, which separates commands into
        # logical groups. In order to print optionals, we rely on the
        # data available in our add_argument() override below.
        #
        # If top_level is False, it's because we're being called from
        # one of the subcommand parsers, and we delegate to super.

        if not top_level:
            return super(WestArgumentParser, self).format_help()

        # Format the help to be at most 75 columns wide, the maximum
        # generally recommended by typographers for readability.
        #
        # If the terminal width (COLUMNS) is less than 75, use width
        # (COLUMNS - 2) instead, unless that is less than 30 columns
        # wide, which we treat as a hard minimum.
        width = min(75, max(shutil.get_terminal_size().columns - 2, 30))

        with StringIO() as sio:

            def append(*strings):
                for s in strings:
                    print(s, file=sio)

            append(self.format_usage(),
                   self.description,
                   '')

            append('optional arguments:')
            for wo in self.west_optionals:
                self.format_west_optional(append, wo, width)

            append('')
            for group, commands in BUILTIN_COMMANDS.items():
                if group is None:
                    # Skip hidden commands.
                    continue

                append(group + ':')
                for command in commands:
                    self.format_command(append, command, width)
                append('')

            if self.west_extensions is None:
                # This only happens when there is an error.
                # If there are simply no extensions, it's an empty dict.
                append('Cannot load extension commands; '
                       'help for them is not available.')
                append('(To debug, try: "west manifest --validate".)')
                append('')
            else:
                # TODO we may want to be more aggressive about loading
                # command modules by default: the current implementation
                # prevents us from formatting one-line help here.
                #
                # Perhaps a commands.extension_paranoid that if set, uses
                # thunks, and otherwise just loads the modules and
                # provides help for each command.
                #
                # This has its own wrinkle: we can't let a failed
                # import break the built-in commands.
                for path, specs in self.west_extensions.items():
                    # This may occur in case a project defines commands already
                    # defined, in which case it has been filtered out.
                    if not specs:
                        continue

                    append('commands from project at "{}":'.
                           format(path))

                    for spec in specs:
                        self.format_extension_spec(append, spec, width)
                    append('')

            append(self.epilog)

            return sio.getvalue().rstrip()

    def format_west_optional(self, append, wo, width):
        metavar = wo['metavar']
        options = wo['options']
        help = wo.get('help')

        # Join the various options together as a comma-separated list,
        # with the metavar if there is one. That's our "thing".
        if metavar is not None:
            opt_str = '  ' + ', '.join('{} {}'.format(o, metavar)
                                       for o in options)
        else:
            opt_str = '  ' + ', '.join(options)

        # Delegate to the generic formatter.
        self.format_thing_and_help(append, opt_str, help, width)

    def format_command(self, append, command, width):
        thing = '  {}:'.format(command.name)
        self.format_thing_and_help(append, thing, command.help, width)

    def format_extension_spec(self, append, spec, width):
        self.format_thing_and_help(append, '  ' + spec.name + ':',
                                   spec.help, width)

    def format_thing_and_help(self, append, thing, help, width):
        # Format help for some "thing" (arbitrary text) and its
        # corresponding help text an argparse-like way.
        help_offset = min(max(10, width - 20), 24)
        help_indent = ' ' * help_offset

        thinglen = len(thing)

        if help is None:
            # If there's no help string, just print the thing.
            append(thing)
        else:
            # Reflow the lines in help to the desired with, using
            # the help_offset as an initial indent.
            help = ' '.join(help.split())
            help_lines = textwrap.wrap(help, width=width,
                                       initial_indent=help_indent,
                                       subsequent_indent=help_indent)

            if thinglen > help_offset - 1:
                # If the "thing" (plus room for a space) is longer
                # than the initial help offset, print it on its own
                # line, followed by the help on subsequent lines.
                append(thing)
                append(*help_lines)
            else:
                # The "thing" is short enough that we can start
                # printing help on the same line without overflowing
                # the help offset, so combine the "thing" with the
                # first line of help.
                help_lines[0] = thing + help_lines[0][thinglen:]
                append(*help_lines)

    def add_argument(self, *args, **kwargs):
        # Track information we want for formatting help.  The argparse
        # module calls kwargs.pop(), so can't call super first without
        # losing data.
        optional = {'options': [], 'metavar': kwargs.get('metavar', None)}
        need_metavar = (optional['metavar'] is None and
                        kwargs.get('action') in (None, 'store'))
        for arg in args:
            if not arg.startswith('-'):
                break
            optional['options'].append(arg)
            # If no metavar was given, the last option name is
            # used. By convention, long options go last, so this
            # matches the default argparse behavior.
            if need_metavar:
                optional['metavar'] = arg.lstrip('-').translate(
                    {ord('-'): '_'}).upper()
        optional['help'] = kwargs.get('help')
        self.west_optionals.append(optional)

        # Let argparse handle the actual argument.
        super(WestArgumentParser, self).add_argument(*args, **kwargs)

    def set_extensions(self, extensions):
        self.west_extensions = extensions


def _make_parsers():
    # Make a fresh instance of the top level argument parser,
    # subparser generator, and return them in that order.

    # The prog='west' override avoids the absolute path of the main.py script
    # showing up when West is run via the wrapper
    parser = WestArgumentParser(
        prog='west', description='The Zephyr RTOS meta-tool.',
        epilog='''Run "west <command> -h" or "west help <command>" for detailed
help on each command.''',
        add_help=False)

    # Remember to update scripts/west-completion.bash if you add or remove
    # flags

    parser.add_argument('-h', '--help', action=WestHelpAction,
                        help='show this help message and exit')

    parser.add_argument('-z', '--zephyr-base', default=None,
                        help='''Override the Zephyr base directory. The
                        default is the manifest project with path
                        "zephyr".''')

    parser.add_argument('-v', '--verbose', default=0, action='count',
                        help='''Display verbose output. May be given
                        multiple times to increase verbosity.''')

    parser.add_argument('-V', '--version', action='version',
                        version='West version: v{}'.format(__version__),
                        help='print the program version and exit')

    subparser_gen = parser.add_subparsers(metavar='<command>', dest='command')

    return parser, subparser_gen


def command_handler(command, topdir, known_args, unknown_args):
    command.run(known_args, unknown_args, topdir)


def add_ext_command_parser(subparser_gen, spec):
    # This subparser exists only to register the name. The real parser
    # will be created as needed later if the command is invoked. We
    # prevent help from being added because the default help printer
    # calls sys.exit(), which is not what we want.
    parser = subparser_gen.add_parser(spec.name, add_help=False)
    return parser


def ext_command_handler(spec, topdir, argv, *ignored):
    # Deferred creation, argument parsing, and handling for extension
    # commands. We go to the extra effort because we don't want to
    # import any extern classes until the user has specifically
    # requested an extension command.
    #
    # 'ignored' is just the known and unknown args as parsed by the
    # 'dummy' parser added by add_ext_command_parser().
    #
    # The purpose of this handler is to create the "real" parser that
    # we need for the newly instantiated `command`, then re-parse the
    # original argv and run the command.
    command = spec.factory()

    # Our original top level parser and subparser generator have some
    # garbage state that prevents us from registering the 'real'
    # command subparser. Just make new ones.
    west_parser, subparser_gen = _make_parsers()
    command.add_parser(subparser_gen)

    # Handle the instantiated command in the usual way.
    args, unknown = west_parser.parse_known_args(argv)
    command.run(args, unknown, topdir)


def help_command_handler(west_parser, topdir, help_parser, extensions, args,
                         *ignored):
    command_name = args.command
    if not command_name:
        west_parser.print_help(top_level=True)
        return

    if command_name in BUILTIN_COMMAND_NAMES:
        if command_name == 'help':
            help_parser.print_help()
            return
        for group, commands in BUILTIN_COMMANDS.items():
            for command in commands:
                if command.name == command_name:
                    command.parser.print_help()
                    return
    else:
        for path, specs in extensions.items():
            for spec in specs:
                if spec.name != command_name:
                    continue
                # ext_command_handler() does not return
                ext_command_handler(spec, topdir, [command_name, '--help'])
        else:
            west_parser.print_help(top_level=True)


def set_zephyr_base(args):
    '''Ensure ZEPHYR_BASE is set
    Order of precedence:
    1) Value given as command line argument
    2) Value from environment setting: ZEPHYR_BASE
    3) Value of zephyr.base setting in west config file

    Order of precedence between 2) and 3) can be changed with the setting
    zephyr.base-prefer.
    zephyr.base-prefer takes the values 'env' and 'configfile'

    If 2) and 3) has different values and zephyr.base-prefer is unset a warning
    is printed to the user.'''

    if args.zephyr_base:
        # The command line --zephyr-base takes precedence over
        # everything else.
        zb = os.path.abspath(args.zephyr_base)
        zb_origin = 'command line'
    else:
        # If the user doesn't specify it concretely, then use ZEPHYR_BASE
        # from the environment or zephyr.base from west.configuration.
        #
        # `west init` will configure zephyr.base to the project that has path
        # 'zephyr'.
        #
        # At some point, we need a more flexible way to set environment
        # variables based on manifest contents, but this is good enough
        # to get started with and to ask for wider testing.
        zb_env = os.environ.get('ZEPHYR_BASE')
        zb_prefer = config.config.get('zephyr', 'base-prefer',
                                      fallback=None)
        zb_config = config.config.get('zephyr', 'base', fallback=None)
        if zb_config is not None:
            zb_config = os.path.join(west_topdir(), zb_config)

        if zb_prefer == 'env' and zb_env is not None:
            zb = zb_env
            zb_origin = 'env'
        elif zb_prefer == 'configfile' and zb_config is not None:
            zb = zb_config
            zb_origin = 'configfile'
        elif zb_env is not None:
            zb = zb_env
            zb_origin = 'env'
            try:
                different = (zb_config is not None and
                             not os.path.samefile(zb_config, zb_env))
            except FileNotFoundError:
                different = (zb_config is not None and
                             (os.path.normpath(os.path.abspath(zb_config)) !=
                              os.path.normpath(os.path.abspath(zb_env))))
            if different:
                # The environment ZEPHYR_BASE takes precedence over the config
                # setting, but in normal multi-repo operation we shouldn't
                # expect to need to set ZEPHYR_BASE.
                # Therefore issue a warning as it might have happened that
                # zephyr-env.sh/cmd was run in some other zephyr installation,
                # and the user forgot about that.
                log.wrn('ZEPHYR_BASE={}'.format(zb_env),
                        'in the calling environment will be used, but was set '
                        'to', zb_config, 'in west config.\n To disable this '
                        'warning, execute '
                        '\'west config --global zephyr.base-prefer env\'')
        elif zb_config is not None:
            zb = zb_config
            zb_origin = 'configfile'
        else:
            zb = None
            zb_origin = None
            # No --zephyr-base, no ZEPHYR_BASE envronment and no zephyr.base
            # Fallback to loop over projects, to identify if a project has path
            # 'zephyr' for fallback.
            try:
                manifest = Manifest.from_file()
                for project in manifest.projects:
                    if project.path == 'zephyr':
                        zb = project.abspath
                        zb_origin = 'manifest file {}'.format(manifest.path)
                        break
                else:
                    log.err('no --zephyr-base given, ZEPHYR_BASE is unset,',
                            'west config contains no zephyr.base setting,',
                            'and no manifest project has path "zephyr"')
            except MalformedConfig as e:
                log.wrn("Can't set ZEPHYR_BASE:",
                        'parsing of manifest file failed during command',
                        args.command, ':', *e.args)
            except WestNotFound:
                log.wrn("Can't set ZEPHYR_BASE:",
                        'not currently in a west installation')

    if zb is not None:
        os.environ['ZEPHYR_BASE'] = zb
        log.dbg('ZEPHYR_BASE={} (origin: {})'.format(zb, zb_origin))


def parse_args(argv, extensions, topdir):
    west_parser, subparser_gen = _make_parsers()

    # Add handlers for the built-in commands.
    for command in itertools.chain(*BUILTIN_COMMANDS.values()):
        parser = command.add_parser(subparser_gen)
        parser.set_defaults(handler=partial(command_handler, command, topdir))

    # Add handlers for extension commands, and finalize the list with
    # our parser.
    if extensions:
        for path, specs in extensions.items():
            for spec in specs:
                parser = add_ext_command_parser(subparser_gen, spec)
                parser.set_defaults(handler=partial(ext_command_handler,
                                                    spec, topdir, argv))
    west_parser.set_extensions(extensions)

    help_parser = subparser_gen.add_parser('help',
                                           help='get help on a west command')
    help_parser.add_argument('command', nargs='?')
    help_parser.set_defaults(handler=partial(help_command_handler, west_parser,
                                             topdir, help_parser, extensions))

    # Parse arguments.
    args, unknown = west_parser.parse_known_args(args=argv)

    # Set up logging verbosity before running the command, so
    # e.g. verbose messages related to argument handling errors work
    # properly. This works even for extension commands that haven't
    # been instantiated yet, because --verbose is an option to the top
    # level parser, and the command run() method doesn't get called
    # until later.
    log.set_verbosity(args.verbose)

    if args.command != 'init':  # init --> we haven't got a zephyr repo.
        set_zephyr_base(args)

    if 'handler' not in args:
        west_parser.print_help(file=sys.stderr, top_level=True)
        sys.exit(1)

    return args, unknown


def get_extension_commands():
    extensions = extension_commands()
    extension_names = set()

    for path, specs in extensions.items():
        # Filter out attempts to shadow built-in commands as well as
        # commands which have names which are already used.
        filtered = []
        for spec in specs:
            if spec.name in BUILTIN_COMMAND_NAMES:
                log.wrn('ignoring project {} extension command {};'.
                        format(spec.project.name, spec.name),
                        'this is a built in command')
                continue
            if spec.name in extension_names:
                log.wrn('ignoring project {} extension command "{}";'.
                        format(spec.project.name, spec.name),
                        'command "{}" already defined as extension command'.
                        format(spec.name))
                continue
            filtered.append(spec)
            extension_names.add(spec.name)
        extensions[path] = filtered

    return extensions


def dump_traceback():
    # Save the current exception to a file and return its path.
    fd, name = tempfile.mkstemp(prefix='west-exc-', suffix='.txt')
    os.close(fd)        # traceback has no use for the fd
    with open(name, 'w') as f:
        traceback.print_exc(file=f)
    return name


def main(argv=None):
    # Silence validation errors from pykwalify, which are logged at
    # logging.ERROR level. We want to handle those ourselves as
    # needed.
    logging.getLogger('pykwalify').setLevel(logging.CRITICAL)

    # Makes ANSI color escapes work on Windows, and strips them when
    # stdout/stderr isn't a terminal
    colorama.init()

    # See if we're in an installation.
    try:
        topdir = west_topdir()
    except WestNotFound:
        topdir = None

    # Read the configuration files before looking for extensions.
    # We need this to find the manifest path in order to load extensions.
    config.read_config()

    # Load any extension command specs if we're in an installation.
    if topdir:
        try:
            extensions = get_extension_commands()
        except (MalformedManifest, MalformedConfig, FileNotFoundError):
            extensions = None
    else:
        extensions = {}

    if argv is None:
        argv = sys.argv[1:]
    args, unknown = parse_args(argv, extensions, topdir)

    try:
        args.handler(args, unknown)
    except KeyboardInterrupt:
        sys.exit(0)
    except CalledProcessError as cpe:
        log.err('command exited with status {}: {}'.
                format(cpe.returncode, quote_sh_list(cpe.cmd)))
        if args.verbose:
            traceback.print_exc()
        sys.exit(cpe.returncode)
    except ExtensionCommandError as ece:
        msg = 'extension command "{}" could not be run{}.'.format(
            args.command, ': ' + ece.hint if ece.hint else '')
        if args.verbose:
            log.err(msg)
            traceback.print_exc()
        else:
            log.err(msg, 'See {} for a traceback.'.format(dump_traceback()))
        sys.exit(ece.returncode)
    except CommandContextError as cce:
        log.err('command', args.command, 'cannot be run in this context:',
                *cce.args)
        log.err('see {} for a traceback.'.format(dump_traceback()))
        sys.exit(cce.returncode)
    except CommandError as ce:
        # No need to dump_traceback() here. The command is responsible
        # for logging its own errors.
        sys.exit(ce.returncode)
    except (MalformedManifest, MalformedConfig) as malformed:
        log.die("can't load west manifest:", malformed)


if __name__ == "__main__":
    main()
