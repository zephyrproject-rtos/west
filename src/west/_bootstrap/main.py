# Copyright 2018 Open Source Foundries Limited.
#
# SPDX-License-Identifier: Apache-2.0

'''West's bootstrap/wrapper script.
'''

import argparse
import configparser
import os
import platform
import subprocess
import sys

import west._bootstrap.version as version

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
WEST_URL_DEFAULT = 'https://github.com/zephyrproject-rtos/west'
# Default revision to check out of the west repository.
WEST_REV_DEFAULT = 'master'
# File inside of WEST_DIR which marks it as the top level of the
# Zephyr project installation.
#
# (The WEST_DIR name is not distinct enough to use when searching for
# the top level; other directories named "west" may exist elsewhere,
# e.g. zephyr/doc/west.)
WEST_MARKER = '.west_topdir'

# Manifest repository directory under WEST_DIR.
MANIFEST = 'manifest'
# Default manifest repository URL.
MANIFEST_URL_DEFAULT = 'https://github.com/zephyrproject-rtos/manifest'
# Default revision to check out of the manifest repository.
MANIFEST_REV_DEFAULT = 'master'


#
# Helpers shared between init and wrapper mode
#


class WestError(RuntimeError):
    pass


class WestNotFound(WestError):
    '''Neither the current directory nor any parent has a West installation.'''


def west_dir(start=None):
    '''
    Returns the path to the west/ directory, searching ``start`` and its
    parents.

    Raises WestNotFound if no west directory is found.
    '''
    return os.path.join(west_topdir(start), WEST_DIR)


def west_topdir(start=None):
    '''
    Like west_dir(), but returns the path to the parent directory of the west/
    directory instead, where project repositories are stored
    '''
    # If you change this function, make sure to update west.util.west_topdir().

    cur_dir = start or os.getcwd()

    while True:
        if os.path.isfile(os.path.join(cur_dir, WEST_DIR, WEST_MARKER)):
            return cur_dir

        parent_dir = os.path.dirname(cur_dir)
        if cur_dir == parent_dir:
            # At the root
            raise WestNotFound('Could not find a West installation '
                               'in this or any parent directory')
        cur_dir = parent_dir


def clone(desc, url, rev, dest):
    if os.path.exists(dest):
        raise WestError('refusing to clone into existing location ' + dest)

    if not url.startswith(('http:', 'https:', 'git:', 'git+shh:', 'file:')):
        raise WestError('Unknown URL scheme for repository: {}'.format(url))

    print('=== Cloning {} from {}, rev. {} ==='.format(desc, url, rev))
    subprocess.check_call(('git', 'clone', '-b', rev, '--', url, dest))


#
# west init
#


def init(argv):
    '''Command line handler for ``west init`` invocations.

    This exits the program with a nonzero exit code if fatal errors occur.'''
    init_parser = argparse.ArgumentParser(
        prog='west init',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=
'''
Initializes a Zephyr installation.

In more detail, does the following:

  1. Clones the manifest repository to west/manifest, and the west repository
     to west/west

  2. Creates a marker file west/{}

  3. Creates an initial configuration file west/config

'west init' can be rerun on an already initialized West instance to update
configuration settings. This is just an alternative to manually editing
west/config. Only explicitly passed configuration values (e.g.
--mr NEW_MANIFEST_REVISION) are updated.

If the URL/revision of the manifest or west repositories is updated, then
'west update' is automatically run afterwards to update the repositories. Note
that this will fail if the new revision can't be fast-forwarded to. In that
case, you will need to e.g. stash away your changes in a separate branch.
'''.format(WEST_MARKER))

    init_parser.add_argument(
        '-b', '--base-url',
        help='''Base URL for the 'manifest' and 'west' repositories. Cannot
             be given if either -m or -w are.''')

    init_parser.add_argument(
        '-m', '--manifest-url',
        help='Manifest repository URL (or remote name) (default: {})'
             .format(MANIFEST_URL_DEFAULT))

    init_parser.add_argument(
        '--mr', '--manifest-rev', dest='manifest_rev',
        help='Manifest revision to fetch (default: {})'
             .format(MANIFEST_REV_DEFAULT))

    init_parser.add_argument(
        '-w', '--west-url',
        help='West repository URL (or remote name) (default: {})'
             .format(WEST_URL_DEFAULT))

    init_parser.add_argument(
        '--wr', '--west-rev', dest='west_rev',
        help='West revision to fetch (default: {})'
             .format(WEST_REV_DEFAULT))

    init_parser.add_argument(
        'directory', nargs='?',
        help='''Directory to initialize West in. Missing directories will be
             created automatically. (default: current directory)''')

    args = init_parser.parse_args(args=argv)

    try:
        reinit(os.path.join(west_dir(args.directory), 'config'), args)
    except WestNotFound:
        bootstrap(args)


def bootstrap(args):
    '''Bootstrap a new manifest + West installation.'''

    if args.base_url:
        if args.west_url or args.manifest_url:
            sys.exit('fatal error: -b is incompatible with -m and -w')
        west_url = args.base_url.rstrip('/') + '/west'
        manifest_url = args.base_url.rstrip('/') + '/manifest'
    else:
        west_url = args.west_url or WEST_URL_DEFAULT
        manifest_url = args.manifest_url or MANIFEST_URL_DEFAULT

    west_rev = args.west_rev or WEST_REV_DEFAULT
    manifest_rev = args.manifest_rev or MANIFEST_REV_DEFAULT

    directory = args.directory or os.getcwd()

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

    clone('west repository', west_url, west_rev,
          os.path.join(directory, WEST_DIR, WEST))

    clone('manifest repository', manifest_url, manifest_rev,
          os.path.join(directory, WEST_DIR, MANIFEST))

    # Create an initial configuration file

    config_path = os.path.join(directory, WEST_DIR, 'config')
    update_conf(config_path, west_url, west_rev, manifest_url, manifest_rev)
    print('=== Initial configuration written to {} ==='.format(config_path))

    # Create a dotfile to mark the installation. Hide it on Windows.

    with open(os.path.join(directory, WEST_DIR, WEST_MARKER), 'w') as f:
        hide_file(f.name)

    print('=== West initialized ===')


def reinit(config_path, args):
    '''
    Reinitialize an existing installation.

    Currently, this is just a shorthand for updating the west/config
    configuration file.
    '''
    if args.base_url:
        if args.west_url or args.manifest_url:
            sys.exit('fatal error: -b is incompatible with -m and -w')
        west_url = args.base_url.rstrip('/') + '/west'
        manifest_url = args.base_url.rstrip('/') + '/manifest'
    else:
        west_url = args.west_url
        manifest_url = args.manifest_url

    if not (west_url or args.west_rev or manifest_url or args.manifest_rev):
        sys.exit('West already initialized. Please pass any settings you '
                 'want to change.')

    update_conf(config_path,
                west_url, args.west_rev, manifest_url, args.manifest_rev)

    print('=== Updated configuration written to {} ==='.format(config_path))

    cmd = ['update']
    if west_url or args.west_rev:
        cmd.append('--update-west')
    if manifest_url or args.manifest_rev:
        cmd.append('--update-manifest')
    print("=== Running 'west {}' to update repositories ==="
          .format(' '.join(cmd)))
    wrap(cmd)


def update_conf(config_path, west_url, west_rev, manifest_url, manifest_rev):
    '''
    Creates or updates the configuration file at 'config_path' with the
    specified values. Values that are None/empty are ignored.
    '''
    config = configparser.ConfigParser()

    # This is a no-op if the file doesn't exist, so no need to check
    config.read(config_path)

    update_key(config, 'west', 'remote', west_url)
    update_key(config, 'west', 'revision', west_rev)
    update_key(config, 'manifest', 'remote', manifest_url)
    update_key(config, 'manifest', 'revision', manifest_rev)

    with open(config_path, 'w') as f:
        config.write(f)


def update_key(config, section, key, value):
    '''
    Updates 'key' in section 'section' in ConfigParser 'config', creating
    'section' if it does not exist.

    If value is None/empty, 'key' is left as-is.
    '''
    if not value:
        return

    if section not in config:
        config[section] = {}

    config[section][key] = value


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


#
# Wrap a West command
#

def append_to_pythonpath(directory):
    pp = os.environ.get('PYTHONPATH')
    os.environ['PYTHONPATH'] = ':'.join(([pp] if pp else []) + [directory])


def wrap(argv):
    printing_version = False

    if argv and argv[0] in ('-V', '--version'):
        print('West bootstrapper version: v{} ({})'.format(version.__version__,
                                                    os.path.dirname(__file__)))
        printing_version = True

    try:
        topdir = west_topdir()
    except WestNotFound:
        if printing_version:
            sys.exit(0)         # run outside of an installation directory
        else:
            sys.exit('Error: not a Zephyr directory (or any parent): {}\n'
                     'Use "west init" to install Zephyr here'
                     .format(os.getcwd()))

    west_git_repo = os.path.join(topdir, WEST_DIR, WEST)
    if printing_version:
        try:
            git_describe = subprocess.check_output(
                ['git', 'describe', '--tags'],
                stderr=subprocess.DEVNULL,
                cwd=west_git_repo).decode(sys.getdefaultencoding()).strip()
            print('West repository version: {} ({})'.format(git_describe,
                                                            west_git_repo))
        except subprocess.CalledProcessError:
            print('West repository verison: unknown; no tags were found')
        sys.exit(0)

    # Replace the wrapper process with the "real" west

    # sys.argv[1:] strips the argv[0] of the wrapper script itself
    argv = ([sys.executable,
             os.path.join(west_git_repo, 'src', 'west', 'main.py')] +
            argv)

    append_to_pythonpath(os.path.join(west_git_repo, 'src'))
    try:
        subprocess.check_call(argv)
    except subprocess.CalledProcessError as e:
        sys.exit(1)


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
