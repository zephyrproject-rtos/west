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


def west_topdir():
    '''
    Returns the absolute path of the first directory containing a .west/
    directory, searching the current directory and its parents.

    Raises WestNotFound if no .west/ directory is found.
    '''
    search_dir = os.getcwd()

    # While the directory is not the root directory...
    while search_dir != os.path.dirname(search_dir):
        if os.path.isdir(os.path.join(search_dir, '.west')):
            return search_dir
        search_dir = os.path.dirname(search_dir)

    raise WestNotFound('Could not find a West installation (a .west/ '
                       'directory) in this or any parent directory')
