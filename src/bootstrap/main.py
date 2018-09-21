# Copyright 2018 Open Source Foundries Limited.
#
# SPDX-License-Identifier: Apache-2.0

'''West's bootstrap/wrapper script.
'''

import argparse
import os
import importlib
import platform
import subprocess
import sys

if sys.version_info < (3,):
    sys.exit('fatal error: you are running Python 2')


#
# Special files and directories in the west installation.
#
# These are given variable names for clarity, but they can't be
# changed without propagating the changes into west itself.
#

# Top-level west directory, containing west itself and the manifest.
WEST_DIR = 'west'
# Subdirectory to check out the west source repository into.
WEST = 'west'
# Default west repository URL.
WEST_DEFAULT = 'https://github.com/zephyrproject-rtos/west'
# Default revision to check out of the west repository.
WEST_REV_DEFAULT = 'master'
# File inside of WEST_DIR which marks it as the top level of the
# Zephyr project installation.
#
# (The WEST_DIR name is not distinct enough to use when searching for
# the top level; other directories named "west" may exist elsewhere,
# e.g. zephyr/doc/west.)
WEST_TOPDIR = '.west_topdir'

# Manifest repository directory under WEST_DIR.
MANIFEST = 'manifest'
# Default manifest repository URL.
MANIFEST_DEFAULT = 'https://github.com/zephyrproject-rtos/manifest'
# Default revision to check out of the manifest repository.
MANIFEST_REV_DEFAULT = 'master'


#
# Helpers shared between init and wrapper mode
#


class WestError(RuntimeError):
    pass


class WestNotFound(WestError):
    '''Neither the current directory nor any parent has a West installation.'''


def find_west_topdir(start):
    '''Find the top-level installation directory, starting at ``start``.

    If none is found, raises WestNotFound.'''
    # If you change this function, make sure to update west.util.west_topdir().
    def is_west_dir(d):
        return os.path.isdir(d) and '.west_topdir' in os.listdir(d)

    cur_dir = start

    while True:
        if is_west_dir(os.path.join(cur_dir, 'west')):
            return cur_dir

        parent_dir = os.path.dirname(cur_dir)
        if cur_dir == parent_dir:
            # At the root
            raise WestNotFound('Could not find a West installation '
                               'in this or any parent directory')
        cur_dir = parent_dir


def clone(url, rev, dest):
    def repository_type(url):
        if url.startswith(('http:', 'https:', 'git:', 'git+ssh:', 'file:')):
            return 'GIT'
        else:
            return 'UNKNOWN'

    if os.path.exists(dest):
        msg = 'refusing to clone into existing location {}'.format(dest)
        raise WestError(msg)

    if repository_type(url) == 'GIT':
        subprocess.check_call(['git', 'clone', url, '-b', rev, dest])
    else:
        raise WestError('Unknown URL scheme for repository: {}'.format(url))


#
# west init
#


def init(argv):
    '''Command line handler for ``west init`` invocations.

    This exits the program with a nonzero exit code if fatal errors occur.'''
    init_parser = argparse.ArgumentParser(
        prog='west init',
        description='Bootstrap initialize a Zephyr installation')
    init_parser.add_argument(
        '-b', '--base-url',
        help='''Base URL for both 'manifest' and 'zephyr' repositories; cannot
        be given if either -u or -w are''')
    init_parser.add_argument(
        '-u', '--manifest-url',
        help='Zephyr manifest fetch URL, default ' + MANIFEST_DEFAULT)
    init_parser.add_argument(
        '--mr', '--manifest-rev', default=MANIFEST_REV_DEFAULT,
        dest='manifest_rev',
        help='Manifest revision to fetch, default ' + MANIFEST_REV_DEFAULT)
    init_parser.add_argument(
        '-w', '--west-url',
        help='West fetch URL, default ' + WEST_DEFAULT)
    init_parser.add_argument(
        '--wr', '--west-rev', default=WEST_REV_DEFAULT, dest='west_rev',
        help='West revision to fetch, default ' + WEST_REV_DEFAULT)
    init_parser.add_argument(
        'directory', nargs='?', default=None,
        help='Initializes in this directory, creating it if necessary')

    args = init_parser.parse_args(args=argv)
    directory = args.directory or os.getcwd()

    if args.base_url:
        if args.manifest_url or args.west_url:
            sys.exit('fatal error: -b is incompatible with -u and -w')
        args.manifest_url = args.base_url.rstrip('/') + '/manifest'
        args.west_url = args.base_url.rstrip('/') + '/west'
    else:
        if not args.manifest_url:
            args.manifest_url = MANIFEST_DEFAULT
        if not args.west_url:
            args.west_url = WEST_DEFAULT

    try:
        topdir = find_west_topdir(directory)
        init_reinit(topdir, args)
    except WestNotFound:
        init_bootstrap(directory, args)


def hide_file(path):
    '''Ensure path is a hidden file.

    On Windows, this uses attrib to hide the file manually.

    On UNIX systems, this just checks that the path's basename begins
    with a period ('.'), for it to be hidden already. It's a fatal
    error if it does not begin with a period in this case.

    On other systems, this just prints a warning.
    '''
    system = platform.system()

    if system == 'Windows':
        subprocess.check_call(['attrib', '+H', path])
    elif os.name == 'posix':  # Try to check for all Unix, not just macOS/Linux
        if not os.path.basename(path).startswith('.'):
            sys.exit("internal error: {} can't be hidden on UNIX".format(path))
    else:
        print("warning: unknown platform {}; {} may not be hidden"
              .format(system, path), file=sys.stderr)


def init_bootstrap(directory, args):
    '''Bootstrap a new manifest + West installation in the given directory.'''
    if not os.path.isdir(directory):
        try:
            print('Initializing in new directory', directory)
            os.makedirs(directory, exist_ok=False)
        except PermissionError:
            sys.exit('Cannot initialize in {}: permission denied'.format(
                directory))
        except FileExistsError:
            sys.exit('Something else created {} concurrently; quitting'.format(
                directory))
        except Exception as e:
            sys.exit("Can't create directory {}: {}".format(
                directory, e.args))
    else:
        print('Initializing in', directory)

    # Clone the west source code and the manifest into west/. Git will create
    # the west/ directory if it does not exist.

    clone(args.west_url, args.west_rev,
          os.path.join(directory, WEST_DIR, WEST))

    clone(args.manifest_url, args.manifest_rev,
          os.path.join(directory, WEST_DIR, MANIFEST))

    # Mark the top level installation.

    with open(os.path.join(directory, WEST_DIR, WEST_TOPDIR), 'w') as f:
        hide_file(f.name)


def init_reinit(directory, args):
    # TODO
    sys.exit('Re-initializing an existing installation is not yet supported.')


#
# Wrap a West command
#


def wrap(argv):
    start = os.getcwd()
    try:
        topdir = find_west_topdir(start)
    except WestNotFound:
        sys.exit('Error: not a Zephyr directory (or any parent): {}\n'
                 'Use "west init" to install Zephyr here'.format(start))
    # Put the top-level west source directory at the highest priority
    # except for the script directory / current working directory.
    sys.path.insert(1, os.path.join(topdir, WEST, 'src'))
    main_module = importlib.import_module('west.main')
    main_module.main(argv=argv)


#
# Main entry point
#


def main(wrap_argv=None):
    '''Entry point to the wrapper script.'''
    if wrap_argv is None:
        wrap_argv = sys.argv[1:]

    if not wrap_argv or wrap_argv[0] != 'init':
        wrap(wrap_argv)
    else:
        init(wrap_argv[1:])
        sys.exit(0)


if __name__ == '__main__':
    main()
