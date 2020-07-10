#!/usr/bin/env python3

# Copyright 2018 Open Source Foundries Limited.
# Copyright 2019 Foundries.io Limited.
# Copyright (c) 2019, Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

'''Zephyr RTOS meta-tool (west) main module

Nothing in here is public API.
'''

import argparse
from collections import OrderedDict
import colorama
from io import StringIO
import logging
import os
from pathlib import Path, PurePath
import shutil
import sys
from subprocess import CalledProcessError
import tempfile
import textwrap
import traceback

from west import log
from west import configuration as config
from west.commands import WestCommand, extension_commands, \
    CommandError, ExtensionCommandError
from west.app.project import List, ManifestCommand, Diff, Status, \
    SelfUpdate, ForAll, Init, Update, Topdir
from west.app.config import Config
from west.manifest import Manifest, MalformedConfig, MalformedManifest, \
    ManifestVersionError, ManifestImportFailed, _ManifestImportDepth, \
    ManifestProject, MANIFEST_REV_BRANCH
from west.util import quote_sh_list, west_topdir, WestNotFound
from west.version import __version__

class WestApp:
    # The west 'application' object.
    #
    # There's enough state to keep track of when building the final
    # WestCommand we want to run that it's convenient to have an
    # object to stash it all in.
    #
    # We could use globals, but that would make it harder to white-box
    # test multiple main() invocations from the same Python process,
    # which is a goal. See #149.

    def __init__(self):
        self.topdir = None          # west_topdir()
        self.manifest = None        # west.manifest.Manifest
        self.mle = None             # saved exception if load_manifest() fails
        self.builtins = {}          # command name -> WestCommand instance
        self.extensions = {}        # extension command name -> spec
        self.builtin_groups = OrderedDict()    # group name -> WestCommand list
        self.extension_groups = OrderedDict()  # project path -> ext spec list
        self.west_parser = None     # a WestArgumentParser
        self.subparser_gen = None   # an add_subparsers() return value

        for group, classes in BUILTIN_COMMAND_GROUPS.items():
            lst = [cls() for cls in classes]
            self.builtins.update({command.name: command for command in lst})
            self.builtin_groups[group] = lst

        # Give the help instance a back-pointer up here.
        #
        # A dirty layering violation, but it does need this data:
        #
        # - 'west help <command>' needs to call into <command>'s
        #   parser's print_help()
        # - 'west help' needs self.west_parser, which
        #   the argparse API does not give us a future-proof way
        #   to access from the Help object's parser attribute,
        #   which comes from subparser_gen.
        self.builtins['help'].app = self

    def run(self, argv):
        # Run the command-line application with argument list 'argv'.

        # See if we're in a workspace. It's fine if we're not.
        # Note that this falls back on searching from ZEPHYR_BASE
        # if the current directory isn't inside a west workspace.
        try:
            self.topdir = west_topdir()
        except WestNotFound:
            pass

        # Read the configuration files. We need this to get
        # manifest.path to parse the manifest, etc.
        #
        # TODO: re-work to avoid global state (#149).
        config.read_config(topdir=self.topdir)

        # Set self.manifest and self.extensions.
        self.load_manifest()
        self.load_extension_specs()

        # Set up initial argument parsers. This requires knowing
        # self.extensions, so it can't happen before now.
        self.setup_parsers()

        # OK, we are all set. Run the command.
        self.run_command(argv)

    def load_manifest(self):
        # Try to parse the manifest. We'll save it if that works, so
        # it doesn't have to be re-parsed.

        if not self.topdir:
            return

        try:
            self.manifest = Manifest.from_file(topdir=self.topdir)
        except (ManifestVersionError, MalformedManifest, MalformedConfig,
                FileNotFoundError, ManifestImportFailed) as e:
            # Defer exception handling to WestCommand.run(), which uses
            # handle_builtin_manifest_load_err() to decide what to do.
            #
            # Make sure to update that function if you change the
            # exceptions caught here. Unexpected exceptions should
            # propagate up and fail fast.
            #
            # This might be OK, e.g. if we're running 'west config
            # manifest.path foo' to fix the MalformedConfig error, but
            # there's no way to know until we've parsed the command
            # line arguments.
            if isinstance(e, _ManifestImportDepth):
                log.wrn('recursion depth exceeded during manifest resolution; '
                        'your manifest likely contains an import loop. '
                        'Run "west -v manifest --resolve" to debug.')
            self.mle = e

    def handle_builtin_manifest_load_err(self, args):
        # Deferred handling for expected load_manifest() exceptions.
        # Called before attempting to run a built-in command. (No
        # extension commands can be run, because we learn about them
        # from the manifest itself, which we have failed to load.)

        # A few commands are always safe to run without a manifest.
        # The update command is sometimes safe and sometimes not, but
        # we need to include it in this list because it's the only way
        # to fix a manifest-rev revision in a project which is being
        # imported to point from a bogus manifest to a non-bogus one.
        no_manifest_ok = ['help', 'config', 'topdir', 'init', 'manifest',
                          'update']

        # Handle ManifestVersionError is a special case.
        if isinstance(self.mle, ManifestVersionError):
            if args.command == 'help':
                log.wrn(mve_msg(self.mle, suggest_upgrade=False) +
                        '\n  Cannot get extension command help, ' +
                        "and most commands won't run." +
                        '\n  To silence this warning, upgrade west.')
                return
            elif args.command in ['config', 'topdir']:
                # config and topdir are safe to run, but let's
                # warn the user that most other commands won't be.
                log.wrn(mve_msg(self.mle, suggest_upgrade=False) +
                        "\n  This should work, but most commands won't." +
                        '\n  To silence this warning, upgrade west.')
                return
            elif args.command == 'init':
                # init is fine to run -- it will print its own error,
                # with context about where the workspace was found,
                # and what the user's choices are.
                return
            else:
                assert args.command not in no_manifest_ok
                log.die(mve_msg(self.mle))

        # Other errors generally just fall back on no_manifest_ok.
        def isinst(*args):
            return any(isinstance(self.mle, t) for t in args)

        if args.command not in no_manifest_ok:
            if isinst(MalformedManifest, MalformedConfig):
                log.die('\n  '.join(["can't load west manifest"] +
                                    list(self.mle.args)))
            elif isinst(FileNotFoundError):
                # This should ordinarily only happen when the top
                # level west.yml is not found.
                log.die(f"file not found: {self.mle.filename}")
            elif isinst(_ManifestImportDepth):
                log.die('failed, likely due to manifest import loop')
            elif isinst(ManifestImportFailed):
                if args.command == 'update':
                    return      # that's fine

                p, f = self.mle.project, self.mle.filename
                ctxt = f'  Missing file: "{f}"'
                if not isinstance(p, ManifestProject):
                    # Try to be more helpful by explaining exactly
                    # what west.manifest needs to happen before we can
                    # resolve the missing import.
                    rev = p.revision

                    ctxt += f' from revision "{rev}"\n'
                    ctxt += '  Hint: for this to work:\n'
                    ctxt += f'          - {p.name} must be cloned\n'
                    ctxt += (f'          - its {MANIFEST_REV_BRANCH} ref '
                             'must point to a commit with the missing file\n')
                    ctxt += '        To fix, run:\n'
                    ctxt += '          west update'

                log.die(f'failed manifest import in {p.name_and_path}\n' +
                        ctxt)
            else:
                log.die('internal error:',
                        f'unhandled manifest load exception: {self.mle}')

    def load_extension_specs(self):
        if self.manifest is None:
            # "None" means "extensions could not be determined".
            # Leaving this an empty dict would mean "there are no
            # extensions", which is different.
            self.extensions = None
            return

        path_specs = extension_commands(manifest=self.manifest)
        extension_names = set()

        for path, specs in path_specs.items():
            # Filter out attempts to shadow built-in commands as well as
            # command names which are already used.

            filtered = []
            for spec in specs:
                if spec.name in self.builtins:
                    log.wrn(f'ignoring project {spec.project.name} '
                            f'extension command "{spec.name}"; '
                            'this is a built in command')
                    continue
                if spec.name in extension_names:
                    log.wrn(f'ignoring project {spec.project.name} '
                            f'extension command "{spec.name}"; '
                            f'command "{spec.name}" is '
                            'already defined as extension command')
                    continue

                filtered.append(spec)
                extension_names.add(spec.name)
                self.extensions[spec.name] = spec

            self.extension_groups[path] = filtered

    def setup_parsers(self):
        # Set up and install command-line argument parsers.

        west_parser, subparser_gen = self.make_parsers()

        # Add sub-parsers for the built-in commands.
        for command in self.builtins.values():
            command.add_parser(subparser_gen)

        # Add stub parsers for extensions.
        #
        # These just reserve the names of each extension. The real parser
        # for each extension can't be added until we import the
        # extension's code, which we won't do unless parse_known_args()
        # says to run that extension.
        if self.extensions:
            for path, specs in self.extension_groups.items():
                for spec in specs:
                    subparser_gen.add_parser(spec.name, add_help=False)

        # Save the instance state.
        self.west_parser = west_parser
        self.subparser_gen = subparser_gen

    def make_parsers(self):
        # Make a fresh instance of the top level argument parser
        # and subparser generator, and return them in that order.

        # The prog='west' override avoids the absolute path of the
        # main.py script showing up when West is run via the wrapper
        parser = WestArgumentParser(
            prog='west', description='The Zephyr RTOS meta-tool.',
            epilog='''Run "west help <command>" for help on each <command>.''',
            add_help=False, west_app=self)

        # Remember to update zephyr's west-completion.bash if you add or
        # remove flags. This is currently the only place where shell
        # completion is available.

        parser.add_argument('-h', '--help', action=WestHelpAction, nargs=0,
                            help='get help for west or a command')

        parser.add_argument('-z', '--zephyr-base', default=None,
                            help='''Override the Zephyr base directory. The
                            default is the manifest project with path
                            "zephyr".''')

        parser.add_argument('-v', '--verbose', default=0, action='count',
                            help='''Display verbose output. May be given
                            multiple times to increase verbosity.''')

        parser.add_argument('-V', '--version', action='version',
                            version=f'West version: v{__version__}',
                            help='print the program version and exit')

        subparser_gen = parser.add_subparsers(metavar='<command>',
                                              dest='command')

        return parser, subparser_gen

    def run_command(self, argv):
        # Parse command line arguments and run the WestCommand.
        # If we're running an extension, instantiate it from its
        # spec and re-parse arguments before running.

        args, unknown = self.west_parser.parse_known_args(args=argv)

        # Set up logging verbosity before running the command, so e.g.
        # verbose messages related to argument handling errors work
        # properly.
        log.set_verbosity(args.verbose)
        log.dbg('args namespace:', args, level=log.VERBOSE_EXTREME)

        # If we were run as 'west -h ...' or 'west --help ...',
        # monkeypatch the args namespace so we end up running Help.  The
        # user might have also provided a command. If so, print help about
        # that command.
        if args.help or args.command is None:
            args.command_name = args.command
            args.command = 'help'

        # Finally, run the command.
        try:
            if args.command in self.builtins:
                if self.mle:
                    self.handle_builtin_manifest_load_err(args)

                cmd = self.builtins.get(args.command, self.builtins['help'])
                cmd.run(args, unknown, self.topdir, manifest=self.manifest)
            else:
                self.run_extension(args.command, argv)
        except KeyboardInterrupt:
            sys.exit(0)
        except BrokenPipeError:
            sys.exit(0)
        except CalledProcessError as cpe:
            log.err(f'command exited with status {cpe.returncode}: '
                    f'{quote_sh_list(cpe.cmd)}', fatal=True)
            if args.verbose >= log.VERBOSE_EXTREME:
                log.banner('Traceback (enabled by -vvv):')
                traceback.print_exc()
            sys.exit(cpe.returncode)
        except ExtensionCommandError as ece:
            msg = f"extension command \"{args.command}\" couldn't be run"
            if ece.hint:
                msg += '\n  Hint: ' + ece.hint

            if args.verbose >= log.VERBOSE_EXTREME:
                log.err(msg, fatal=True)
                log.banner('Traceback (enabled by -vvv):')
                traceback.print_exc()
            else:
                tb_file = dump_traceback()
                msg += f'\n  See {tb_file} for a traceback.'
                log.err(msg, fatal=True)
            sys.exit(ece.returncode)
        except CommandError as ce:
            # No need to dump_traceback() here. The command is responsible
            # for logging its own errors.
            sys.exit(ce.returncode)
        except MalformedManifest as mm:
            # We can get here because 'west update' is allowed to run
            # even when an invalid manifest was detected, as a way to
            # try to fix a previous update that left 'manifest-rev'
            # branches pointing at revisions with invalid manifest
            # data in projects that get imported.
            log.die('\n  '.join(str(arg) for arg in mm.args))

    def run_extension(self, name, argv):
        # Check a program invariant. We should never get here
        # unless we were able to parse the manifest. That's where
        # information about extensions is loaded from.
        assert self.manifest is not None and self.mle is None, \
            f'internal error: running extension "{name}" ' \
            f'but got {self.mle}'

        command = self.extensions[name].factory()

        # Our original top level parser and subparser generator have some
        # garbage state that prevents us from registering the 'real'
        # command subparser. Just make new ones.
        west_parser, subparser_gen = self.make_parsers()
        command.add_parser(subparser_gen)

        # Parse arguments again.
        args, unknown = west_parser.parse_known_args(argv)

        # HACK: try to set ZEPHYR_BASE.
        #
        # Currently required by zephyr extensions like "west build".
        #
        # TODO: get rid of this. Instead:
        #
        # - support a WEST_DIR environment variable to specify the
        #   workspace if we're not running under a .west directory
        #   (controversial)
        # - make zephyr extensions that need ZEPHYR_BASE just set it
        #   themselves (easy if above is OK, unnecessary if it isn't)
        set_zephyr_base(args, self.manifest, self.topdir)

        command.run(args, unknown, self.topdir, manifest=self.manifest)

class Help(WestCommand):
    # west help <command> implementation.

    def __init__(self):
        super().__init__('help', 'get help for west or a command',
                         textwrap.dedent('''\
                         With an argument, prints help for that command.
                         Without one, prints top-level help for west.'''),
                         requires_workspace=False)

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(
            self.name, help=self.help, description=self.description,
            formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument('command_name', nargs='?', default=None,
                            help='name of command to get help for')
        return parser

    def do_run(self, args, ignored):
        assert self.app, "Help has no WestApp and can't do its job"
        app = self.app
        name = args.command_name

        if not name:
            app.west_parser.print_help(top_level=True)
        elif name == 'help':
            self.parser.print_help()
        elif name in app.builtins:
            app.builtins[name].parser.print_help()
        elif app.extensions is not None and name in app.extensions:
            # It's fine that we don't handle any errors here. The
            # exception handling block in app.run_command is in a
            # parent stack frame.
            app.run_extension(name, [name, '--help'])
        else:
            log.wrn(f'unknown command "{name}"')
            app.west_parser.print_help(top_level=True)
            if app.mle:
                log.wrn('your manifest could not be loaded, '
                        'which may be causing this issue.\n'
                        '  Try running "west update" or fixing the manifest.')

class WestHelpAction(argparse.Action):

    def __call__(self, parser, namespace, values, option_string=None):
        # Just mark that help was requested.
        namespace.help = True

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
        self.west_app = kwargs.pop('west_app', None)
        super(WestArgumentParser, self).__init__(*args, **kwargs)

    def print_help(self, file=None, top_level=False):
        print(self.format_help(top_level=top_level), end='',
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
            for group, commands in self.west_app.builtin_groups.items():
                if group is None:
                    # Skip hidden commands.
                    continue

                append(group + ':')
                for command in commands:
                    self.format_command(append, command, width)
                append('')

            if self.west_app.extensions is None:
                if not self.west_app.mle:
                    # This only happens when there is an error.
                    # If there are simply no extensions, it's an empty dict.
                    # If the user has already been warned about the error
                    # because it's due to a ManifestVersionError, don't
                    # warn them again.
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
                for path, specs in self.west_app.extension_groups.items():
                    # This may occur in case a project defines commands already
                    # defined, in which case it has been filtered out.
                    if not specs:
                        continue

                    project = specs[0].project  # they're all from this project
                    append('extension commands from project '
                           f'{project.name} (path: {project.path}):')

                    for spec in specs:
                        self.format_extension_spec(append, spec, width)
                    append('')

            if self.epilog:
                append(self.epilog)

            return sio.getvalue()

    def format_west_optional(self, append, wo, width):
        metavar = wo['metavar']
        options = wo['options']
        help = wo.get('help')

        # Join the various options together as a comma-separated list,
        # with the metavar if there is one. That's our "thing".
        if metavar is not None:
            opt_str = '  ' + ', '.join(f'{o} {metavar}' for o in options)
        else:
            opt_str = '  ' + ', '.join(options)

        # Delegate to the generic formatter.
        self.format_thing_and_help(append, opt_str, help, width)

    def format_command(self, append, command, width):
        thing = f'  {command.name}:'
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
        super().add_argument(*args, **kwargs)

    def error(self, message):
        if self.west_app and self.west_app.mle and \
           isinstance(self.west_app.mle, ManifestVersionError):
            log.die(mve_msg(self.west_app.mle))
        super().error(message=message)

def mve_msg(mve, suggest_upgrade=True):
    return '\n  '.join(
        [f'west v{mve.version} or later is required by the manifest',
         f'West version: v{__version__}'] +
        ([f'Manifest file: {mve.file}'] if mve.file else []) +
        (['Please upgrade west and retry.'] if suggest_upgrade else []))

def set_zephyr_base(args, manifest, topdir):
    '''Ensure ZEPHYR_BASE is set
    Order of precedence:
    1) Value given as command line argument
    2) Value from environment setting: ZEPHYR_BASE
    3) Value of zephyr.base setting in west config file
    4) Project in the manifest with name, or path, "zephyr" (will
       be persisted as zephyr.base in the local config if found)

    Order of precedence between 2) and 3) can be changed with the setting
    zephyr.base-prefer.
    zephyr.base-prefer takes the values 'env' and 'configfile'

    If 2) and 3) have different values and zephyr.base-prefer is unset,
    a warning is printed.'''

    if args.zephyr_base:
        # The command line --zephyr-base takes precedence over
        # everything else.
        zb = os.path.abspath(args.zephyr_base)
        zb_origin = 'command line'
    else:
        # If the user doesn't specify it concretely, then use ZEPHYR_BASE
        # from the environment or zephyr.base from west.configuration.
        #
        # (We will configure zephyr.base to the project that has path
        # 'zephyr' as a last resort here.)
        #
        # At some point, we need a more flexible way to set environment
        # variables based on manifest contents, but this is good enough
        # to get started with and to ask for wider testing.
        zb_env = os.environ.get('ZEPHYR_BASE')
        zb_prefer = config.config.get('zephyr', 'base-prefer',
                                      fallback=None)
        rel_zb_config = config.config.get('zephyr', 'base', fallback=None)
        if rel_zb_config is None:
            projects = None
            try:
                projects = manifest.get_projects(['zephyr'])
            except ValueError:
                pass
            if projects:
                zephyr = projects[0]
                config.update_config('zephyr', 'base', zephyr.path)
                rel_zb_config = zephyr.path
        if rel_zb_config is not None:
            zb_config = Path(topdir) / rel_zb_config
        else:
            zb_config = None

        if zb_prefer == 'env' and zb_env is not None:
            zb = zb_env
            zb_origin = 'env'
        elif zb_prefer == 'configfile' and zb_config is not None:
            zb = str(zb_config)
            zb_origin = 'configfile'
        elif zb_env is not None:
            zb = zb_env
            zb_origin = 'env'
            try:
                different = (zb_config and not zb_config.samefile(zb_env))
            except FileNotFoundError:
                different = (zb_config and
                             (PurePath(zb_config)) != PurePath(zb_env))
            if different:
                # The environment ZEPHYR_BASE takes precedence over the config
                # setting, but is different than the zephyr.base config value.
                #
                # Therefore, issue a warning as the user might have
                # run zephyr-env.sh/cmd in some other zephyr
                # workspace and forgotten about it.
                log.wrn(f'ZEPHYR_BASE={zb_env} '
                        f'in the calling environment will be used,\n'
                        f'but the zephyr.base config option in {topdir} '
                        f'is "{rel_zb_config}"\n'
                        f'which implies a different ZEPHYR_BASE={zb_config}\n'
                        f'To disable this warning in the future, execute '
                        f"'west config --global zephyr.base-prefer env'")
        elif zb_config:
            zb = str(zb_config)
            zb_origin = 'configfile'
        else:
            zb = None
            zb_origin = None
            # No --zephyr-base, no ZEPHYR_BASE, and no zephyr.base.
            log.wrn("can't find the zephyr repository\n"
                    '  - no --zephyr-base given\n'
                    '  - ZEPHYR_BASE is unset\n'
                    '  - west config contains no zephyr.base setting\n'
                    '  - no manifest project has name or path "zephyr"\n'
                    '\n'
                    "  If this isn't a Zephyr workspace, you can "
                    "  silence this warning with something like this:\n"
                    '    west config zephyr.base not-using-zephyr')

    if zb is not None:
        os.environ['ZEPHYR_BASE'] = zb
        log.dbg(f'ZEPHYR_BASE={zb} (origin: {zb_origin})')

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

    # Create the WestApp instance and let it run.
    app = WestApp()
    app.run(argv or sys.argv[1:])

# If you add a command here, make sure to think about how it should be
# handled in case of ManifestVersionError or other reason the manifest
# might fail to load (import error, configuration file error, etc.)
BUILTIN_COMMAND_GROUPS = {
    'built-in commands for managing git repositories': [
        Init,
        Update,
        List,
        ManifestCommand,
        Diff,
        Status,
        ForAll,
    ],

    'other built-in commands': [
        Help,
        Config,
        Topdir,
    ],

    # None is for hidden commands we don't want to show to the user.
    None: [SelfUpdate]
}

if __name__ == "__main__":
    main()
