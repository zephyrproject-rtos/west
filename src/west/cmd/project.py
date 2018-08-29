# Copyright (c) 2018, Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

'''West project commands'''

import argparse
import collections
import os
import shutil
import subprocess
import textwrap

import pykwalify.core
import yaml

from . import WestCommand
from .. import log
from .. import util


# Branch that points to the revision specified in the manifest (which might be
# an SHA). Local branches created with 'west branch' are set to track this
# branch.
_MANIFEST_REV_BRANCH = 'manifest-rev'


class ListProjects(WestCommand):
    def __init__(self):
        super().__init__(
            'list-projects',
            _wrap('''
            List projects.

            Prints the path to the manifest file and lists all projects along
            with their clone paths and manifest revisions. Also includes
            information on which projects are currently cloned.
            '''))

    def do_add_parser(self, parser_adder):
        return _add_parser(parser_adder, self)

    def do_run(self, args, user_args):
        log.inf("Manifest path: {}\n".format(_manifest_path(args)))

        for project in _all_projects(args):
            log.inf('{:15} {:30} {:15} {}'.format(
                project.name,
                os.path.join(project.path, ''),  # Add final '/' if missing
                project.revision,
                "(cloned)" if _cloned(project) else "(not cloned)"))


class Fetch(WestCommand):
    def __init__(self):
        super().__init__(
            'fetch',
            _wrap('''
            Clone/fetch projects.

            Fetches upstream changes in each of the specified projects
            (default: all projects). Repositories that do not already exist are
            cloned.

            ''' + _MANIFEST_REV_HELP))

    def do_add_parser(self, parser_adder):
        return _add_parser(parser_adder, self, _project_list_arg)

    def do_run(self, args, user_args):
        for project in _projects(args, listed_must_be_cloned=False):
            log.dbg('fetching:', project, level=log.VERBOSE_VERY)
            _fetch(project)


class Pull(WestCommand):
    def __init__(self):
        super().__init__(
            'pull',
            _wrap('''
            Clone/fetch and rebase projects.

            Fetches upstream changes in each of the specified projects
            (default: all projects) and rebases the checked-out branch (or
            detached HEAD state) on top of '{}', effectively bringing the
            branch up to date. Repositories that do not already exist are
            cloned.

            '''.format(_MANIFEST_REV_BRANCH) + _MANIFEST_REV_HELP))

    def do_add_parser(self, parser_adder):
        return _add_parser(parser_adder, self, _project_list_arg)

    def do_run(self, args, user_args):
        for project in _projects(args, listed_must_be_cloned=False):
            if _fetch(project):
                _rebase(project)


class Rebase(WestCommand):
    def __init__(self):
        super().__init__(
            'rebase',
            _wrap('''
            Rebase projects.

            Rebases the checked-out branch (or detached HEAD) on top of '{}' in
            each of the specified projects (default: all cloned projects),
            effectively bringing the branch up to date.

            '''.format(_MANIFEST_REV_BRANCH) + _MANIFEST_REV_HELP))

    def do_add_parser(self, parser_adder):
        return _add_parser(parser_adder, self, _project_list_arg)

    def do_run(self, args, user_args):
        for project in _projects(args):
            if _cloned(project):
                _rebase(project)


class Branch(WestCommand):
    def __init__(self):
        super().__init__(
            'branch',
            _wrap('''
            Create a branch or list branches, in multiple projects.

            Creates a branch in each of the specified projects (default: all
            cloned projects). The new branches are set to track '{}'.

            With no arguments, lists all local branches along with the
            repositories they appear in.

            '''.format(_MANIFEST_REV_BRANCH) + _MANIFEST_REV_HELP))

    def do_add_parser(self, parser_adder):
        return _add_parser(parser_adder, self, _opt_branch_arg,
                           _project_list_arg)

    def do_run(self, args, user_args):
        # Generator
        projects = (project for project in _projects(args) if _cloned(project))

        if args.branch:
            # Create a branch in the specified projects
            for project in projects:
                _create_branch(project, args.branch)
        else:
            # No arguments. List local branches from all projects along with
            # the projects they appear in.

            branch2projs = collections.defaultdict(list)
            for project in projects:
                for branch in _branches(project):
                    branch2projs[branch].append(project.name)

            for branch, projs in sorted(branch2projs.items()):
                log.inf('{:18} {}'.format(branch, ", ".join(projs)))


class Checkout(WestCommand):
    def __init__(self):
        super().__init__(
            'checkout',
            _wrap('''
            Check out topic branch.

            Checks out the specified branch in each of the specified projects
            (default: all cloned projects). Projects that do not have the
            branch are left alone.
            '''))

    def do_add_parser(self, parser_adder):
        return _add_parser(parser_adder, self, _b_flag, _branch_arg,
                           _project_list_arg)

    def do_run(self, args, user_args):
        branch_exists = False

        for project in _projects(args):
            if _cloned(project):
                if args.create_branch:
                    _create_branch(project, args.branch)
                    _checkout(project, args.branch)
                    branch_exists = True
                elif _has_branch(project, args.branch):
                    _checkout(project, args.branch)
                    branch_exists = True

        if not branch_exists:
            msg = 'No branch {} exists in any '.format(args.branch)
            if args.projects:
                log.die(msg + 'of the listed projects')
            else:
                log.die(msg + 'cloned project')


class Diff(WestCommand):
    def __init__(self):
        super().__init__(
            'diff',
            _wrap('''
            'git diff' projects.

            Runs 'git diff' for each of the specified projects (default: all
            cloned projects).

            Extra arguments are passed as-is to 'git diff'.
            '''),
            accepts_unknown_args=True)

    def do_add_parser(self, parser_adder):
        return _add_parser(parser_adder, self, _project_list_arg)

    def do_run(self, args, user_args):
        for project in _projects(args):
            if _cloned(project):
                # Use paths that are relative to the base directory to make it
                # easier to see where the changes are
                _git(project, 'diff --src-prefix=(path)/ --dst-prefix=(path)/',
                     extra_args=user_args)


class Status(WestCommand):
    def __init__(self):
        super().__init__(
            'status',
            _wrap('''
            Runs 'git status' for each of the specified projects (default: all
            cloned projects). Extra arguments are passed as-is to 'git status'.
            '''),
            accepts_unknown_args=True)

    def do_add_parser(self, parser_adder):
        return _add_parser(parser_adder, self, _project_list_arg)

    def do_run(self, args, user_args):
        for project in _projects(args):
            if _cloned(project):
                _inf(project, 'status of (name-and-path)')
                _git(project, 'status', extra_args=user_args)


class ForAll(WestCommand):
    def __init__(self):
        super().__init__(
            'forall',
            _wrap('''
            Runs a shell (Linux) or batch (Windows) command within the
            repository of each of the specified projects (default: all cloned
            projects). Note that you have to quote the command if it consists
            of more than one word, to prevent the shell you use to run 'west'
            from splitting it up.

            Since the command is run through the shell, you can use wildcards
            and the like.

            For example, the following command will list the contents of
            proj-1's and proj-2's repositories on Linux, in long form:

              west forall -c 'ls -l' proj-1 proj-2
            '''))

    def do_add_parser(self, parser_adder):
        return _add_parser(parser_adder, self, _command_arg, _project_list_arg)

    def do_run(self, args, user_args):
        for project in _projects(args):
            if _cloned(project):
                _inf(project, "Running '{}' in (name-and-path)"
                              .format(args.command))
                subprocess.Popen(
                    args.command, shell=True, cwd=project.abspath
                ).wait()


def _add_parser(parser_adder, cmd, *extra_args):
    # Adds and returns a subparser for the project-related WestCommand 'cmd'.
    # All of these commands (currently) take the manifest path flag, so it's
    # hardcoded here.

    return parser_adder.add_parser(
        cmd.name,
        description=cmd.description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=(_manifest_arg,) + extra_args)


def _wrap(s):
    # Wraps help texts for commands. Some of them have variable length (due to
    # _MANIFEST_REV_BRANCH), so just a textwrap.dedent() can look a bit wonky.

    # [1:] gets rid of the initial newline. It's turned into a space by
    # textwrap.fill() otherwise.
    paragraphs = textwrap.dedent(s[1:]).split("\n\n")

    return "\n\n".join(textwrap.fill(paragraph) for paragraph in paragraphs)


_MANIFEST_REV_HELP = """
The '{}' branch points to the revision that the manifest specified for the
project as of the most recent 'west fetch'/'west pull'.
""".format(_MANIFEST_REV_BRANCH)[1:].replace("\n", " ")


def _arg(*args, **kwargs):
    # Helper for creating a new argument parser for a single argument,
    # later passed in parents= to add_parser()

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(*args, **kwargs)
    return parser


# Common manifest file argument
_manifest_arg = _arg(
    '-m', '--manifest',
    help='path to manifest file (default: <west-topdir>/manifest/default.yml)')

# Optional -b flag for 'west checkout'
_b_flag = _arg(
    '-b',
    dest='create_branch',
    action='store_true',
    help='create the branch before checking it out')

# Optional branch argument
_opt_branch_arg = _arg('branch', nargs='?', metavar='BRANCH_NAME')

# Mandatory branch argument
_branch_arg = _arg('branch', metavar='BRANCH_NAME')

# Command flag, for 'forall'
_command_arg = _arg(
    '-c',
    dest='command',
    metavar='COMMAND',
    required=True)

# Common project list argument
_project_list_arg = _arg('projects', metavar='PROJECT', nargs='*')


# Holds information about a project, taken from the manifest file
Project = collections.namedtuple(
    'Project',
    'name url revision path abspath clone_depth')


def _projects(args, listed_must_be_cloned=True):
    # Returns a list of project instances for the projects requested in 'args'
    # (the command-line arguments), in the same order that they were listed by
    # the user. If args.projects is empty, no projects were listed, and all
    # projects will be returned. If a non-existent project was listed by the
    # user, an error is raised.
    #
    # Before the manifest is parsed, it is validated agains a pykwalify schema.
    # An error is raised on validation errors.
    #
    # listed_must_be_cloned (default: True):
    #   If True, an error is raised if an uncloned project was listed (or if a
    #   listed project's directory doesn't look like a Git repository). This
    #   only applies to projects listed explicitly on the command line.

    projects = _all_projects(args)

    if not args.projects:
        # No projects specified. Return all projects.
        return projects

    # Got a list of projects on the command line. First, check that they exist
    # in the manifest.

    project_names = [project.name for project in projects]
    nonexistent = set(args.projects) - set(project_names)
    if nonexistent:
        log.die('Unknown project{} {} (available projects: {})'
                .format('s' if len(nonexistent) > 1 else '',
                        ', '.join(nonexistent),
                        ', '.join(project_names)))

    # Return the projects in the order they were listed
    res = []
    for name in args.projects:
        for project in projects:
            if project.name == name:
                res.append(project)
                break

    # Check that all listed repositories are cloned, if requested
    if listed_must_be_cloned:
        uncloned = [prj.name for prj in res if not _cloned(prj)]
        if uncloned:
            log.die('The following projects are not cloned: {}. Please clone '
                    "them first (with 'west fetch')."
                    .format(", ".join(uncloned)))

    return res


def _all_projects(args):
    # Parses the manifest file, returning a list of Project instances.
    #
    # Before the manifest is parsed, it is validated against a pykwalify
    # schema. An error is raised on validation errors.

    manifest_path = _manifest_path(args)

    _validate_manifest(manifest_path)

    with open(manifest_path) as f:
        manifest = yaml.safe_load(f)['manifest']

    projects = []

    # mp = manifest project (dictionary with values parsed from the manifest)
    project_defaults = ('remote', 'revision')
    for mp in manifest['projects']:
        # Fill in any missing fields in 'mp' with values from the 'defaults'
        # dictionary
        for key, val in manifest['defaults'].items():
            if key in project_defaults:
                mp.setdefault(key, val)

        # Add the repository URL to 'mp'
        for remote in manifest['remotes']:
            if remote['name'] == mp['remote']:
                mp['url'] = remote['url']
                break
        else:
            log.die('Remote {} not defined in {}'
                    .format(mp['remote'], manifest_path))

        # If no clone path is specified, the project's name is used
        clone_path = mp.get('path', mp['name'])

        # Use named tuples to store project information. That gives nicer
        # syntax compared to a dict (project.name instead of project['name'],
        # etc.)
        projects.append(Project(
            mp['name'],
            mp['url'],
            # If no revision is specified, 'master' is used
            mp.get('revision', 'master'),
            clone_path,
            # Absolute clone path
            os.path.join(util.west_topdir(), clone_path),
            # If no clone depth is specified, we fetch the entire history
            mp.get('clone-depth', None)))

    return projects


def _validate_manifest(manifest_path):
    # Validates the manifest with pykwalify. schema.yml holds the schema.

    schema_path = os.path.join(os.path.dirname(__file__), "schema.yml")

    try:
        pykwalify.core.Core(
            source_file=manifest_path,
            schema_files=[schema_path]
        ).validate()
    except pykwalify.errors.SchemaError as e:
        log.die('{} malformed (schema: {}):\n{}'
                .format(manifest_path, schema_path, e))


def _manifest_path(args):
    # Returns the path to the manifest file. Unless explicitly specified by the
    # user, it defaults to manifest/default.yml within the West top directory.

    if args.manifest:
        return args.manifest

    return os.path.join(util.west_topdir(), 'manifest', 'default.yml')


def _fetch(project):
    # Clone 'project' if it does not exist, and 'git fetch' it otherwise. Also
    # update the 'manifest-rev' branch to point to the revision specified in
    # the manifest.
    #
    # Returns True if the project already existed, and False if it was cloned.

    if os.path.exists(project.abspath):
        existed = True
        _verify_repo(project)
        _inf(project, 'Fetching changes for (name-and-path)')
        _git(project, 'remote update')
    else:
        existed = False

        # --depth implies --single-branch, so we must pass --branch when
        # creating a shallow clone (since the remote might not have HEAD
        # pointing to the 'revision' branch).
        #
        # Currently, we also pass --branch for non-shallow clones, which checks
        # the 'revision' branch out in the newly created repo, but this might
        # change.

        msg = 'Cloning (name-and-path)'
        cmd = 'clone'
        if not _is_sha(project.revision):
            cmd += ' --branch (revision)'
        if project.clone_depth:
            msg += ' with --depth (clone-depth)'
            cmd += ' --depth (clone-depth)'
        cmd += ' (url)/(name) (path)'

        _inf(project, msg)
        _git_base(project, cmd)

    # Update manifest-rev branch
    _git(project,
         'update-ref refs/heads/(manifest-rev-branch) {}'.format(
             (project.revision if _is_sha(project.revision) else
                 'origin/' + project.revision)))

    return existed


def _rebase(project):
    _inf(project, 'Rebasing (name-and-path) to (manifest-rev-branch)')
    _git(project, 'rebase (manifest-rev-branch)')


def _cloned(project):
    # Returns True if the project's repository exists and looks like a Git
    # repository.
    #
    # Prints a warning if the project's clone path exist but doesn't look like
    # a Git repository.

    if os.path.exists(project.abspath):
        _verify_repo(project)
        return True

    return False


def _verify_repo(project):
    # Raises an error if the project's clone path is not the top-level
    # directory of a Git repository
    log.dbg('verifying project:', project.name, level=log.VERBOSE_EXTREME)
    # --is-inside-work-tree doesn't require that the directory is the top-level
    # directory of a Git repository. Use --show-cdup instead, which prints an
    # empty string (i.e., just a newline, which we strip) for the top-level
    # directory.
    res = _git(project, 'rev-parse --show-cdup', capture_stdout=True,
               check=False)

    if res.returncode or res.stdout:
        _die(project, '(name-and-path) is not the top-level directory of a '
                      'Git repository, as reported by '
                      "'git rev-parse --show-cdup'")


def _branches(project):
    # Returns a sorted list of all local branches in 'project'

    # refname:lstrip=-1 isn't available before Git 2.8 (introduced by commit
    # 'tag: do not show ambiguous tag names as "tags/foo"'). Strip
    # 'refs/heads/' manually instead.
    return [ref[len('refs/heads/'):] for ref in
            _git(project,
                 'for-each-ref --sort=refname --format=%(refname) refs/heads',
                 capture_stdout=True).stdout.split('\n')]


def _create_branch(project, branch):
    if _has_branch(project, branch):
        _inf(project, "Branch '{}' already exists in (name-and-path)"
                      .format(branch))
    else:
        _inf(project, "Creating branch '{}' in (name-and-path)"
                      .format(branch))
        _git(project, 'branch --quiet --track {} (manifest-rev-branch)'
                      .format(branch))


def _has_branch(project, branch):
    return _git(project, 'show-ref --quiet --verify refs/heads/' + branch,
                check=False).returncode == 0


def _checkout(project, branch):
    _inf(project, "Checking out branch '{}' in (name-and-path)".format(branch))
    _git(project, 'checkout ' + branch)


def _is_sha(s):
    try:
        int(s, 16)
    except ValueError:
        return False

    return len(s) == 40


def _git_base(project, cmd, *, extra_args=(), capture_stdout=False,
              check=True):
    # Runs a git command in the West top directory. See _git_helper() for
    # parameter documentation.
    #
    # Returns a CompletedProcess instance (see below).

    return _git_helper(project, cmd, extra_args, util.west_topdir(),
                       capture_stdout, check)


def _git(project, cmd, *, extra_args=(), capture_stdout=False, check=True):
    # Runs a git command within a particular project. See _git_helper() for
    # parameter documentation.
    #
    # Returns a CompletedProcess instance (see below).

    return _git_helper(project, cmd, extra_args, project.abspath,
                       capture_stdout, check)


def _git_helper(project, cmd, extra_args, cwd, capture_stdout, check):
    # Runs a git command.
    #
    # project:
    #   The Project instance for the project, derived from the manifest file.
    #
    # cmd:
    #   String with git arguments. Supports some "(foo)" shorthands. See below.
    #
    # extra_args:
    #   List of additional arguments to pass to the git command (e.g. from the
    #   user).
    #
    # cwd:
    #   Directory to switch to first (None = current directory)
    #
    # capture_stdout:
    #   True if stdout should be captured into the returned
    #   subprocess.CompletedProcess instance instead of being printed.
    #
    #   We never capture stderr, to prevent error messages from being eaten.
    #
    # check:
    #   True if an error should be raised if the git command finishes with a
    #   non-zero return code.
    #
    # Returns a subprocess.CompletedProcess instance.

    # TODO: Run once somewhere?
    if shutil.which('git') is None:
        log.die('Git is not installed or cannot be found')

    args = (('git',) +
            tuple(_expand_shorthands(project, arg) for arg in cmd.split()) +
            tuple(extra_args))
    cmd_str = util.quote_sh_list(args)

    log.dbg("running '{}'".format(cmd_str), 'in', cwd, level=log.VERBOSE_VERY)
    popen = subprocess.Popen(
        args, stdout=subprocess.PIPE if capture_stdout else None, cwd=cwd)

    stdout, _ = popen.communicate()

    if check and popen.returncode:
        _die(project, "Command '{}' failed for (name-and-path)"
                      .format(cmd_str))

    if capture_stdout:
        # Manual UTF-8 decoding and universal newlines. Before Python 3.6,
        # Popen doesn't seem to allow using universal newlines mode (which
        # enables decoding) with a specific encoding (because the encoding=
        # parameter is missing).
        #
        # Also strip all trailing newlines as convenience. The splitlines()
        # already means we lose a final '\n' anyway.
        stdout = "\n".join(stdout.decode('utf-8').splitlines()).rstrip("\n")

    return CompletedProcess(popen.args, popen.returncode, stdout)


def _expand_shorthands(project, s):
    # Expands project-related shorthands in 's' to their values,
    # returning the expanded string

    return s.replace('(name)', project.name) \
            .replace('(name-and-path)',
                     '{} ({})'.format(
                         project.name, os.path.join(project.path, ""))) \
            .replace('(url)', project.url) \
            .replace('(path)', project.path) \
            .replace('(revision)', project.revision) \
            .replace('(manifest-rev-branch)', _MANIFEST_REV_BRANCH) \
            .replace('(clone-depth)', str(project.clone_depth))


def _die(project, msg):
    # Die with 'msg'. Supports the same (foo) shorthands as the git commands.

    log.die(_expand_shorthands(project, msg))


def _inf(project, msg):
    # Print '=== msg' (to clearly separate it from Git output). Supports the
    # same (foo) shorthands as the git commands.

    log.inf('=== ' + _expand_shorthands(project, msg))


def _wrn(project, msg):
    # Warn with 'msg'. Supports the same (foo) shorthands as the git commands.

    log.wrn(_expand_shorthands(project, msg))


# subprocess.CompletedProcess-alike, used instead of the real deal for Python
# 3.4 compatibility, and with two small differences:
#
# - Trailing newlines are stripped from stdout
#
# - The 'stderr' attribute is omitted, because we never capture stderr
CompletedProcess = collections.namedtuple(
    'CompletedProcess', 'args returncode stdout')
