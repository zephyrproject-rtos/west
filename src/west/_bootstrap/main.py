# Copyright 2018 Open Source Foundries Limited.
# Copyright (c) 2019, Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

'''West's bootstrap/wrapper script.
'''

import argparse
import os
import platform
import pykwalify.core
import subprocess
import sys
import yaml
import shutil

import colorama

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
WEST_DIR = '.west'
# Subdirectory to check out the west source repository into.
WEST = 'west'
# Default west repository URL.
WEST_URL_DEFAULT = 'https://github.com/zephyrproject-rtos/west'
# Default revision to check out of the west repository.
WEST_REV_DEFAULT = 'master'

# Manifest repository directory under WEST_DIR.
MANIFEST = 'manifest'
# Default manifest repository URL.
MANIFEST_URL_DEFAULT = 'https://github.com/zephyrproject-rtos/zephyr'
# Default revision to check out of the manifest repository.
MANIFEST_REV_DEFAULT = 'master'

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "west-schema.yml")

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
    return os.path.join(west_topdir(start, False), WEST_DIR)


def west_topdir(start=None, fall_back=True):
    '''
    Like west_dir(), but returns the path to the parent directory of the west/
    directory instead, where project repositories are stored
    '''
    # If you change this function, make sure to update west.util.west_topdir().

    cur_dir = start or os.getcwd()

    while True:
        if os.path.isdir(os.path.join(cur_dir, WEST_DIR)):
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


def _is_sha(s):
    try:
        int(s, 16)
    except ValueError:
        return False

    return len(s) == 40


def clone(desc, url, rev, dest, exist_ok=False):
    if not exist_ok and os.path.exists(dest):
        raise WestError('refusing to clone into existing location ' + dest)

    _banner('Cloning {} from {}, rev. {} into {}'.format(desc, url, rev, dest))
    local_rev = False
    subprocess.check_call(('git', 'init', dest))
    subprocess.check_call(('git', 'remote', 'add', 'origin', '--', url),
                          cwd=dest)
    is_sha = _is_sha(rev)
    if is_sha:
        # Fetch the ref-space and hope the SHA is contained in that ref-space
        subprocess.check_call(('git', 'fetch', 'origin', '--tags',
                               '--', 'refs/heads/*:refs/remotes/origin/*'),
                              cwd=dest)
    else:
        # Fetch the ref-space similar to git clone plus the ref given by user.
        # Redundancy is ok, as example if user specify 'heads/master'.
        # This allows users to specify: pull/<no>/head for pull requests
        subprocess.check_call(('git', 'fetch', 'origin', '--tags', '--', rev,
                               'refs/heads/*:refs/remotes/origin/*'),
                              cwd=dest)

    try:
        # Using show-ref to determine if rev is available in local repo.
        subprocess.check_call(('git', 'show-ref', '--', rev), cwd=dest)
        local_rev = True
    except subprocess.CalledProcessError:
        pass

    if local_rev or is_sha:
        subprocess.check_call(('git', 'checkout', rev), cwd=dest)
    else:
        subprocess.check_call(('git', 'checkout', 'FETCH_HEAD'), cwd=dest)


def _banner(*args, colorize=True):
    # This approach colorizes any sep= and end= text too, as expected.
    #
    # colorama automatically strips the ANSI escapes when stdout isn't a
    # terminal (by wrapping sys.stdout).
    if colorize:
        print(colorama.Fore.LIGHTGREEN_EX, end='')

    print('=== ', end='')
    print(*args)

    if colorize:
        # The flush=True avoids issues with unrelated output from
        # commands (usually Git) becoming colorized, due to the final
        # attribute reset ANSI escape getting line-buffered
        print(colorama.Style.RESET_ALL, end='', flush=True)


#
# west init
#

def init(argv):
    '''Command line handler for ``west init`` invocations.

    This exits the program with a nonzero exit code if fatal errors occur.'''

    # Remember to update scripts/west-completion.bash if you add or remove
    # flags

    init_parser = argparse.ArgumentParser(
        prog='west init',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='''
Initializes a Zephyr installation. Use "west clone" afterwards to fetch the
sources.

In more detail, does the following:

  1. Clones the manifest repository to a temporary folder, and the west
     repository to .west/west

  2. Creates an initial configuration file .west/config

  3. Continues the initialization process through west.main.main() with 'init'
     as command and '--use-cache' pointing to temporary folder as parameter

As an alternative to manually editing west/config, 'west init' can be rerun on
an already initialized West instance to update configuration settings. Only
explicitly passed configuration values (e.g. --mr MANIFEST_REVISION) are
updated.

Updating the manifest URL or revision via 'west init' automatically runs 'west
update --reset-projects' afterwards to reset all projects to their new manifest
revisions.

Updating the west URL or revision also runs 'west update --reset-west'.

To suppress the reset of the manifest, west, and projects, pass --no-reset.
With --no-reset, only the configuration file will be updated, and you will have
to handle any resetting yourself.
''')
    mutualex_group = init_parser.add_mutually_exclusive_group()
    mutualex_group.add_argument(
        '-m', '--manifest-url',
        help='Manifest repository URL (default: {})'
             .format(MANIFEST_URL_DEFAULT))

    mutualex_group.add_argument(
        '-l', '--local', action='store_true',
        help='''Use an existing local manifest repository directly. The local
             repository will not be modified in any way. Cannot be combined
             with --manifest-url and --manifest-rev''')
    init_parser.add_argument(
        '--mr', '--manifest-rev', dest='manifest_rev',
        help='''Manifest revision to fetch (default: {}). Cannot be combined
             with --local'''
             .format(MANIFEST_REV_DEFAULT))

    init_parser.add_argument(
        'directory', nargs='?', default=None,
        help='''Directory to initialize West in, unless the -l option is
             provided, in which case this is the local path to an existing
             manifest repository. Missing directories will be created
             automatically. (default: current directory)''')

    args = init_parser.parse_args(args=argv)

    try:
        west_dir(args.directory)
        sys.exit('West already initialized. Aborting.')
    except WestNotFound:
        pass

    if args.local:
        if args.manifest_rev is not None:
            sys.exit('west init: error: argument --mr/--manifest-rev: not '
                     'allowed with argument -l/--local')
        initialize_west(args)
    else:
        bootstrap(args)


def initialize_west(args):
    '''Initialize a West installation in existing project.'''
    manifest_dir = os.path.normpath(os.path.abspath(args.directory or
                                                    os.getcwd()))
    directory = os.path.dirname(manifest_dir)

    manifest_file = os.path.join(manifest_dir, 'west.yml')
    if not os.path.exists(manifest_file):
        sys.exit('Cannot initialize in {}\nNo \'west.yml\' found in {}'.format(
            directory, manifest_dir))

    print('Initializing in', directory)
    clone_west(manifest_file, directory)

    # Call `west post-init <options> --local <localdir>` to finish up.
    #
    # Note: main west will try to discover zephyr base and fail, as it is
    # not fully initialized at this time.
    #
    # The real fix is to eliminate post-init, merge the two mains, and
    # have west init be a special case that doesn't set up
    # ZEPHYR_BASE. For now, we assume the original argument (or cwd)
    # is a Zephyr repository, since that's how people are using it in
    # practice.
    os.chdir(directory)
    cmd = ['--zephyr-base', manifest_dir, 'post-init', '--local',
           os.path.abspath(manifest_dir)]
    wrap(cmd)

    _banner('West initialized. Now run "west update" in {}.'.
            format(directory))


def bootstrap(args):
    '''Bootstrap a new manifest + West installation.'''

    manifest_url = args.manifest_url or MANIFEST_URL_DEFAULT
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
    tempdir = os.path.join(os.path.abspath(directory),
                           WEST_DIR, 'tmp')
    try:
        clone('manifest repository', manifest_url, manifest_rev, tempdir,
              exist_ok=True)
        # Parse the manifest and look for a section named "west"
        clone_west(os.path.join(tempdir, 'west.yml'), directory)

        # Call `west post-init <options> --use-cache <tempdir>` to finish up.
        #
        # Note: main west will try to discover zephyr base and fail, as it is
        # not fully initialized at this time.
        # Thus a dummy zephyr_base is provided.
        os.chdir(directory)
        cmd = ['--zephyr-base', directory, 'post-init', '--manifest-url',
               manifest_url, '--use-cache', tempdir]
        wrap(cmd)
    finally:
        shutil.rmtree(tempdir, ignore_errors=True)

    _banner('West initialized. Now run "west update" in {}.'.
            format(directory))


def clone_west(manifest_file, directory):
    west_url = WEST_URL_DEFAULT
    west_rev = WEST_REV_DEFAULT
    west_dir = os.path.join(directory, WEST_DIR, WEST)

    # Parse the manifest and look for a section named "west"
    with open(manifest_file, 'r') as f:
        data = yaml.safe_load(f.read())

    if 'west' in data:
        wdata = data['west']
        try:
            pykwalify.core.Core(
                source_data=wdata,
                schema_files=[_SCHEMA_PATH]
            ).validate()
        except pykwalify.errors.SchemaError as e:
            sys.exit("Error: Failed to parse manifest file '{}': {}"
                     .format(manifest_file, e))

        if 'url' in wdata:
            west_url = wdata['url']
        if 'revision' in wdata:
            west_rev = wdata['revision']

    print("cloning {} at revision {}".format(west_url, west_rev))
    clone('west repository', west_url, west_rev, west_dir)

    # Make sure west has a manifest-rev branch, like any project.
    subprocess.check_call(['git', 'update-ref', 'refs/heads/manifest-rev',
                           'HEAD'],
                          cwd=west_dir)

    # Make sure HEAD is detached to avoid a warning when self-updating west.
    subprocess.check_call(['git', 'checkout', '-q', '--detach'], cwd=west_dir)


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
    printing_help_only = False

    if argv:
        if argv[0] in ('-V', '--version'):
            print('West bootstrapper version: v{} ({})'.
                  format(version.__version__, os.path.dirname(__file__)))
            printing_version = True
        elif len(argv) == 1 and argv[0] in ('-h', '--help'):
            # This only matters if we're called outside of an
            # installation directory. We delegate to the main help if
            # called from within one, because it includes a list of
            # available commands, etc.
            printing_help_only = True

    start = os.getcwd()
    try:
        topdir = west_topdir(start)
    except WestNotFound:
        if printing_version:
            sys.exit(0)         # run outside of an installation directory
        elif printing_help_only:
            # We call print multiple times here and below instead of using
            # \n to be newline agnostic.
            print('To set up a Zephyr installation here, run "west init".')
            print('Run "west init -h" for additional information.')
            sys.exit(0)
        else:
            print('Error: "{}" is not in a west installation.'.
                  format(start), file=sys.stderr)
            print('Things to try:', file=sys.stderr)
            print(' - Set ZEPHYR_BASE to a zephyr repository path in a',
                  'west installation.', file=sys.stderr)
            print(' - Run "west init" to set up an installation here.',
                  file=sys.stderr)
            print(' - Run "west init -h" for additional information.',
                  file=sys.stderr)
            sys.exit(1)

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
            print('West repository version: unknown; no tags were found')
        sys.exit(0)

    # Import the west package from the installation and run its main
    # function with the given command-line arguments.
    #
    # This can't be done as a subprocess: that would break the
    # runners' debug handling for GDB, which needs to block the usual
    # control-C signal handling. GDB uses Ctrl-C to halt the debug
    # target. So we really do need to import west and delegate within
    # this bootstrap process.
    #
    # Put this at position 1 to make sure it comes before random stuff
    # that might be on a developer's PYTHONPATH in the import order.
    sys.path.insert(1, os.path.join(west_git_repo, 'src'))
    import west.main
    west.main.main(argv)


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
