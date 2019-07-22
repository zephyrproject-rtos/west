# Copyright 2018 Open Source Foundries Limited.
#
# SPDX-License-Identifier: Apache-2.0

# Some code (the PyYAML representer for an OrderedDict) is adapted
# from the PyYAML source code, which is MIT Licensed:
#
# SPDX-License-Identifier: MIT
#
# Full license text from the PyYAML repository is reproduced below:
#
# Copyright (c) 2017-2018 Ingy döt Net
# Copyright (c) 2006-2016 Kirill Simonov
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

'''Miscellaneous utilities.
'''

import os
import pathlib
import shlex
import textwrap

import yaml

def canon_path(path):
    '''Returns a canonical version of the path.

    This is currently ``os.path.normcase(os.path.abspath(path))``. The
    path separator is converted to os.sep on platforms where that
    matters (Windows).

    :param path: path whose canonical name to return; need not
                 refer to an existing file.
    '''
    return os.path.normcase(os.path.abspath(path))

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
    '''Returns the absolute path of the installation's .west directory.

    Starts the search from the start directory, and goes to its
    parents. If the start directory is not specified, the current
    directory is used.

    Raises WestNotFound if no .west directory is found.
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


def _represent_ordered_dict(dumper, tag, mapping, flow_style=None):
    # PyYAML representer for ordered dicts. Used internally.

    value = []
    node = yaml.MappingNode(tag, value, flow_style=flow_style)
    if dumper.alias_key is not None:
        dumper.represented_objects[dumper.alias_key] = node
    best_style = True
    if hasattr(mapping, 'items'):
        mapping = list(mapping.items())
        # The only real difference between
        # BaseRepresenter.represent_mapping and this function is that
        # we omit the sort here. Ugh!
    for item_key, item_value in mapping:
        node_key = dumper.represent_data(item_key)
        node_value = dumper.represent_data(item_value)
        if not (isinstance(node_key, yaml.ScalarNode) and
                not node_key.style):
            best_style = False
        if not (isinstance(node_value, yaml.ScalarNode) and
                not node_value.style):
            best_style = False
        value.append((node_key, node_value))
    if flow_style is None:
        if dumper.default_flow_style is not None:
            node.flow_style = dumper.default_flow_style
        else:
            node.flow_style = best_style
    return node
