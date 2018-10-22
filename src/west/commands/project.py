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

from west.config import config
from west import log
from west import util
from west.commands import WestCommand
from west.manifest import default_path, Remote, Project, \
                          Manifest, MalformedManifest


# Branch that points to the revision specified in the manifest (which might be
# an SHA). Local branches created with 'west branch' are set to track this
# branch.
_MANIFEST_REV_BRANCH = 'manifest-rev'


class List(WestCommand):
    def __init__(self):
        super().__init__(
            'list',
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
            log.inf('{:14}  {:18}  {:13}  {}  {}'.format(
                project.name,
                os.path.join(project.path, ''),  # Add final '/' if missing
                project.revision,
                project.url,
                "(cloned)" if _cloned(project) else "(not cloned)"))


class Clone(WestCommand):
    def __init__(self):
        super().__init__(
            'clone',
            _wrap('''
            Clone projects.

            Clones each of the specified projects (default: all projects) and
            creates a branch in each. The branch is named after the project's
            revision, and tracks the 'manifest-rev' branch (see below).

            If the project's revision is an SHA, the branch will simply be
            called 'work'.

            This command is really just a shorthand for 'west fetch' +
            'west checkout -b <branch name>'. If you clone a project with
            'west fetch' instead, you will start in a detached HEAD state at
            'manifest-rev'.

            {}

            {}'''.format(_NO_UPDATE_HELP, _MANIFEST_REV_HELP)))

    def do_add_parser(self, parser_adder):
        return _add_parser(
            parser_adder, self,
            _arg('-b',
                 dest='branch',
                 metavar='BRANCH_NAME',
                 help='an alternative branch name to use, instead of one '
                      'based on the revision'),
            _no_update_arg,
            _project_list_arg)

    def do_run(self, args, user_args):
        if args.update:
            _update_west()
            _update_manifest()

        for project in _projects(args, listed_must_be_cloned=False):
            if args.branch:
                branch = args.branch
            elif _is_sha(project.revision):
                # Don't name the branch after an SHA
                branch = 'work'
            else:
                # Use the last component of the revision, in case it is a
                # qualified ref (refs/heads/foo and the like)
                branch = project.revision.split('/')[-1]

            _fetch(project)
            _create_branch(project, branch)
            _checkout(project, branch)


class Fetch(WestCommand):
    def __init__(self):
        super().__init__(
            'fetch',
            _wrap('''
            Fetch projects.

            Fetches upstream changes in each of the specified projects
            (default: all projects). Repositories that do not already exist are
            cloned.

            {}

            {}'''.format(_NO_UPDATE_HELP, _MANIFEST_REV_HELP)))

    def do_add_parser(self, parser_adder):
        return _add_parser(parser_adder, self, _no_update_arg,
                           _project_list_arg)

    def do_run(self, args, user_args):
        if args.update:
            _update_west()
            _update_manifest()

        for project in _projects(args, listed_must_be_cloned=False):
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

            {}

            {}'''.format(_MANIFEST_REV_BRANCH, _NO_UPDATE_HELP,
                         _MANIFEST_REV_HELP)))

    def do_add_parser(self, parser_adder):
        return _add_parser(parser_adder, self, _no_update_arg,
                           _project_list_arg)

    def do_run(self, args, user_args):
        if args.update:
            _update_west()
            _update_manifest()

        for project in _projects(args, listed_must_be_cloned=False):
            _fetch(project)
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
        for project in _cloned_projects(args):
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
        return _add_parser(
            parser_adder, self,
            _arg('branch', nargs='?', metavar='BRANCH_NAME'),
            _project_list_arg)

    def do_run(self, args, user_args):
        if args.branch:
            # Create a branch in the specified projects
            for project in _cloned_projects(args):
                _create_branch(project, args.branch)
        else:
            # No arguments. List local branches from all cloned projects along
            # with the projects they appear in.

            branch2projs = collections.defaultdict(list)
            for project in _cloned_projects(args):
                for branch in _branches(project):
                    branch2projs[branch].append(project.name)

            for branch, projs in sorted(branch2projs.items()):
                log.inf('{:18} {}'.format(branch, ", ".join(projs)))


class Checkout(WestCommand):
    def __init__(self):
        super().__init__(
            'checkout',
            _wrap('''
            Check out local branch.

            Checks out a local branch in each of the specified projects
            (default: all cloned projects). Projects that do not have the
            branch are left alone.

            Note: To check out remote branches, use ordinary Git commands
            inside the repositories. This command is meant for switching
            between work branches that span multiple repositories, without any
            interference from whatever remote branches might exist.

            If '-b BRANCH_NAME' is passed, the new branch will be set to track
            '{}', like for 'west branch BRANCH_NAME'.
            '''.format(_MANIFEST_REV_BRANCH)))

    def do_add_parser(self, parser_adder):
        return _add_parser(
            parser_adder, self,
            _arg('-b',
                 dest='create_branch',
                 action='store_true',
                 help='create the branch before checking it out'),
            _arg('branch', metavar='BRANCH_NAME'),
            _project_list_arg)

    def do_run(self, args, user_args):
        branch_exists = False

        for project in _cloned_projects(args):
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
        for project in _cloned_projects(args):
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
        for project in _cloned_projects(args):
            _inf(project, 'status of (name-and-path)')
            _git(project, 'status', extra_args=user_args)


class Update(WestCommand):
    def __init__(self):
        super().__init__(
            'update',
            _wrap('''
            Updates the manifest repository and/or the West source code
            repository.

            There is normally no need to run this command manually, because
            'west fetch' and 'west pull' automatically update the West and
            manifest repositories to the latest version before doing anything
            else.

            Pass --update-west or --update-manifest to update just that
            repository. With no arguments, both are updated.
            '''))

    def do_add_parser(self, parser_adder):
        return _add_parser(
            parser_adder, self,
            _arg('--update-west',
                 dest='update_west',
                 action='store_true',
                 help='update the West source code repository'),
            _arg('--update-manifest',
                 dest='update_manifest',
                 action='store_true',
                 help='update the manifest repository'))

    def do_run(self, args, user_args):
        if args.update_west or args.update_manifest:
            if args.update_west:
                _update_west()
            if args.update_manifest:
                _update_manifest()
        else:
            _update_west()
            _update_manifest()


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
        return _add_parser(
            parser_adder, self,
            _arg('-c',
                 dest='command',
                 metavar='COMMAND',
                 required=True),
            _project_list_arg)

    def do_run(self, args, user_args):
        for project in _cloned_projects(args):
            _inf(project, "Running '{}' in (name-and-path)"
                          .format(args.command))

            subprocess.Popen(args.command, shell=True, cwd=project.abspath) \
                .wait()


def _arg(*args, **kwargs):
    # Helper for creating a new argument parser for a single argument,
    # later passed in parents= to add_parser()

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(*args, **kwargs)
    return parser


# Arguments shared between more than one command

_manifest_arg = _arg(
    '-m', '--manifest',
    help='path to manifest file (default: west/manifest/default.yml)')

# For 'fetch' and 'pull'
_no_update_arg = _arg(
    '--no-update',
    dest='update',
    action='store_false',
    help='do not update the manifest or West before fetching project data')

# List of projects
_project_list_arg = _arg('projects', metavar='PROJECT', nargs='*')


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


_NO_UPDATE_HELP = """
Unless --no-update is passed, the manifest and West source code repositories
are updated prior to cloning. See the 'update' command.
"""[1:].replace('\n', ' ')



_MANIFEST_REV_HELP = """
The '{}' branch points to the revision that the manifest specified for the
project as of the most recent 'west fetch'/'west pull'.
""".format(_MANIFEST_REV_BRANCH)[1:].replace("\n", " ")


def _cloned_projects(args):
    # Returns _projects(args, listed_must_be_cloned=True) if a list of projects
    # was given by the user (i.e., listed projects are required to be cloned).
    # If no projects were listed, returns all cloned projects.

    # This approach avoids redundant _cloned() checks
    return _projects(args) if args.projects else \
        [project for project in _all_projects(args) if _cloned(project)]


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
    #   If True, an error is raised if an uncloned project was listed. This
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
    # Get a list of project objects from the manifest.
    #
    # If the manifest is malformed, a fatal error occurs and the
    # command aborts.

    try:
        return list(Manifest.from_file(_manifest_path(args)).projects)
    except MalformedManifest as m:
        log.die(m.args[0])


def _manifest_path(args):
    # Returns the path to the manifest file. Defaults to
    # .west/manifest/default.yml if the user didn't specify a manifest.

    return args.manifest or default_path()


def _fetch(project):
    # Fetches upstream changes for 'project' and updates the 'manifest-rev'
    # branch to point to the revision specified in the manifest. If the
    # project's repository does not already exist, it is created first.

    if not _cloned(project):
        _inf(project, 'Creating repository for (name-and-path)')
        _git_base(project, 'init (abspath)')
        # This remote is only added for the user's convenience. We always fetch
        # directly from the URL specified in the manifest.
        _git(project, 'remote add -- (remote-name) (url)')

    # Fetch the revision specified in the manifest into the manifest-rev branch

    msg = "Fetching changes for (name-and-path)"
    if project.clone_depth:
        fetch_cmd = "fetch --depth=(clone-depth)"
        msg += " with --depth (clone-depth)"
    else:
        fetch_cmd = "fetch"

    _inf(project, msg)
    # This two-step approach avoids a "trying to write non-commit object" error
    # when the revision is an annotated tag. ^{commit} type peeling isn't
    # supported for the <src> in a <src>:<dst> refspec, so we have to do it
    # separately.
    _git(project, fetch_cmd + ' -- (url) (revision)')
    _git(project, 'update-ref (qual-manifest-rev-branch) FETCH_HEAD^{commit}')

    if not _ref_ok(project, 'HEAD'):
        # If nothing it checked out (which would usually only happen just after
        # we initialize the repository), check out 'manifest-rev' in a detached
        # HEAD state.
        #
        # Otherwise, the initial state would have nothing checked out, and HEAD
        # would point to a non-existent refs/heads/master branch (that would
        # get created if the user makes an initial commit). That state causes
        # e.g. 'west rebase' to fail, and might look confusing.
        #
        # The --detach flag is strictly redundant here, because the
        # refs/heads/<branch> form already detaches HEAD, but it avoids a
        # spammy detached HEAD warning from Git.
        _git(project, 'checkout --detach (qual-manifest-rev-branch)')


def _rebase(project):
    # Rebases the project against the manifest-rev branch

    if _up_to_date_with(project, _MANIFEST_REV_BRANCH):
        _inf(project,
             '(name-and-path) is up-to-date with (manifest-rev-branch)')
    else:
        _inf(project, 'Rebasing (name-and-path) to (manifest-rev-branch)')
        _git(project, 'rebase (qual-manifest-rev-branch)')


def _sha(project, rev):
    # Returns the SHA of a revision (HEAD, v2.0.0, etc.), passed as a string in
    # 'rev'

    return _git(project, 'rev-parse ' + rev, capture_stdout=True).stdout


def _merge_base(project, rev1, rev2):
    # Returns the latest commit in common between 'rev1' and 'rev2'

    return _git(project, 'merge-base -- {} {}'.format(rev1, rev2),
                capture_stdout=True).stdout


def _up_to_date_with(project, rev):
    # Returns True if all commits in 'rev' are also in HEAD. This is used to
    # check if 'project' needs rebasing. 'revision' can be anything that
    # resolves to a commit.

    return _sha(project, rev) == _merge_base(project, 'HEAD', rev)


def _cloned(project):
    # Returns True if the project's path is a directory that looks
    # like the top-level directory of a Git repository, and False
    # otherwise.

    def handle(result):
        log.dbg('project', project.name,
                'is {}cloned'.format('' if result else 'not '),
                level=log.VERBOSE_EXTREME)
        return result

    if not os.path.isdir(project.abspath):
        return handle(False)

    # --is-inside-work-tree doesn't require that the directory is the top-level
    # directory of a Git repository. Use --show-cdup instead, which prints an
    # empty string (i.e., just a newline, which we strip) for the top-level
    # directory.
    res = _git(project, 'rev-parse --show-cdup', capture_stdout=True,
               check=False)

    return handle(not (res.returncode or res.stdout))


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

        _git(project, 'branch --quiet --track -- {} (qual-manifest-rev-branch)'
                      .format(branch))


def _has_branch(project, branch):
    return _ref_ok(project, 'refs/heads/' + branch)


def _ref_ok(project, ref):
    # Returns True if the reference 'ref' exists and can be resolved to a
    # commit
    return _git(project, 'show-ref --quiet --verify ' + ref, check=False) \
           .returncode == 0


def _checkout(project, branch):
    _inf(project, "Checking out branch '{}' in (name-and-path)".format(branch))
    _git(project, 'checkout ' + branch)


def _special_project(name):
    # Returns a Project instance for one of the special repositories in west/,
    # so that we can reuse the project-related functions for them

    # TODO: Maybe the Remote class could be replaced by a single
    # project.remote_name field, now that we no longer use the Git remote
    # mechanism and fetch directly from URLs

    remote = Remote(name='dummy name for {} repository'.format(name),
                    url='dummy URL for {} repository'.format(name))

    # 'revision' always exists and defaults to 'master'
    project = Project(name, remote, None,
                      revision=config.get(name, 'revision', fallback='master'),
                      path=os.path.join('west', name))

    # This could also be the name of a Git remote. The naming breaks a bit
    # here.
    project.url = config.get(name, 'remote', fallback='origin')

    return project


def _update_west():
    _update_special('west')


def _update_manifest():
    _update_special('manifest')


def _update_special(name):
    with _error_context(_FAILED_UPDATE_MSG):
        project = _special_project(name)
        _dbg(project, 'Updating (name-and-path)', level=log.VERBOSE_NORMAL)

        old_sha = _sha(project, 'HEAD')

        # Only update special repositories if possible via fast-forward, as
        # automatic rebasing is probably more annoying than useful when working
        # directly on them. --rebase=false must be passed for --ff-only to be
        # respected e.g. when pull.rebase is set.
        if _git(project,
                'pull --quiet --rebase=false --ff-only -- (url) (revision)',
                check=False).returncode:

            _wrn(project, 'Skipping automatic update of (name-and-path). '
                          "Can't be fast-forwarded to (revision) (from "
                          '(url)).')

        elif old_sha != _sha(project, 'HEAD'):
            _inf(project, 'Updated (name-and-path) to (revision) (from '
                          '(url)).')

            if project.name == 'west':
                # Signal self-update, which will cause a restart. This is a
                # bit nicer than doing the restart here, as callers will
                # have a chance to flush file buffers, etc.
                raise WestUpdated()


_FAILED_UPDATE_MSG = """
, while running automatic self-update. Pass --no-update to 'west fetch/pull' to
skip updating the manifest and West for the duration of the command."""[1:]


class WestUpdated(Exception):
    '''Raised after West has updated its own source code'''


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

    dbg_msg = "'{}' in {} finished with exit status {}" \
              .format(cmd_str, cwd, popen.returncode)
    if capture_stdout:
        dbg_msg += " and wrote {} to stdout".format(stdout)
    log.dbg(dbg_msg, level=log.VERBOSE_VERY)

    if check and popen.returncode:
        msg = "Command '{}' failed for (name-and-path)".format(cmd_str)
        if _error_context_msg:
            msg += _error_context_msg.replace('\n', ' ')
        _die(project, msg)

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


# Some Python shenanigans to be able to set up a context with
#
#   with _error_context("Doing stuff"):
#       Do the stuff
#
# A context is just some extra text that gets printed on Git errors.
#
# Note: If we ever need to support nested contexts, _error_context_msg could be
# turned into a stack.

_error_context_msg = None

class _error_context:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        global _error_context_msg
        _error_context_msg = self.msg

    def __exit__(self, *args):
        global _error_context_msg
        _error_context_msg = None


def _expand_shorthands(project, s):
    # Expands project-related shorthands in 's' to their values,
    # returning the expanded string

    # Some of the trickier ones below. 'qual' stands for 'qualified', meaning
    # the full path to the ref (e.g. refs/heads/master).
    #
    # manifest-rev-branch:
    #   The name of the magic branch that points to the manifest revision
    #
    # qual-manifest-rev-branch:
    #   A qualified reference to the magic manifest revision branch, e.g.
    #   refs/heads/manifest-rev

    return s.replace('(name)', project.name) \
            .replace('(name-and-path)',
                     '{} ({})'.format(
                         project.name, os.path.join(project.path, ""))) \
            .replace('(remote-name)', project.remote.name) \
            .replace('(url)', project.url) \
            .replace('(path)', project.path) \
            .replace('(abspath)', project.abspath) \
            .replace('(revision)', project.revision) \
            .replace('(manifest-rev-branch)', _MANIFEST_REV_BRANCH) \
            .replace('(qual-manifest-rev-branch)',
                     'refs/heads/' + _MANIFEST_REV_BRANCH) \
            .replace('(clone-depth)', str(project.clone_depth))


def _inf(project, msg):
    # Print '=== msg' (to clearly separate it from Git output). Supports the
    # same (foo) shorthands as the git commands.
    #
    # Prints the message in green if stdout is a terminal, to clearly separate
    # it from command (usually Git) output.

    log.inf('=== ' + _expand_shorthands(project, msg), colorize=True)


def _wrn(project, msg):
    # Warn with 'msg'. Supports the same (foo) shorthands as the git commands.

    log.wrn(_expand_shorthands(project, msg))


def _dbg(project, msg, level):
    # Like _wrn(), for debug messages

    log.dbg(_expand_shorthands(project, msg), level=level)


def _die(project, msg):
    # Like _wrn(), for dying

    log.die(_expand_shorthands(project, msg))


# subprocess.CompletedProcess-alike, used instead of the real deal for Python
# 3.4 compatibility, and with two small differences:
#
# - Trailing newlines are stripped from stdout
#
# - The 'stderr' attribute is omitted, because we never capture stderr
CompletedProcess = collections.namedtuple(
    'CompletedProcess', 'args returncode stdout')
