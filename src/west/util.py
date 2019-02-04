# Copyright 2018 Open Source Foundries Limited.
#
# SPDX-License-Identifier: Apache-2.0

'''Miscellaneous utilities used by west
'''

import os
import pathlib
import shlex
import textwrap


def escapes_directory(path, directory):
    '''Returns True if `path` escapes parent directory `directory`.

    :param path: path to check is inside `directory`
    :param directory: parent directory to check

    Verifies `path` is inside of `directory`, after computing
    normalized real paths for both.'''
    # It turns out not to be easy to implement this without using
    # pathlib.
    p = pathlib.Path(os.path.normcase(os.path.realpath(path)))
    d = pathlib.Path(os.path.normcase(os.path.realpath(directory)))
    try:
        p.relative_to(d)
        ret = False
    except ValueError:
        ret = True
    return ret


def quote_sh_list(cmd):
    '''Transform a command from list into shell string form.'''
    fmt = ' '.join('{}' for _ in cmd)
    args = [shlex.quote(s) for s in cmd]
    return fmt.format(*args)


def wrap(text, indent):
    '''Convenience routine for wrapping text to a consistent indent.'''
    return textwrap.wrap(text, initial_indent=indent,
                         subsequent_indent=indent)


class WestNotFound(RuntimeError):
    '''Neither the current directory nor any parent has a West installation.'''


def west_dir(start=None):
    '''Returns the absolute path of the west/ top level directory.

    Starts the search from the start directory, and goes to its
    parents. If the start directory is not specified, the current
    directory is used.

    Raises WestNotFound if no west top-level directory is found.
    '''
    return os.path.join(west_topdir(start), '.west')


def west_topdir(start=None, fall_back=True):
    '''
    Like west_dir(), but returns the path to the parent directory of the .west/
    directory instead, where project repositories are stored
    '''
    # If you change this function, make sure to update the bootstrap
    # script's find_west_topdir().

    cur_dir = start or os.getcwd()

    while True:
        if os.path.isdir(os.path.join(cur_dir, '.west')):
            return cur_dir

        parent_dir = os.path.dirname(cur_dir)
        if cur_dir == parent_dir:
            # At the root. Should we fall back?
            if fall_back and os.environ.get('ZEPHYR_BASE'):
                return west_topdir(os.environ['ZEPHYR_BASE'],
                                   fall_back=False)
            else:
                raise WestNotFound('Could not find a West installation '
                                   'in this or any parent directory')
        cur_dir = parent_dir
