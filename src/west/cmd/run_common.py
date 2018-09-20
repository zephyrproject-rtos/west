# Copyright (c) 2018 Open Source Foundries Limited.
#
# SPDX-License-Identifier: Apache-2.0

'''Common code used by commands which execute runners.
'''

import argparse
from os import getcwd, path
from subprocess import CalledProcessError
import textwrap

from west import cmake, log, util
from west.runner.core import get_runner_cls, ZephyrBinaryRunner, RunnerConfig
from west.cmd.command import CommandContextError

# Context-sensitive help indentation.
# Don't change this, or output from argparse won't match up.
INDENT = ' ' * 2


def add_parser_common(parser_adder, command):
    parser = parser_adder.add_parser(
        command.name,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=command.description)

    parser.add_argument('-H', '--context', action='store_true',
                        help='''Rebuild application and print context-sensitive
                        help; this may be combined with --runner to restrict
                        output to a given runner.''')

    group = parser.add_argument_group(title='General Options')

    group.add_argument('-d', '--build-dir',
                       help='''Build directory to obtain runner information
                       from; default is the current working directory.''')
    group.add_argument('-c', '--cmake-cache', default=cmake.DEFAULT_CACHE,
                       help='''Path to CMake cache file containing runner
                       configuration (this is generated by the Zephyr
                       build system when compiling binaries);
                       default: {}.

                       If this is a relative path, it is assumed relative to
                       the build directory. An absolute path can also be
                       given instead.'''.format(cmake.DEFAULT_CACHE))
    group.add_argument('-r', '--runner',
                       help='''If given, overrides any cached {}
                       runner.'''.format(command.name))
    group.add_argument('--skip-rebuild', action='store_true',
                       help='''If given, do not rebuild the application
                       before running {} commands.'''.format(command.name))

    group = parser.add_argument_group(
        title='Configuration overrides',
        description=textwrap.dedent('''\
        These values usually come from the Zephyr build system itself
        as stored in the CMake cache; providing these options
        overrides those settings.'''))

    # Important:
    #
    # 1. The destination variables of these options must match
    #    the RunnerConfig slots.
    # 2. The default values for all of these must be None.
    #
    # This is how we detect if the user provided them or not when
    # overriding values from the cached configuration.
    group.add_argument('--board-dir',
                       help='Zephyr board directory')
    group.add_argument('--kernel-elf',
                       help='Path to kernel binary in .elf format')
    group.add_argument('--kernel-hex',
                       help='Path to kernel binary in .hex format')
    group.add_argument('--kernel-bin',
                       help='Path to kernel binary in .bin format')
    group.add_argument('--gdb',
                       help='Path to GDB, if applicable')
    group.add_argument('--openocd',
                       help='Path to OpenOCD, if applicable')
    group.add_argument(
        '--openocd-search',
        help='Path to add to OpenOCD search path, if applicable')

    return parser


def desc_common(command_name):
    return textwrap.dedent('''\
    Any options not recognized by this command are passed to the
    back-end {command} runner (run "west {command} --context"
    for help on available runner-specific options).

    If you need to pass an option to a runner which has the
    same name as one recognized by this command, you can
    end argument parsing with a '--', like so:

    west {command} --{command}-arg=value -- --runner-arg=value2
    '''.format(**{'command': command_name}))


def cached_runner_config(build_dir, cache):
    '''Parse the RunnerConfig from a build directory and CMake Cache.'''
    board_dir = cache['ZEPHYR_RUNNER_CONFIG_BOARD_DIR']
    kernel_elf = cache['ZEPHYR_RUNNER_CONFIG_KERNEL_ELF']
    kernel_hex = cache['ZEPHYR_RUNNER_CONFIG_KERNEL_HEX']
    kernel_bin = cache['ZEPHYR_RUNNER_CONFIG_KERNEL_BIN']
    gdb = cache.get('ZEPHYR_RUNNER_CONFIG_GDB')
    openocd = cache.get('ZEPHYR_RUNNER_CONFIG_OPENOCD')
    openocd_search = cache.get('ZEPHYR_RUNNER_CONFIG_OPENOCD_SEARCH')

    return RunnerConfig(build_dir, board_dir,
                        kernel_elf, kernel_hex, kernel_bin,
                        gdb=gdb, openocd=openocd,
                        openocd_search=openocd_search)


def _override_config_from_namespace(cfg, namespace):
    '''Override a RunnerConfig's contents with command-line values.'''
    for var in cfg.__slots__:
        if var in namespace:
            val = getattr(namespace, var)
            if val is not None:
                setattr(cfg, var, val)


def do_run_common(command, args, runner_args, cached_runner_var):
    if args.context:
        _dump_context(command, args, runner_args, cached_runner_var)
        return

    command_name = command.name
    build_dir = args.build_dir or getcwd()

    if not args.skip_rebuild:
        try:
            cmake.run_build(build_dir)
        except CalledProcessError:
            if args.build_dir:
                log.die('cannot run {}, build in {} failed'.format(
                    command_name, args.build_dir))
            else:
                log.die('cannot run {}; no --build-dir given and build in '
                        'current directory {} failed'.format(command_name,
                                                             build_dir))

    # Runner creation, phase 1.
    #
    # Get the default runner name from the cache, allowing a command
    # line override. Get the ZephyrBinaryRunner class by name, and
    # make sure it supports the command.

    cache_file = path.join(build_dir, args.cmake_cache)
    cache = cmake.CMakeCache(cache_file)
    board = cache['CACHED_BOARD']
    available = cache.get_list('ZEPHYR_RUNNERS')
    if not available:
        log.wrn('No cached runners are available in', cache_file)
    runner = args.runner or cache.get(cached_runner_var)

    if runner is None:
        raise CommandContextError(textwrap.dedent("""
        No {} runner available for {}. Please either specify one
        manually, or check your board's documentation for
        alternative instructions.""".format(command_name, board)))

    log.inf('Using runner:', runner)
    if runner not in available:
        log.wrn('Runner {} is not configured for use with {}, '
                'this may not work'.format(runner, board))
    runner_cls = get_runner_cls(runner)
    if command_name not in runner_cls.capabilities().commands:
        log.die('Runner {} does not support command {}'.format(
            runner, command_name))

    # Runner creation, phase 2.
    #
    # At this point, the common options above are already parsed in
    # 'args', and unrecognized arguments are in 'runner_args'.
    #
    # - Pull the RunnerConfig out of the cache
    # - Override cached values with applicable command-line options

    cfg = cached_runner_config(build_dir, cache)
    _override_config_from_namespace(cfg, args)

    # Runner creation, phase 3.
    #
    # - Pull out cached runner arguments, and append command-line
    #   values (which should override the cache)
    # - Construct a runner-specific argument parser to handle cached
    #   values plus overrides given in runner_args
    # - Parse arguments and create runner instance from final
    #   RunnerConfig and parsed arguments.

    cached_runner_args = cache.get_list(
        'ZEPHYR_RUNNER_ARGS_{}'.format(cmake.make_c_identifier(runner)))
    assert isinstance(runner_args, list), runner_args
    # If the user passed -- to force the parent argument parser to stop
    # parsing, it will show up here, and needs to be filtered out.
    runner_args = [arg for arg in runner_args if arg != '--']
    final_runner_args = cached_runner_args + runner_args
    parser = argparse.ArgumentParser(prog=runner)
    runner_cls.add_parser(parser)
    parsed_args, unknown = parser.parse_known_args(args=final_runner_args)
    if unknown:
        raise CommandContextError('Runner', runner,
                                  'received unknown arguments', unknown)
    runner = runner_cls.create(cfg, parsed_args)
    runner.run(command_name)


#
# Context-specific help
#

def _dump_context(command, args, runner_args, cached_runner_var):
    build_dir = args.build_dir or getcwd()

    # If the cache is a file, try to ensure build artifacts are up to
    # date. If that doesn't work, still try to print information on a
    # best-effort basis.
    cache_file = path.abspath(path.join(build_dir, args.cmake_cache))
    cache = None

    if path.isfile(cache_file):
        have_cache_file = True
    else:
        have_cache_file = False
        if args.build_dir:
            msg = textwrap.dedent('''\
            CMake cache {}: no such file or directory, --build-dir {}
            is invalid'''.format(cache_file, args.build_dir))
            log.die('\n'.join(textwrap.wrap(msg, initial_indent='',
                                            subsequent_indent=INDENT,
                                            break_on_hyphens=False)))
        else:
            msg = textwrap.dedent('''\
            No cache file {} found; is this a build directory?
            (Use --build-dir to set one if not, otherwise, output will be
            limited.)'''.format(cache_file))
            log.wrn('\n'.join(textwrap.wrap(msg, initial_indent='',
                                            subsequent_indent=INDENT,
                                            break_on_hyphens=False)))

    if have_cache_file and not args.skip_rebuild:
        try:
            cmake.run_build(build_dir)
        except CalledProcessError:
            msg = 'Failed re-building application; cannot load context. '
            if args.build_dir:
                msg += 'Is {} the right --build-dir?'.format(args.build_dir)
            else:
                msg += textwrap.dedent('''\
                Use --build-dir (-d) to specify a build directory; the default
                is the current directory, {}.'''.format(build_dir))
            log.die('\n'.join(textwrap.wrap(msg, initial_indent='',
                                            subsequent_indent=INDENT,
                                            break_on_hyphens=False)))

    if have_cache_file:
        try:
            cache = cmake.CMakeCache(cache_file)
        except Exception:
            log.die('Cannot load cache {}.'.format(cache_file))

    if cache is None:
        _dump_no_context_info(command, args)
        if not args.runner:
            return

    if args.runner:
        # Just information on one runner was requested.
        _dump_one_runner_info(cache, args, build_dir, INDENT)
        return

    board = cache['CACHED_BOARD']

    all_cls = {cls.name(): cls for cls in ZephyrBinaryRunner.get_runners() if
               command.name in cls.capabilities().commands}
    available = [r for r in cache.get_list('ZEPHYR_RUNNERS') if r in all_cls]
    available_cls = {r: all_cls[r] for r in available if r in all_cls}

    default_runner = cache.get(cached_runner_var)
    cfg = cached_runner_config(build_dir, cache)

    log.inf('All Zephyr runners which support {}:'.format(command.name))
    for line in util.wrap(', '.join(all_cls.keys()), INDENT):
        log.inf(line)
    log.inf('(Not all may work with this build, see available runners below.)')

    if cache is None:
        log.warn('Missing or invalid CMake cache {}; there is no context.',
                 'Use --build-dir to specify the build directory.')
        return

    log.inf('Build directory:', build_dir)
    log.inf('Board:', board)
    log.inf('CMake cache:', cache_file)

    if not available:
        # Bail with a message if no runners are available.
        msg = ('No runners available for {}. '
               'Consult the documentation for instructions on how to run '
               'binaries on this target.').format(board)
        for line in util.wrap(msg, ''):
            log.inf(line)
        return

    log.inf('Available {} runners:'.format(command.name), ', '.join(available))
    log.inf('Additional options for available', command.name, 'runners:')
    for runner in available:
        _dump_runner_opt_help(runner, all_cls[runner])
    log.inf('Default {} runner: {}'.format(command.name, default_runner))
    _dump_runner_config(cfg, '', INDENT)
    log.inf('Runner-specific information:')
    for runner in available:
        log.inf('{}{}:'.format(INDENT, runner))
        _dump_runner_cached_opts(cache, runner, INDENT * 2, INDENT * 3)
        _dump_runner_caps(available_cls[runner], INDENT * 2)

    if len(available) > 1:
        log.inf('(Add -r RUNNER to just print information about one runner.)')


def _dump_no_context_info(command, args):
    all_cls = {cls.name(): cls for cls in ZephyrBinaryRunner.get_runners() if
               command.name in cls.capabilities().commands}
    log.inf('All Zephyr runners which support {}:'.format(command.name))
    for line in util.wrap(', '.join(all_cls.keys()), INDENT):
        log.inf(line)
    if not args.runner:
        log.inf('Add -r RUNNER to print more information about any runner.')


def _dump_one_runner_info(cache, args, build_dir, indent):
    runner = args.runner
    cls = get_runner_cls(runner)

    if cache is None:
        _dump_runner_opt_help(runner, cls)
        _dump_runner_caps(cls, '')
        return

    available = runner in cache.get_list('ZEPHYR_RUNNERS')
    cfg = cached_runner_config(build_dir, cache)

    log.inf('Build directory:', build_dir)
    log.inf('Board:', cache['CACHED_BOARD'])
    log.inf('CMake cache:', cache.cache_file)
    log.inf(runner, 'is available:', 'yes' if available else 'no')
    _dump_runner_opt_help(runner, cls)
    _dump_runner_config(cfg, '', indent)
    if available:
        _dump_runner_cached_opts(cache, runner, '', indent)
    _dump_runner_caps(cls, '')
    if not available:
        log.wrn('Runner', runner, 'is not configured in this build.')


def _dump_runner_caps(cls, base_indent):
    log.inf('{}Capabilities:'.format(base_indent))
    log.inf('{}{}'.format(base_indent + INDENT, cls.capabilities()))


def _dump_runner_opt_help(runner, cls):
    # Construct and print the usage text
    dummy_parser = argparse.ArgumentParser(prog='', add_help=False)
    cls.add_parser(dummy_parser)
    formatter = dummy_parser._get_formatter()
    for group in dummy_parser._action_groups:
        # Break the abstraction to filter out the 'flash', 'debug', etc.
        # TODO: come up with something cleaner (may require changes
        # in the runner core).
        actions = group._group_actions
        if len(actions) == 1 and actions[0].dest == 'command':
            # This is the lone positional argument. Skip it.
            continue
        formatter.start_section('{} option help'.format(runner))
        formatter.add_text(group.description)
        formatter.add_arguments(actions)
        formatter.end_section()
    log.inf(formatter.format_help())


def _dump_runner_config(cfg, initial_indent, subsequent_indent):
    log.inf('{}Cached common runner configuration:'.format(initial_indent))
    for var in cfg.__slots__:
        log.inf('{}--{}={}'.format(subsequent_indent, var, getattr(cfg, var)))


def _dump_runner_cached_opts(cache, runner, initial_indent, subsequent_indent):
    runner_args = _get_runner_args(cache, runner)
    if not runner_args:
        return

    log.inf('{}Cached runner-specific options:'.format(
        initial_indent))
    for arg in runner_args:
        log.inf('{}{}'.format(subsequent_indent, arg))


def _get_runner_args(cache, runner):
    runner_ident = cmake.make_c_identifier(runner)
    args_var = 'ZEPHYR_RUNNER_ARGS_{}'.format(runner_ident)
    return cache.get_list(args_var)
