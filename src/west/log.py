# Copyright 2018 Open Source Foundries Limited.
# Copyright 2019 Foundries.io Limited.
#
# SPDX-License-Identifier: Apache-2.0

'''Provides common methods for printing messages to display to the user.

WestCommand instances should generally use the functions in this
module rather than calling print() directly if possible, as these
respect the ``color.ui`` configuration option and verbosity level.
'''

from west import configuration as config

import colorama
import sys

VERBOSE_NONE = 0
'''Default verbosity level, no dbg() messages printed.'''

VERBOSE_NORMAL = 1
'''Some verbose messages printed.'''

VERBOSE_VERY = 2
'''Very verbose output messages will be printed.'''

VERBOSE_EXTREME = 3
'''Extremely verbose output messages will be printed.'''

VERBOSE = VERBOSE_NONE
'''Global verbosity level. VERBOSE_NONE is the default.'''


def set_verbosity(value):
    '''Set the logging verbosity level.

    :param value: verbosity level to set, e.g. VERBOSE_VERY.
    '''
    global VERBOSE
    VERBOSE = int(value)


def dbg(*args, level=VERBOSE_NORMAL):
    '''Print a verbose debug logging message.

    :param args: sequence of arguments to print.
    :param value: verbosity level to set, e.g. VERBOSE_VERY.

    The message is only printed if level is at least the current
    verbosity level.'''
    if level > VERBOSE:
        return
    print(*args)


def inf(*args, colorize=False):
    '''Print an informational message.

    :param args: sequence of arguments to print.
    :param colorize: If this is True, the configuration option ``color.ui``
                     is undefined or true, and stdout is a terminal, then
                     the message is printed in green.
    '''

    if not _use_colors():
        colorize = False

    # This approach colorizes any sep= and end= text too, as expected.
    #
    # colorama automatically strips the ANSI escapes when stdout isn't a
    # terminal (by wrapping sys.stdout).
    if colorize:
        print(colorama.Fore.LIGHTGREEN_EX, end='')

    print(*args)

    if colorize:
        _reset_colors(sys.stdout)


def wrn(*args):
    '''Print a warning.

    :param args: sequence of arguments to print.

    The message is prefixed with the string ``"WARNING: "``.

    If the configuration option ``color.ui`` is undefined or true and
    stdout is a terminal, then the message is printed in yellow.'''

    if _use_colors():
        print(colorama.Fore.LIGHTYELLOW_EX, end='', file=sys.stderr)

    print('WARNING: ', end='', file=sys.stderr)
    print(*args, file=sys.stderr)

    if _use_colors():
        _reset_colors(sys.stderr)


def err(*args, fatal=False):
    '''Print an error.

    This function does not abort the program. For that, use `die()`.

    :param args: sequence of arguments to print.
    :param fatal: if True, the the message is prefixed with "FATAL ERROR: ";
                  otherwise, "ERROR: " is used.

    If the configuration option ``color.ui`` is undefined or true and
    stdout is a terminal, then the message is printed in red.'''

    if _use_colors():
        print(colorama.Fore.LIGHTRED_EX, end='', file=sys.stderr)

    print('FATAL ERROR: ' if fatal else 'ERROR: ', end='', file=sys.stderr)
    print(*args, file=sys.stderr)

    if _use_colors():
        _reset_colors(sys.stderr)


def die(*args, exit_code=1):
    '''Print a fatal error, and abort the program.

    :param args: sequence of arguments to print.
    :param exit_code: return code the program should use when aborting.

    Equivalent to ``die(*args, fatal=True)``, followed by an attempt to
    abort with the given *exit_code*.'''
    err(*args, fatal=True)
    sys.exit(exit_code)


_COLOR_UI_WARNED = False

def _use_colors():
    # Convenience function for reading the color.ui setting
    try:
        return config.config.getboolean('color', 'ui', fallback=True)
    except ValueError as e:
        global _COLOR_UI_WARNED
        if not _COLOR_UI_WARNED:
            print("WARNING: invalid color.ui value: {}.".format(e),
                  file=sys.stderr)
            print('         To fix, run: "west config color.ui <true|false>"',
                  file=sys.stderr)
            _COLOR_UI_WARNED = True
        return False


def _reset_colors(file):
    # The flush=True avoids issues with unrelated output from commands (usually
    # Git) becoming colorized, due to the final attribute reset ANSI escape
    # getting line-buffered
    print(colorama.Style.RESET_ALL, end='', file=file, flush=True)
