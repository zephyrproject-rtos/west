# Copyright (c) 2018, Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

'''west git commands'''

import collections
import os
import shutil
import subprocess

import pykwalify.core
import yaml

from . import WestCommand
from .. import log


class Sync(WestCommand):
    def __init__(self):
        super().__init__(
            'sync',
            'Clone/update all Git repositories specified in the manifest file')

    def do_add_parser(self, parser_adder):
        return _add_common_git_flags(parser_adder, self)

    def do_run(self, args, user_args):
        projects = _all_projects(args)

        # TODO: Error checking (if _git(...).returncode != 0: ...)

        for project in projects:
            if not os.path.exists(project.path):
                _git_top(project, 'clone -b (branch) (url)/(name) (path)')
            else:
                # Fetch first to make sure the project's branch exists. It
                # might not if 'git clone' was aborted, for example.
                _git(project, 'fetch')

                # TODO: The local branch might not exist if the manifest was
                # updated. Maybe local branches could be stored in a separate
                # namespace...

                # Check out the main branch and update it
                _git(project, 'checkout (branch)')
                _git(project, 'rebase FETCH_HEAD')

                # Switch back to previous branch the user was on
                _git(project, 'checkout -')


class Diff(WestCommand):
    def __init__(self):
        super().__init__(
            'diff',
            "Run 'git diff' for each project. Extra arguments are passed "
            "as-is to 'git diff'.",
            accepts_unknown_args=True)

    def do_add_parser(self, parser_adder):
        return _add_common_git_flags(parser_adder, self)

    def do_run(self, args, user_args):
        for project in _all_projects(args):
            if _check_repo(project):
                # Use paths that are relative to the base directory to show
                # which repo any changes are in
                _git(project, 'diff --src-prefix=(path)/ --dst-prefix=(path)/',
                     extra_args=user_args)


class Status(WestCommand):
    def __init__(self):
        super().__init__(
            'status',
            "Run 'git status' for each project. Extra arguments are passed "
            "as-is to 'git status'.",
            accepts_unknown_args=True)

    def do_add_parser(self, parser_adder):
        return _add_common_git_flags(parser_adder, self)

    def do_run(self, args, user_args):
        for project in _all_projects(args):
            if _check_repo(project):
                _git(project, 'fetch')

                log.inf("=== 'git status' for {} (in {}) ==="
                        .format(project.name, project.path))
                _git(project, 'status', extra_args=user_args)


def _add_common_git_flags(parser_adder, command):
    # Adds common command-line flags for the Git-related commands. The manifest
    # file contains repository information.

    parser = parser_adder.add_parser(
        command.name,
        description=command.description)

    parser.add_argument(
        '-m', '--manifest',
        dest='manifest',
        help='path to manifest file (default: <zephyr-base>/manifest/default.yml)')

    # TODO: Make schema optional?

    parser.add_argument(
        '-s', '--schema',
        dest='schema',
        help='path to pykwalify schema for manifest (default: <zephyr-base>/manifest/default.yml)')

    return parser


# Holds information about a project, taken from the manifest file
Project = collections.namedtuple('Project', 'name url revision path')


def _all_projects(args):
    # Parses the manifest file, returning a list with Project instances for all
    # projects. Also verifies the manifest against a pykwalify schema.

    if (not args.manifest or not args.schema) and not args.zephyr_base:
        log.die('Zephyr base directory not specified (via --zephyr-base or ZEPHYR_BASE)')

    if args.schema:
        schema_filename = args.schema
    else:
        schema_filename = os.path.join(
            args.zephyr_base, 'manifest', 'schema.yml')

    if args.manifest:
        manifest_filename = args.manifest
    else:
        manifest_filename = os.path.join(
            args.zephyr_base, 'manifest', 'manifest.yml')


    # Validate the manifest with pykwalify

    try:
        pykwalify.core.Core(
            source_file=manifest_filename, schema_files=[schema_filename]
        ).validate()
    except pykwalify.errors.SchemaError as e:
        log.die('{} malformed (schema: {}):\n{}'
                .format(manifest_filename, schema_filename, e))


    # Load project information from manifest

    with open(manifest_filename) as f:
        manifest = yaml.safe_load(f)['manifest']

    projects = []

    # mp = manifest project (dictionary with values parsed from the manifest)
    for mp in manifest['projects']:
        # Fill in any missing fields in 'mp' with values from the 'defaults'
        # dictionary
        for key, val in manifest['defaults'].items():
            mp.setdefault(key, val)

        # Add the repository URL to 'mp'
        for remote in manifest['remotes']:
            if remote['name'] == mp['remote']:
                mp['url'] = remote['url']
                break
        else:
            log.die('Remote {} not defined in {}'
                    .format(mp['remote'], manifest_filename))

        # If 'mp' doesn't specify a clone path, the project's name is used
        if 'path' not in mp:
            mp['path'] = mp['name']

        # If 'mp' doesn't specify a branch, 'master' is used
        if 'revision' not in mp:
            mp['revision'] = 'master'

        # Use named tuples to store project information. That gives nicer
        # syntax compared to a dict (project.name instead of project['name'],
        # etc.)
        projects.append(Project(mp['name'], mp['url'], mp['revision'],
                                mp['path']))

    return projects


def _check_repo(project):
    # Returns True if the project's repository exists and looks like a Git
    # repository. Otherwise, returns False and prints a message about the
    # repistory being skipped.

    if not os.path.exists(project.path):
        log.inf("{} is not cloned (to {}). Use 'west sync' to clone it. Skipping."
                .format(project.name, project.path))
        return False

    # The directory exists. Check that it looks like a Git repository too.

    res = _git(project, "rev-parse --is-inside-work-tree", capture_output=True)
    if res.stdout.strip() != "true":
        log.inf("{} (in {}) does not seem to be a Git repository. Skipping."
                .format(project.name, project.path))
        return False

    return True


def _git_top(project, cmd, *, extra_args=(), capture_output=False):
    # Runs a git command in the base directory. See _git_helper() for parameter
    # documentation.
    #
    # Returns a CompletedProcess instance (see below).

    return _git_helper(project, cmd, extra_args, None, capture_output)


def _git(project, cmd, *, extra_args=(), capture_output=False):
    # Runs a git command within a particular project. See _git_helper() for
    # parameter documentation.
    #
    # Returns a CompletedProcess instance (see below).

    return _git_helper(project, cmd, extra_args, project.path, capture_output)


def _git_helper(project, cmd, extra_args, cwd, capture_output):
    # Runs a git command.
    #
    # project: The Project instance for the project, derived from the
    #   manifest file.
    #
    # cmd: String with git arguments. Supports some "(foo)" shorthands. See
    #   below.
    #
    # extra_args: List of additional arguments to pass to the git command
    #   (e.g. from the user).
    #
    # cwd: Directory to switch to first (None = current directory)
    #
    # capture_output: True if output should be captured into the returned
    #   subprocess.CompletedProcess instance instead of being printed.
    #
    # Returns a subprocess.CompletedProcess instance.

    # TODO: Run once somewhere?
    if shutil.which('git') is None:
        log.die('Git is not installed or cannot be found')


    args = [arg.replace('(name)', project.name)
               .replace('(url)', project.url)
               .replace('(path)', project.path)
               .replace('(branch)', project.revision)
            for arg in cmd.split()]

    pipe = subprocess.PIPE if capture_output else None

    # universal_newlines=True indirectly turns on decoding of the output.
    #
    # TODO: The Popen() 'encoding' parameter is Python 3.6+ only, and the
    # encoding from the environment gets used here. That should usually be
    # fine, but maybe this could be manually decoded somehow.
    popen = subprocess.Popen(
        ('git', *args, *extra_args), stdout=pipe, stderr=pipe,
        universal_newlines=True, cwd=cwd)

    stdout, stderr = popen.communicate()

    return CompletedProcess(popen.args, popen.returncode, stdout, stderr)


# subprocess.CompletedProcess-alike, used instead of the real deal for Python
# 3.4 compatibility
CompletedProcess = collections.namedtuple(
    'CompletedProcess', 'args returncode stdout stderr')
