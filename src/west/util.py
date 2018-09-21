# Copyright 2018 Open Source Foundries Limited.
#
# SPDX-License-Identifier: Apache-2.0

'''Miscellaneous utilities used by west
'''

import os
import shlex
import textwrap


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


def west_dir():
    '''
    Returns the absolute path of the first west/ directory found, searching the
    current directory and its parents.

    Raises WestNotFound if no west/ directory is found.
    '''
    return os.path.join(west_topdir(), 'west')


def west_topdir():
    '''
    Like west_dir(), but returns the path to the parent directory of the west/
    directory instead, where project repositories are stored
    '''
    # If you change this function, make sure to update the bootstrap
    # script's find_west_topdir().

    cur_dir = os.getcwd()

    while True:
        if os.path.isfile(os.path.join(cur_dir, 'west', '.west_topdir')):
            return cur_dir

        parent_dir = os.path.dirname(cur_dir)
        if cur_dir == parent_dir:
            # At the root
            raise WestNotFound('Could not find a West installation '
                               'in this or any parent directory')
        cur_dir = parent_dir
