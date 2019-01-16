#!/usr/bin/env python3

# Copyright 2018 Open Source Foundries Limited.
# Copyright 2019 Foundries.io Limited.
#
# SPDX-License-Identifier: Apache-2.0

'''Zephyr RTOS meta-tool (west) main module
'''


import argparse
import colorama
from functools import partial
from io import StringIO
import itertools
import os
import shutil
import sys
from subprocess import CalledProcessError, check_output, DEVNULL
import textwrap

from west import log
from west import config
from west.commands import CommandContextError
from west.commands.build import Build
from west.commands.flash import Flash
from west.commands.debug import Debug, DebugServer, Attach
from west.commands.project import List, Clone, Fetch, Pull, Rebase, Branch, \
                             Checkout, Diff, Status, Update, ForAll, \
                             WestUpdated, PostInit
from west.manifest import Manifest, MalformedConfig
from west.util import quote_sh_list, in_multirepo_install

IN_MULTIREPO_INSTALL = in_multirepo_install(os.path.dirname(__file__))

RUNNER_COMMANDS = {
    'building and running zephyr': [
        Build(),
        Flash(),
        Debug(),
        DebugServer(),
        Attach()
    ]
}

PROJECT_COMMANDS = {
    'managing multiple repositories in the installation': [
        List(),
        Clone(),
        Fetch(),
        Pull(),
        Rebase(),
        Branch(),
        Checkout(),
        Diff(),
        Status(),
        Update(),
        ForAll()
    ]
}

# Commands we don't want to show to the user. For now, this is PostInit.
HIDDEN_COMMANDS = {None: [PostInit()]}

# Built-in commands in this West. For compatibility with monorepo
# installations of West within the Zephyr tree, we only expose the
# project commands if this is a multirepo installation.
COMMANDS = dict(RUNNER_COMMANDS)

if IN_MULTIREPO_INSTALL:
    COMMANDS.update(PROJECT_COMMANDS)
    COMMANDS.update(HIDDEN_COMMANDS)


class InvalidWestContext(RuntimeError):
    pass


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

            append('',
                   'west commands used in various situations:')
            for group, commands in COMMANDS.items():
                if group is None:
                    # Skip hidden commands.
                    continue

                append(' ' + group + ':')
                for command in commands:
                    self.format_command(append, command, width)
                append('')

            append(self.epilog)

            return sio.getvalue()

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
        thing = '     {}:'.format(command.name)
        self.format_thing_and_help(append, thing, command.help, width)

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


def command_handler(command, known_args, unknown_args):
    command.run(known_args, unknown_args)


def set_zephyr_base(args):
    '''Ensure ZEPHYR_BASE is set, emitting warnings if that's not
    possible, or if the user is pointing it somewhere different than
    what the manifest expects.'''
    zb_env = os.environ.get('ZEPHYR_BASE')

    if args.zephyr_base:
        # The command line --zephyr-base takes precedence over
        # everything else.
        zb = os.path.abspath(args.zephyr_base)
        zb_origin = 'command line'
    else:
        # If the user doesn't specify it concretely, use the project
        # with path 'zephyr' if that exists, or the ZEPHYR_BASE value
        # in the calling environment.
        #
        # At some point, we need a more flexible way to set environment
        # variables based on manifest contents, but this is good enough
        # to get started with and to ask for wider testing.
        try:
            manifest = Manifest.from_file()
        except MalformedConfig as e:
            log.die('Parsing of manifest file failed during command',
                    args.command, ':', *e.args)
        for project in manifest.projects:
            if project.path == 'zephyr':
                zb = project.abspath
                zb_origin = 'manifest file {}'.format(manifest.path)
                break
        else:
            if zb_env is None:
                log.wrn('no --zephyr-base given, ZEPHYR_BASE is unset,',
                        'and no manifest project has path "zephyr"')
                zb = None
                zb_origin = None
            else:
                zb = zb_env
                zb_origin = 'environment'

    if zb_env and os.path.abspath(zb) != os.path.abspath(zb_env):
        # The environment ZEPHYR_BASE takes precedence over either the
        # command line or the manifest, but in normal multi-repo
        # operation we shouldn't expect to need to set ZEPHYR_BASE to
        # point to some random place. In practice, this is probably
        # happening because zephyr-env.sh/cmd was run in some other
        # zephyr installation, and the user forgot about that.
        log.wrn('ZEPHYR_BASE={}'.format(zb_env),
                'in the calling environment, but has been set to',
                zb, 'instead by the', zb_origin)

    os.environ['ZEPHYR_BASE'] = zb

    log.dbg('ZEPHYR_BASE={} (origin: {})'.format(zb, zb_origin))


def print_version_info():
    # The bootstrapper will print its own version, as well as that of
    # the west repository itself, then exit. So if this file is being
    # asked to print the version, it's because it's being run
    # directly, and not via the bootstrapper.
    #
    # Rather than play tricks like invoking "pip show west" (which
    # assumes the bootstrapper was installed via pip, the common but
    # not universal case), refuse the temptation to make guesses and
    # print an honest answer.
    log.inf('West bootstrapper version: N/A, not run via bootstrapper')

    # The running west installation.
    if IN_MULTIREPO_INSTALL:
        try:
            desc = check_output(['git', 'describe', '--tags'],
                                stderr=DEVNULL,
                                cwd=os.path.dirname(__file__))
            west_version = desc.decode(sys.getdefaultencoding()).strip()
        except CalledProcessError:
            west_version = 'unknown'
    else:
        west_version = 'N/A, monorepo installation'
    west_src_west = os.path.dirname(__file__)
    print('West repository version: {} ({})'.
          format(west_version,
                 os.path.dirname(os.path.dirname(west_src_west))))


def parse_args(argv):
    # The prog='west' override avoids the absolute path of the main.py script
    # showing up when West is run via the wrapper
    west_parser = WestArgumentParser(
        prog='west', description='The Zephyr RTOS meta-tool.',
        epilog='Run "west <command> -h" for help on each command.',
        add_help=False)

    # Remember to update scripts/west-completion.bash if you add or remove
    # flags

    west_parser.add_argument('-h', '--help', action=WestHelpAction,
                             help='show this help message and exit')

    west_parser.add_argument('-z', '--zephyr-base', default=None,
                             help='''Override the Zephyr base directory. The
                             default is the manifest project with path
                             "zephyr".''')

    west_parser.add_argument('-v', '--verbose', default=0, action='count',
                             help='''Display verbose output. May be given
                             multiple times to increase verbosity.''')

    west_parser.add_argument('-V', '--version', action='store_true',
                             help='print the program version and exit')

    subparser_gen = west_parser.add_subparsers(metavar='<command>',
                                               dest='command')

    for command in itertools.chain(*COMMANDS.values()):
        parser = command.add_parser(subparser_gen)
        parser.set_defaults(handler=partial(command_handler, command))

    args, unknown = west_parser.parse_known_args(args=argv)

    if args.version:
        print_version_info()
        sys.exit(0)

    # Set up logging verbosity before doing anything else, so
    # e.g. verbose messages related to argument handling errors
    # work properly.
    log.set_verbosity(args.verbose)

    if IN_MULTIREPO_INSTALL:
        set_zephyr_base(args)

    if 'handler' not in args:
        west_parser.print_help(file=sys.stderr, top_level=True)
        sys.exit(1)

    return args, unknown


def main(argv=None):
    # Makes ANSI color escapes work on Windows, and strips them when
    # stdout/stderr isn't a terminal
    colorama.init()

    if IN_MULTIREPO_INSTALL:
        # Read the configuration files
        config.read_config()

    if argv is None:
        argv = sys.argv[1:]
    args, unknown = parse_args(argv)

    for_stack_trace = 'run as "west -v ... {} ..." for a stack trace'.format(
        args.command)
    try:
        args.handler(args, unknown)
    except WestUpdated:
        # West has been automatically updated. Restart ourselves to run the
        # latest version, with the same arguments that we were given.
        os.execv(sys.executable, [sys.executable] + argv)
    except KeyboardInterrupt:
        sys.exit(0)
    except CalledProcessError as cpe:
        log.err('command exited with status {}: {}'.format(
            cpe.args[0], quote_sh_list(cpe.args[1])))
        if args.verbose:
            raise
        else:
            log.inf(for_stack_trace)
    except CommandContextError as cce:
        log.die('command', args.command, 'cannot be run in this context:',
                *cce.args)


if __name__ == "__main__":
    main()
