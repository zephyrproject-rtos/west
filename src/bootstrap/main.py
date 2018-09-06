# Copyright 2018 Open Source Foundries Limited.
#
# SPDX-License-Identifier: Apache-2.0

'''West's bootstrap/wrapper script.
'''

import argparse
import os
from os.path import join
import importlib
import platform
import subprocess
import sys

if sys.version_info < (3,):
    sys.exit('fatal error: you are running Python 2')

MANIFEST = 'manifest'
MANIFEST_DEFAULT = 'https://github.com/zephyrproject-rtos/manifest'
MANIFEST_REV_DEFAULT = 'master'
WEST = 'west'
WEST_DEFAULT = 'https://github.com/zephyrproject-rtos/west'
WEST_REV_DEFAULT = 'master'
# NB: on Windows, this will be hidden manually, using attrib.
WEST_DIR = '.west'

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
    if not os.path.isdir(start):
        raise WestNotFound()

    if WEST_DIR in os.listdir(start):
        return start

    dirname = os.path.dirname(start)
    if start == dirname:
        # / on POSIX, top level drive name on Windows.
        raise WestNotFound()
    else:
        return find_west_topdir(dirname)


def make_topdir_sentinel(directory):
    '''Make the hidden sentinel that marks a West installation.

    This is similar to .git or .repo directories: it marks the top
    level directory of the installation. This allows runtime building
    other paths, etc., which is how this script delegates to the
    cloned west in wrapper mode (i.e. after 'west init' is run and the
    "real" west repository has been cloned).'''
    # The sentinel is a directory in case we ever need to stash
    # anything inside. It could just as well be an empty file for now,
    # but this is more future-proof.
    sentinel = join(directory, WEST_DIR)
    os.mkdir(sentinel)
    # On Windows, we have to manually mark a file as hidden.
    if platform.system() == 'Windows':
        subprocess.check_call(['attrib', '+H', sentinel])


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

    make_topdir_sentinel(directory)
    clone(args.manifest_url, args.manifest_rev, join(directory, MANIFEST))
    clone(args.west_url, args.west_rev, join(directory, WEST))


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
    sys.path.insert(1, join(topdir, WEST, 'src'))
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
