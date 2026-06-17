# Copyright 2018 Open Source Foundries Limited.
#
# SPDX-License-Identifier: Apache-2.0

'''Miscellaneous utilities.'''

import os
import shlex
import textwrap
from collections.abc import Iterable
from pathlib import Path
from typing import Optional

import dotenv

# What west's APIs accept for paths.
#
# Here, os.PathLike objects should return str from their __fspath__
# methods, not bytes. We could try to do something like the approach
# taken in https://github.com/python/mypy/issues/5264 to annotate that
# as os.PathLike[str] if TYPE_CHECKING and plain os.PathLike
# otherwise, but it doesn't seem worth it.
PathType = str | os.PathLike

WEST_DIR = '.west'


def escapes_directory(path: PathType, directory: PathType) -> bool:
    '''Returns True if `path` escapes parent directory `directory`.

    :param path: path to check is inside `directory`
    :param directory: parent directory to check

    Verifies `path` is inside of `directory`, after resolving
    both.'''
    path_resolved = Path(path).resolve()
    dir_resolved = Path(directory).resolve()
    try:
        path_resolved.relative_to(dir_resolved)
        ret = False
    except ValueError:
        ret = True
    return ret


def quote_sh_list(cmd: Iterable[str | os.PathLike]) -> str:
    '''Transform a command from list into shell string form.'''
    return ' '.join(shlex.quote(str(s)) for s in cmd)


def wrap(text: str, indent: str) -> list[str]:
    '''Convenience routine for wrapping text to a consistent indent.'''
    return textwrap.wrap(text, initial_indent=indent, subsequent_indent=indent)


class WestNotFound(RuntimeError):
    '''Neither the current directory nor any parent has a west workspace.'''


def west_dir(start: PathType | None = None) -> str:
    '''Returns the absolute path of the workspace's .west directory.

    Starts the search from the start directory, and goes to its
    parents. If the start directory is not specified, the current
    directory is used.

    Raises WestNotFound if no .west directory is found.
    '''
    return os.path.join(west_topdir(start), WEST_DIR)


class WestEnvFileError(RuntimeError):
    '''And error with the .westenv file.'''


def _parse_westenv_file(file_path: Path) -> Optional[str]:
    '''
    Parses the .westenv file to extract the ZEPHYR_BASE variable.
    Also ensures that no invalid entries exist in that file.

    Returns the ZEPHYR_BASE value of the file, if it exists.
    Raises WestEnvFileError when the file is invalid
    '''
    file_data = dotenv.dotenv_values(file_path)
    unknown_keys = set(file_data.keys()).difference({'ZEPHYR_BASE'})
    if unknown_keys:
        raise WestEnvFileError(f'.westenv file contains invalid keys: {", ".join(unknown_keys)}')

    return file_data.get('ZEPHYR_BASE')


def west_topdir(start: PathType | None = None, fall_back: bool = True) -> str:
    '''
    Like west_dir(), but returns the path to the parent directory of the .west/
    directory instead, where project repositories are stored
    '''
    cur_dir = Path(start or os.getcwd())

    while True:
        if (cur_dir / WEST_DIR).is_dir():
            return os.fspath(cur_dir)

        if fall_back and (files := list(cur_dir.glob('.westenv'))):
            zephyr_base_path = _parse_westenv_file(files[0])
            if zephyr_base_path:
                zephyr_base_path = Path(zephyr_base_path)

                if not zephyr_base_path.is_absolute():
                    zephyr_base_path = Path(cur_dir / zephyr_base_path).resolve()

                return west_topdir(str(zephyr_base_path), fall_back=False)

        parent_dir = cur_dir.parent
        if cur_dir == parent_dir:
            # At the root. Should we fall back?
            if fall_back and os.environ.get('ZEPHYR_BASE'):
                return west_topdir(os.environ['ZEPHYR_BASE'], fall_back=False)
            else:
                raise WestNotFound(
                    'Could not find a west workspace in this or any parent directory'
                )
        cur_dir = parent_dir


def expand_path(path: PathType) -> Path:
    '''Returns an expanded path for the provided path-like argument.'''
    return Path(path).expanduser()
