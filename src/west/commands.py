# Copyright 2018 Open Source Foundries Limited.
# Copyright 2019 Foundries.io Limited.
# Copyright 2022 Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

import argparse
import os
import re
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable
from enum import IntEnum
from typing import NoReturn

# Enable ANSI escape interpretation on Windows 10 1607+ (Aug 2016)
# and later — conhost, Windows Terminal, cmd.exe on Win11,
# PowerShell, VS Code's terminal. Pre-1607 Windows is silently
# not-enabled here; escapes will print literally on those, which
# is acceptable since they're out of mainstream support. No-op on
# non-Windows.
if sys.platform == "win32":
    import ctypes as _ctypes

    _ENABLE_VT = 0x0004
    _k32 = _ctypes.windll.kernel32
    for _handle_id in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
        _h = _k32.GetStdHandle(_handle_id)
        _mode = _ctypes.c_ulong()
        if _k32.GetConsoleMode(_h, _ctypes.byref(_mode)):
            _k32.SetConsoleMode(_h, _mode.value | _ENABLE_VT)
    del _handle_id, _h, _mode, _ENABLE_VT, _k32, _ctypes

from west.configuration import Configuration
from west.manifest import Manifest
from west.util import PathType, quote_sh_list

'''\
This package provides WestCommand, which is the common abstraction all
west commands subclass.

This package also provides support for extension commands.'''

__all__ = ['CommandContextError', 'CommandError', 'WestCommand']


class CommandError(RuntimeError):
    '''Indicates that a command failed.'''

    def __init__(self, returncode=1):
        super().__init__()
        self.returncode = returncode


class CommandContextError(CommandError):
    '''Indicates that a context-dependent command could not be run.'''


class ExtensionCommandError(CommandError):
    '''Exception class indicating an extension command was badly
    defined and could not be created.'''

    def __init__(self, **kwargs):
        self.hint = kwargs.pop('hint', None)
        super().__init__(**kwargs)


def _no_topdir_msg(cwd, name):
    return f'''\
no west workspace found from "{cwd}"; "west {name}" requires one.
Things to try:
  - Change directory to somewhere inside a west workspace and retry.
  - Run "west init" to set up a workspace here.
  - Run "west init -h" for additional information.
'''


class Verbosity(IntEnum):
    '''Verbosity levels for WestCommand instances.'''

    # DO NOT CHANGE THESE VALUES WITHOUT UPDATING main.py!

    #: No output is printed when WestCommand.dbg(), .inf(), etc.
    #: are called.
    QUIET = 0

    #: Only error messages are printed.
    ERR = 1

    #: Only error and warnings are printed.
    WRN = 2

    #: Errors, warnings, and informational messages are printed.
    INF = 3

    #: Like INFO, but WestCommand.dbg(..., level=Verbosity.DBG) output
    #: is also printed.
    DBG = 4

    #: Like DEBUG, but WestCommand.dbg(..., level=Verbosity.DBG_MORE)
    #: output is also printed.
    DBG_MORE = 5

    #: Like DEBUG_MORE, but WestCommand.dbg(..., level=Verbosity.DBG_EXTREME)
    #: output is also printed.
    DBG_EXTREME = 6


# ANSI escape codes for the log-level palette. These are the
# exact strings colorama's Fore.LIGHT{GREEN,YELLOW,RED}_EX and
# Style.RESET_ALL emit (ECMA-48 SGR codes), inlined so the
# project doesn't carry colorama just for the constants table.
_ANSI_INFO = "\x1b[92m"  # bright green
_ANSI_WARN = "\x1b[93m"  # bright yellow
_ANSI_ERR = "\x1b[91m"  # bright red
_ANSI_RESET = "\x1b[0m"

#: Color used (when applicable) for printing with inf().
#: Kept as part of the public API surface for any extension
#: that imports it; equivalent to the inline `_ANSI_INFO`.
INF_COLOR = _ANSI_INFO

#: Color used (when applicable) for printing with wrn().
WRN_COLOR = _ANSI_WARN

#: Color used (when applicable) for printing with err() and die().
ERR_COLOR = _ANSI_ERR

# NO_COLOR is the no-color.org convention honoured by cargo,
# clap, ripgrep, fd, bat. Colorama doesn't honour it; we add it
# as a side benefit of taking ownership of the styling code.
# (Pre-existing behaviour: escapes were emitted whenever
# ``color_ui`` was True, regardless of TTY — colorama was
# imported but never `init()`-ed, so its implicit isatty-strip
# wasn't active. We preserve that; NO_COLOR is the new opt-out.)
_NO_COLOR = "NO_COLOR" in os.environ


def _styled(text: str, ansi: str) -> str:
    '''Return ``text`` wrapped in ``ansi`` + reset, or ``text``
    unchanged if NO_COLOR is set.'''
    if _NO_COLOR:
        return text
    return f"{ansi}{text}{_ANSI_RESET}"


class WestCommand(ABC):
    '''Abstract superclass for a west command.'''

    def __init__(
        self,
        name: str,
        # Required for built-ins but unused by extensions, see
        # https://github.com/zephyrproject-rtos/west/issues/927
        help: str | None = None,
        # We want a description. But this parameter comes after .help which has a
        # default value, so it must have a default too. We check below instead.
        description: str | None = None,
        accepts_unknown_args: bool = False,
        requires_workspace: bool = True,
        verbosity: Verbosity = Verbosity.INF,
    ):
        '''Abstract superclass for a west command.

        Some fields, such as *name*, *help*, and *description*,
        overlap with kwargs that should be passed to the
        ``argparse.ArgumentParser`` added by `WestCommand.add_parser`.
        This wart is by design: ``argparse`` doesn't make many API stability
        guarantees, so this information must be duplicated here for
        future-proofing.

        :param name: the command's name, as entered by the user
        :param help: one-line command help text
        :param description: multi-line command description
        :param accepts_unknown_args: if true, the command can handle
            arbitrary unknown command line arguments in `WestCommand.run`.
            Otherwise, it's a fatal to pass unknown arguments.
        :param requires_workspace: if true, the command requires a
            west workspace to run, and running it outside of one is
            a fatal error.
        :param verbosity: command output verbosity level; can be changed later
        '''
        self.name: str = name
        self.help: str | None = help
        assert description is not None, f"west command '{name}' misses a description field"
        # We have unfortunately allowed blank description strings in the past
        self.description: str = description if description else "MISSING description"
        self.accepts_unknown_args: bool = accepts_unknown_args
        self.requires_workspace = requires_workspace
        self.verbosity = verbosity
        self.topdir: str | None = None
        self.manifest = None
        self.config = None
        self._hooks: list[Callable[[WestCommand], None]] = []

    def add_pre_run_hook(self, hook: Callable[['WestCommand'], None]) -> None:
        '''Add a hook which will be called right before do_run().

        This can be useful to defer work that needs a fully set up
        command to work.

        :param hook: hook to add
        '''
        self._hooks.append(hook)

    def run(
        self,
        args: argparse.Namespace,
        unknown: list[str],
        topdir: PathType,
        manifest: Manifest | None = None,
        config: Configuration | None = None,
    ) -> None:
        '''Run the command.

        This raises `west.commands.CommandContextError` if the command
        cannot be run due to a context mismatch. Other exceptions may
        be raised as well.

        :param args: known arguments parsed via `WestCommand.add_parser`
        :param unknown: unknown arguments present on the command line;
            must be empty unless ``accepts_unknown_args`` is true
        :param topdir: west workspace topdir, accessible as a str via
            ``self.topdir`` from `WestCommand.do_run`
        :param manifest: `west.manifest.Manifest` or ``None``,
            accessible as ``self.manifest`` from `WestCommand.do_run`
        :param config: `west.configuration.Configuration` or ``None``,
            accessible as ``self.config`` from `WestCommand.do_run`
        '''
        self.config = config
        if unknown and not self.accepts_unknown_args:
            self.parser.error(f'unexpected arguments: {unknown}')
        if not topdir and self.requires_workspace:
            self.die(_no_topdir_msg(os.getcwd(), self.name))
        self.topdir = os.fspath(topdir) if topdir else None
        self.manifest = manifest
        for hook in self._hooks:
            hook(self)
        self.do_run(args, unknown)

    def add_parser(self, parser_adder) -> argparse.ArgumentParser:
        '''Registers a parser for this command, and returns it.

        The parser object is stored in a ``parser`` attribute.

        :param parser_adder: The return value of a call to
            ``argparse.ArgumentParser.add_subparsers()``
        '''
        parser = self.do_add_parser(parser_adder)

        if parser is None:
            raise ValueError('do_add_parser did not return a value')

        self.parser = parser
        return self.parser

    #
    # Mandatory subclass hooks
    #

    @abstractmethod
    def do_add_parser(self, parser_adder) -> argparse.ArgumentParser:
        '''Subclass method for registering command line arguments.

        This is called by `WestCommand.add_parser` to register the
        command's options and arguments.

        Subclasses should ``parser_adder.add_parser()`` to add an
        ``ArgumentParser`` for that subcommand, then add any
        arguments. The final parser must be returned.

        :param parser_adder: The return value of a call to
            ``argparse.ArgumentParser.add_subparsers()``
        '''

    @abstractmethod
    def do_run(self, args: argparse.Namespace, unknown: list[str]):
        '''Subclasses must implement; called to run the command.

        :param args: ``argparse.Namespace`` of parsed arguments
        :param unknown: If ``accepts_unknown_args`` is true, a
            sequence of un-parsed argument strings.
        '''

    #
    # Public API, mostly for subclasses.
    #
    # These are meant to be useful to subclasses during their do_run()
    # calls. Using this functionality outside of a WestCommand
    # subclass leads to undefined results.
    #

    @property
    def has_manifest(self) -> bool:
        '''Property which is True if self.manifest is safe to access.'''
        return self._manifest is not None

    def _get_manifest(self) -> Manifest:
        '''Property for the manifest which was passed to run().

        If `do_run` was given a *manifest* kwarg, it is returned.
        Otherwise, a fatal error occurs.
        '''
        if self._manifest is None:
            self.die(
                f"can't run west {self.name};",
                "it requires the manifest, which was not available.",
                'Try "west -vv manifest --validate" to debug.',
            )
        return self._manifest

    def _set_manifest(self, manifest: Manifest | None):
        self._manifest = manifest

    # Do not use @property decorator syntax to avoid a false positive
    # error from mypy by using this workaround:
    # https://github.com/python/mypy/issues/3004#issuecomment-726022329
    manifest = property(_get_manifest, _set_manifest)

    @property
    def has_config(self) -> bool:
        '''Property which is True if self.config is safe to access.'''
        return self._config is not None

    def _get_config(self) -> Configuration:
        '''Property for the config which was passed to run().

        If `do_run` was given a *config* kwarg, it is returned.
        Otherwise, a fatal error occurs.
        '''
        if self._config is None:
            self.die(
                f"can't run west {self.name}; it requires config "
                "variables, which were not available."
            )
        return self._config

    def _set_config(self, config: Configuration | None):
        self._config = config

    config = property(_get_config, _set_config)

    def _log_subproc(self, args, **kwargs):
        self.dbg(
            f"running '{quote_sh_list(args)}' in {kwargs.get('cwd') or os.getcwd()}",
            level=Verbosity.DBG_MORE,
        )

    #
    # Other public methods
    #

    def check_call(self, args, **kwargs):
        '''Runs ``subprocess.check_call(args, **kwargs)`` after
        logging the call at Verbosity.DBG_MORE level.'''

        self._log_subproc(args, **kwargs)
        subprocess.check_call(args, **kwargs)

    def check_output(self, args, **kwargs):
        '''Runs ``subprocess.check_output(args, **kwargs)`` after
        logging the call at Verbosity.DBG_MORE level.'''

        self._log_subproc(args, **kwargs)
        return subprocess.check_output(args, **kwargs)

    def run_subprocess(self, args, **kwargs):
        '''Runs ``subprocess.run(args, **kwargs)`` after logging
        the call at Verbosity.DBG_MORE level.'''

        self._log_subproc(args, **kwargs)
        return subprocess.run(args, errors='backslashreplace', **kwargs)

    def die_if_no_git(self):
        '''Abort if git is not installed on PATH.'''
        if not hasattr(self, '_git'):
            self._git = shutil.which('git')
        if self._git is None:
            self.die("can't find git; install it or ensure it's on your PATH")

    @property
    def git_version_info(self):
        '''Returns git version info as a tuple of ints, usually in
        (major, minor, patch) format, like (2, 29, 1) for git version
        2.29.1.

        Aborts the program if there is no git installed.

        In rare circumstances, you may get a (major, minor) tuple,
        like (2, 29).
        '''
        # It's perfectly safe to compare 2-tuples against 3-tuples.
        # For example, '(2, 29) > (2, 28, 0)' is True.
        # https://docs.python.org/3/reference/expressions.html#comparisons

        if not hasattr(self, '_git_ver'):
            self.die_if_no_git()
            raw_version = self.check_output([self._git, '--version'])
            self._git_ver = self._parse_git_version(raw_version)
            if self._git_ver is None:
                self.die(f"can't get git version from {raw_version!r}")
            self.dbg(f'git version: {self._git_ver}', level=Verbosity.DBG_MORE)
        return self._git_ver

    @staticmethod
    def _parse_git_version(raw_version):
        # Convert the raw 'git --version' output to a tuple.
        #
        # This is a @staticmethod so it can be white box tested.
        #
        # Usually the resulting tuple looks like (major, minor,
        # patch).
        #
        # We get a length 2 tuple in obscure situations like git
        # built from a development source tree created using 'git
        # archive'. (See GIT-VERSION-GEN in the git sources if you're
        # curious about details.)
        #
        # Downstream distributors sometimes tweak the results by
        # adding to the end of 'x.y.z' in the 'git version x.y.z'
        # string, but git itself always prints "git version %s", where
        # the %s is the version.
        #
        # https://github.com/git/git/blob/7e391989789db82983665667013a46eabc6fc570/help.c#L646
        #
        # Some example possibilities:
        #
        # git version 2.25.1
        # git version 2.28.0.windows.1
        # git version 2.24.3 (Apple Git-128)
        # git version 2.29.GIT
        #
        # We handle this by matching the first bit in the
        # whitespace-separated output that has a prefix that looks
        # like a semver.

        match = re.search(
            r'\s(?P<major>\d+)\.(?P<minor>\d+)(\.(?P<patch>\d+))?',
            raw_version.decode(),
            flags=re.ASCII,
        )
        if not match:
            return None

        major, minor, patch = (match.group('major'), match.group('minor'), match.group('patch'))
        version = int(major), int(minor)
        if patch is None:
            return version
        return version + (int(patch),)

    def dbg(self, *args, level: Verbosity = Verbosity.DBG, end: str = '\n'):
        '''Print a verbose debug message.

        The message is only printed if *self.verbosity* is at least *level*.

        :param args: sequence of arguments to print
        :param level: verbosity level of the message
        '''
        if self.verbosity < level:
            return
        print(*args, end=end)

    def inf(self, *args, colorize: bool = False, end: str = '\n'):
        '''Print an informational message.

        The message is only printed if *self.verbosity* is at least INF.

        :param args: sequence of arguments to print.
        :param colorize: If this is True, the configuration option ``color.ui``
                         is undefined or true, and stdout is a terminal, then
                         the message is printed in green.
        '''
        if self.verbosity < Verbosity.INF:
            return

        if not self.color_ui:
            colorize = False

        text = ' '.join(str(a) for a in args)
        if colorize:
            text = _styled(text, _ANSI_INFO)
        print(text, end=end, flush=True)

    def banner(self, *args):
        '''Prints args as a "banner" using inf().

        The args are prefixed with '=== ' and colorized by default.'''
        self.inf('===', *args, colorize=True)

    def small_banner(self, *args):
        '''Prints args as a smaller banner(), i.e. prefixed with '-- ' and
        not colorized.'''
        self.inf('---', *args, colorize=False)

    def wrn(self, *args, end: str = '\n'):
        '''Print a warning.

        The message is only printed if *self.verbosity* is at least WRN.

        The message is prefixed with the string ``"WARNING: "``.

        If the configuration option ``color.ui`` is undefined or true and
        stdout is a terminal, then the message is printed in yellow.

        :param args: sequence of arguments to print.'''

        if self.verbosity < Verbosity.WRN:
            return

        text = 'WARNING: ' + ' '.join(str(a) for a in args)
        if self.color_ui:
            text = _styled(text, _ANSI_WARN)
        print(text, end=end, file=sys.stderr, flush=True)

    def err(self, *args, fatal: bool = False, end: str = '\n'):
        '''Print an error.

        The message is only printed if *self.verbosity* is at least ERR.

        This function does not abort the program. For that, use `die()`.

        If the configuration option ``color.ui`` is undefined or true and
        stdout is a terminal, then the message is printed in red.

        :param args: sequence of arguments to print.
        :param fatal: if True, the the message is prefixed with
                      "FATAL ERROR: "; otherwise, "ERROR: " is used.
        '''

        if self.verbosity < Verbosity.ERR:
            return

        prefix = 'FATAL ERROR: ' if fatal else 'ERROR: '
        text = prefix + ' '.join(str(a) for a in args)
        if self.color_ui:
            text = _styled(text, _ANSI_ERR)
        print(text, end=end, file=sys.stderr, flush=True)

    def die(self, *args, exit_code: int = 1) -> NoReturn:
        '''Print a fatal error using err(), and abort the program.

        :param args: sequence of arguments to print.
        :param exit_code: return code the program should use when aborting.

        Equivalent to ``die(*args, fatal=True)``, followed by an attempt to
        abort with the given *exit_code*.'''
        self.err(*args, fatal=True)
        if self.verbosity >= Verbosity.DBG_EXTREME:
            raise RuntimeError(
                "die with -vvv or more shows a stack trace. exit_code argument is ignored."
            )
        else:
            sys.exit(exit_code)

    @property
    def color_ui(self) -> bool:
        '''Should we colorize output?'''
        return self.config.getboolean('color.ui', default=True) if self.has_config else True
