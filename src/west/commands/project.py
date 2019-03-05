# Copyright (c) 2018, Nordic Semiconductor ASA
# Copyright 2018, 2019 Foundries.io
#
# SPDX-License-Identifier: Apache-2.0

'''West project commands'''

import argparse
import collections
import os
import shutil
import subprocess
import sys
import textwrap
import yaml

from west.config import config
from west import log
from west import util
from west.commands import WestCommand, CommandError
from west.manifest import Manifest, MalformedManifest, MANIFEST_PROJECT_INDEX
from west.manifest import MANIFEST_REV_BRANCH as MANIFEST_REV
from west.manifest import QUAL_MANIFEST_REV_BRANCH as QUAL_MANIFEST_REV
from urllib.parse import urlparse
import posixpath


class PostInit(WestCommand):
    def __init__(self):
        super().__init__(
            'post-init',
            'finish init tasks (do not use)',
            _wrap('''
            Finish initializing projects.

            Continue the initialization of the project containing the manifest
            file. You should never need to call this.
            '''))

    def do_add_parser(self, parser_adder):
        return _add_parser(
            parser_adder, self,
            _arg('--manifest-url',
                 metavar='URL',
                 help='Manifest repository URL'),
            _arg('--use-cache',
                 dest='cache',
                 metavar='CACHE',
                 help='''Use cached repo at location CACHE'''),
            _arg('--local',
                 metavar='LOCAL',
                 help='''Use local repo at location LOCAL'''),
            _project_list_arg)

    def do_run(self, args, user_args):
        # manifest.path is not supposed to be set during init, thus clear it
        # for the session and update it to correct location when complete.
        if config.get('manifest', 'path', fallback=None) is not None:
            config.remove_option('manifest', 'path')

        manifest_file = os.path.join(args.local or args.cache, 'west.yml')

        project = Manifest.from_file(manifest_file)\
            .projects[MANIFEST_PROJECT_INDEX]

        if args.local is not None:
            rel_manifest = os.path.relpath(args.local, util.west_topdir())
            _update_key(config, 'manifest', 'path', rel_manifest)
        else:
            if project.path == '':
                url_path = urlparse(args.manifest_url).path
                project.path = posixpath.basename(url_path)
                project.abspath = os.path.realpath(
                    os.path.join(util.west_topdir(), project.path))
                project.name = project.path

            _banner(project.format('Creating repository for {name_and_path}'))
            shutil.move(args.cache, project.abspath)

            _update_key(config, 'manifest', 'path', project.path)


class List(WestCommand):
    def __init__(self):
        super().__init__(
            'list',
            'print information about projects in the west manifest',
            _wrap('''
            List projects.

            Individual projects can be specified by name.

            By default, lists all project names in the manifest, along with
            each project's path, revision, URL, and whether it has been cloned.

            The west repository in the top-level west directory is not included
            by default. Use --all or the name "west" to include it.'''))

    def do_add_parser(self, parser_adder):
        default_fmt = '{name:14} {path:18} {revision:13} {url} {cloned}'
        return _add_parser(
            parser_adder, self,
            _arg('-a', '--all', action='store_true',
                 help='''Do not ignore repositories in west/ (i.e. west and the
                 manifest) in the output. Since these are not part of
                 the manifest, some of their format values (like "revision")
                 come from other sources. The behavior of this option is
                 modeled after the Unix ls -a option.'''),
            _arg('-f', '--format', default=default_fmt,
                 help='''Format string to use to list each project; see
                 FORMAT STRINGS below.'''),
            _project_list_arg,
            epilog=textwrap.dedent('''\
            FORMAT STRINGS

            Projects are listed using a Python 3 format string. Arguments
            to the format string are accessed by name.

            The default format string is:

            "{}"

            The following arguments are available:

            - name: project name in the manifest
            - url: full remote URL as specified by the manifest
            - path: the relative path to the project from the top level,
              as specified in the manifest where applicable
            - abspath: absolute and normalized path to the project
            - posixpath: like abspath, but in posix style, that is, with '/'
              as the separator character instead of '\\'
            - revision: project's manifest revision
            - cloned: "(cloned)" if the project has been cloned, "(not cloned)"
              otherwise
            - clone_depth: project clone depth if specified, "None" otherwise
            '''.format(default_fmt)))

    def do_run(self, args, user_args):
        # Only list west if it was given by name, or --all was given.
        list_west = bool(args.projects) or args.all

        for project in _projects(args, include_west=True):
            if project.name == 'west' and not list_west:
                continue

            # Spelling out the format keys explicitly here gives us
            # future-proofing if the internal Project representation
            # ever changes.
            try:
                result = args.format.format(
                    name=project.name,
                    url=project.url,
                    path=project.path,
                    abspath=project.abspath,
                    posixpath=project.posixpath,
                    revision=project.revision,
                    cloned="(cloned)" if _cloned(project) else "(not cloned)",
                    clone_depth=project.clone_depth or "None")
            except KeyError as e:
                # The raised KeyError seems to just put the first
                # invalid argument in the args tuple, regardless of
                # how many unrecognizable keys there were.
                log.die('unknown key "{}" in format string "{}"'.
                        format(e.args[0], args.format))

            log.inf(result)


class ManifestCommand(WestCommand):
    # The slightly weird naming is to avoid a conflict with
    # west.manifest.Manifest.

    def __init__(self):
        super(ManifestCommand, self).__init__(
            'manifest',
            'manage the west manifest',
            _wrap('''
            Manages the west manifest.

            Currently, only one operation on the manifest is
            implemented, namely --freeze. This outputs the manifest
            with all project-related values fully specified: defaults
            are applied, and all revisions are converted to SHAs based
            on the current manifest-rev branches.'''),
            accepts_unknown_args=False)

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(
            self.name,
            help=self.help,
            description=self.description)

        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--freeze', action='store_true',
                           help='emit a manifest with SHAs for each revision')

        group = parser.add_argument_group('options for --freeze')
        group.add_argument('-o', '--out',
                           help='output file, default is standard output')

        return parser

    def do_run(self, args, user_args):
        # We assume --freeze here. Future extensions to the group
        # --freeze is part of can move this code around.
        frozen = Manifest.from_file().as_frozen_dict()

        # This is a destructive operation, so it's done here to avoid
        # impacting code which doesn't expect this representer to be
        # in place.
        yaml.SafeDumper.add_representer(collections.OrderedDict, self._rep)

        if args.out:
            with open(args.out, 'w') as f:
                yaml.safe_dump(frozen, default_flow_style=False, stream=f)
        else:
            yaml.safe_dump(frozen, default_flow_style=False, stream=sys.stdout)

    def _rep(self, dumper, value):
        # See https://yaml.org/type/map.html for details on the tag.
        return util._represent_ordered_dict(dumper, 'tag:yaml.org,2002:map',
                                            value)


class Diff(WestCommand):
    def __init__(self):
        super().__init__(
            'diff',
            '"git diff" for one or more projects',
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
            _git(project, 'diff --src-prefix={path}/ --dst-prefix={path}/',
                 extra_args=user_args)


class Status(WestCommand):
    def __init__(self):
        super().__init__(
            'status',
            '"git status" for one or more projects',
            _wrap('''
            Runs 'git status' for each of the specified projects (default: all
            cloned projects). Extra arguments are passed as-is to 'git status'.
            '''),
            accepts_unknown_args=True)

    def do_add_parser(self, parser_adder):
        return _add_parser(parser_adder, self, _project_list_arg)

    def do_run(self, args, user_args):
        for project in _cloned_projects(args):
            _banner(project.format('status of {name_and_path}'))
            _git(project, 'status', extra_args=user_args)


class Update(WestCommand):
    # Commit comment
    def __init__(self):
        super().__init__(
            'update',
            'update projects described in west.yml',
            _wrap('''
            Updates west and all project repositories according to the
            manifest file, `west.yml`, in the manifest repository.

            By default:

            1. The revisions in the manifest file are fetched from the
            remote in the west repository in the installation and each
            project repository.

            2. The local manifest-rev branches in each repository
            (which are managed exclusively by west) will be hard reset
            to the updated commits fetched in step 1.

            3. All repositories will have detached HEADs checked out
            at the new manifest-rev commits. Locally checked out
            branches are unaffected but will no longer be checked out
            by default.  West will print information on how to get
            back to those branches or update them to the new
            manifest-rev versions.

            You can skip updating west using --exclude-west.

            You can influence the behavior when local branches are
            checked out using --keep-descendants and/or --rebase.

            This command does not change the contents of the manifest
            repository.
            '''))

    def do_add_parser(self, parser_adder):
        return _add_parser(parser_adder, self,
                           _arg('-x', '--exclude-west',
                                dest='update',
                                action='store_false',
                                help='do not self-update West'),
                           _arg('-k', '--keep-descendants',
                                action='store_true',
                                help=_wrap('''
                                If a local branch is checked out and is a
                                descendant commit of the new manifest-rev,
                                then keep that descendant branch checked out
                                instead of detaching HEAD. This takes priority
                                over --rebase when possible if both are given.
                                ''')),
                           _arg('-r', '--rebase',
                                action='store_true',
                                help=_wrap('''
                                If a local branch is checked out, try
                                to rebase it onto the new HEAD and
                                leave the rebased branch checked
                                out. If this fails, you will need to
                                clean up the rebase yourself manually.
                                ''')),
                           _project_list_arg)

    def do_run(self, args, user_args):
        if args.update:
            _update_west(args.rebase, args.keep_descendants)

        failed_rebases = []

        for project in _projects(args, listed_must_be_cloned=False,
                                 exclude_manifest=True):
            returncode = _update(project, args.rebase, args.keep_descendants)
            if returncode:
                failed_rebases.append(project)
                log.err(project.format('{name_and_path} failed to rebase'))

        if failed_rebases:
            # Avoid printing this message if exactly one project
            # was specified on the command line.
            if len(args.projects) != 1:
                log.err(('The following project{} failed to rebase; '
                        'see above for details: {}').format(
                            's' if len(failed_rebases) > 1 else '',
                            ', '.join(p.format('{name_and_path}')
                                      for p in failed_rebases)))
            raise CommandError(1)


class SelfUpdate(WestCommand):
    def __init__(self):
        super().__init__(
            'selfupdate',
            'selfupdate the west repository',
            _wrap('''
            Updates the West source code repository. The remote to update from
            and the revision to update to is taken from the west section within
            the manifest file, `west.yml`, in the manifest repository.

            There is normally no need to run this command manually, because
            'west update' automatically updates the West repository to the
            latest version before doing anything else.
            '''))

    def do_add_parser(self, parser_adder):
        return _add_parser(parser_adder, self,
                           _arg('-k', '--keep-descendants',
                                action='store_true',
                                help=_wrap('''
                                If a local branch is checked out and is a
                                descendant commit of the new manifest-rev,
                                then keep that descendant branch checked out
                                instead of detaching HEAD. This takes priority
                                over --rebase when possible if both are given.
                                ''')),
                           _arg('-r', '--rebase',
                                action='store_true',
                                help=_wrap('''
                                If a local branch is checked out, try
                                to rebase it onto the new HEAD and
                                leave the rebased branch checked
                                out. If this fails, you will need to
                                clean up the rebase yourself manually.
                                ''')))

    def do_run(self, args, user_args):
        _update_west(args.rebase, args.keep_descendants)


class ForAll(WestCommand):
    def __init__(self):
        super().__init__(
            'forall',
            'run a command in one or more local projects',
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
            _banner(project.format("Running '{cmd}' in {name_and_path}",
                                   cmd=args.command))

            subprocess.Popen(args.command, shell=True, cwd=project.abspath) \
                .wait()


def _arg(*args, **kwargs):
    # Helper for creating a new argument parser for a single argument,
    # later passed in parents= to add_parser()

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(*args, **kwargs)
    return parser


# Arguments shared between more than one command

# List of projects
_project_list_arg = _arg('projects', metavar='PROJECT', nargs='*')


def _add_parser(parser_adder, cmd, *extra_args, **kwargs):
    # Adds and returns a subparser for the project-related WestCommand 'cmd'.
    # Any defaults can be overridden with kwargs.

    if 'help' not in kwargs:
        kwargs['help'] = cmd.help
    if 'description' not in kwargs:
        kwargs['description'] = cmd.description
    if 'formatter_class' not in kwargs:
        kwargs['formatter_class'] = argparse.RawDescriptionHelpFormatter
    if 'parents' not in kwargs:
        kwargs['parents'] = extra_args

    return parser_adder.add_parser(cmd.name, **kwargs)


def _wrap(s):
    # Wraps help texts for commands. Some of them have variable length (due to
    # MANIFEST_REV), so just a textwrap.dedent() can look a bit wonky.

    # [1:] gets rid of the initial newline. It's turned into a space by
    # textwrap.fill() otherwise.
    paragraphs = textwrap.dedent(s[1:]).split("\n\n")

    return "\n\n".join(textwrap.fill(paragraph) for paragraph in paragraphs)


_MANIFEST_REV_HELP = """
The '{}' branch points to the revision that the manifest specified for the
project as of the most recent 'west fetch'/'west pull'.
""".format(MANIFEST_REV)[1:].replace("\n", " ")


def _cloned_projects(args):
    # Returns _projects(args, listed_must_be_cloned=True) if a list of projects
    # was given by the user (i.e., listed projects are required to be cloned).
    # If no projects were listed, returns all cloned projects.

    # This approach avoids redundant _cloned() checks
    return _projects(args) if args.projects else \
        [project for project in _all_projects() if _cloned(project)]


def _projects(args, listed_must_be_cloned=True, include_west=False,
              exclude_manifest=False):
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
    #
    # include_west (default: False):
    #   If True, west may be given in args.projects without raising errors.
    #   It will be included in the return value if args.projects is empty.
    #
    # exclude_manifest (default: False):
    #   If True, the manifest project will not be included in the returned
    #   list.

    projects = _all_projects()
    west_project = _west_project()

    if include_west:
        projects.append(west_project)

    if exclude_manifest:
        projects.pop(MANIFEST_PROJECT_INDEX)

    if not args.projects:
        # No projects specified. Return all projects.
        return projects

    # Sort the projects by the length of their absolute paths, with the longest
    # path first. That way, projects within projects (e.g., for submodules) are
    # tried before their parent projects, when projects are specified via their
    # path.
    projects.sort(key=lambda project: len(project.abspath), reverse=True)

    # Listed but missing projects. Used for error reporting.
    missing_projects = []

    def normalize(path):
        # Returns a case-normalized canonical absolute version of 'path', for
        # comparisons. The normcase() is a no-op on platforms on case-sensitive
        # filesystems.
        return os.path.normcase(os.path.realpath(path))

    res = []
    uncloned = []
    for project_arg in args.projects:
        for project in projects:
            if project.name == project_arg:
                # The argument is a project name
                res.append(project)
                if listed_must_be_cloned and not _cloned(project):
                    uncloned.append(project.name)
                break
        else:
            # The argument is not a project name. See if it specifies
            # an absolute or relative path to a project.
            proj_arg_norm = normalize(project_arg)
            for project in projects:
                if proj_arg_norm == normalize(project.abspath):
                    res.append(project)
                    break
            else:
                # Neither a project name nor a project path. We will report an
                # error below.
                missing_projects.append(project_arg)

    if missing_projects:
        log.die('Unknown project name{0}/path{0} {1} (available projects: {2})'
                .format('s' if len(missing_projects) > 1 else '',
                        ', '.join(missing_projects),
                        ', '.join(project.name for project in projects)))

    # Check that all listed repositories are cloned, if requested.
    if listed_must_be_cloned and uncloned:
        log.die('The following projects are not cloned: {}. Please clone '
                "them first with 'west clone'."
                .format(", ".join(uncloned)))

    return res


def _all_projects():
    # Get a list of project objects from the manifest.
    #
    # If the manifest is malformed, a fatal error occurs and the
    # command aborts.

    try:
        return list(Manifest.from_file(sections=['manifest']).projects)
    except MalformedManifest as m:
        log.die(m.args[0])


def _fetch(project):
    # Fetches upstream changes for 'project' and updates the 'manifest-rev'
    # branch to point to the revision specified in the manifest. If the
    # project's repository does not already exist, it is created first.

    if not _cloned(project):
        _banner(project.format('Creating repository for {name_and_path}'))
        _git(project, 'init {abspath}', cwd=util.west_topdir())
        # This remote is only added for the user's convenience. We always fetch
        # directly from the URL specified in the manifest.
        _git(project, 'remote add -- {remote_name} {url}')

    # Fetch the revision specified in the manifest into the manifest-rev branch

    msg = "Fetching changes for {name_and_path}"
    if project.clone_depth:
        fetch_cmd = "fetch --depth={clone_depth}"
        msg += " with --depth {clone_depth}"
    else:
        fetch_cmd = "fetch"

    _banner(project.format(msg))
    # This two-step approach avoids a "trying to write non-commit object" error
    # when the revision is an annotated tag. ^{commit} type peeling isn't
    # supported for the <src> in a <src>:<dst> refspec, so we have to do it
    # separately.
    #
    # --tags is required to get tags when the remote is specified as an URL.
    if _is_sha(project.revision):
        # Don't fetch a SHA directly, as server may restrict from doing so.
        _git(project, fetch_cmd + ' --tags -- {url} refs/heads/*:refs/west/*')
        _git(project, 'update-ref ' + QUAL_MANIFEST_REV + ' {revision}')
    else:
        _git(project, fetch_cmd + ' --tags -- {url} {revision}')
        _git(project,
             'update-ref ' + QUAL_MANIFEST_REV + ' FETCH_HEAD^{{commit}}')

    if not _head_ok(project):
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
        _git(project, 'checkout --detach ' + QUAL_MANIFEST_REV)


def _rebase(project, **kwargs):
    # Rebases the project against the manifest-rev branch
    #
    # Any kwargs are passed on to the underlying _git() call for the
    # rebase operation. A CompletedProcess instance is returned for
    # the git rebase.
    log.inf(project.format('Rebasing {name_and_path} to ' + MANIFEST_REV))
    return _git(project, 'rebase ' + QUAL_MANIFEST_REV, **kwargs)


def _sha(project, rev):
    # Returns project.sha(rev), aborting the program on CalledProcessError.

    try:
        return project.sha(rev)
    except subprocess.CalledProcessError:
        log.die(project.format(
            "failed to get SHA for revision '{r}' in {name_and_path}",
            r=rev))


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


def _current_branch(project):
    # Determine if project is currently on a branch
    if not _cloned(project):
        return None

    branch = _git(project, 'rev-parse --abbrev-ref HEAD',
                  capture_stdout=True).stdout

    if branch == 'HEAD':
        return None
    else:
        return branch


def _head_ok(project):
    # Returns True if the reference 'HEAD' exists and is not a tag or remote
    # ref (e.g. refs/remotes/origin/HEAD).
    # Some versions of git will report 1, when doing
    # 'git show-ref --verify HEAD' even if HEAD is valid, see #119.
    # 'git show-ref --head <reference>' will always return 0 if HEAD or
    # <reference> is valid.
    # We are only interested in HEAD, thus we must avoid <reference> being
    # valid. '/' can never point to valid reference, thus 'show-ref --head /'
    # will return:
    # - 0 if HEAD is present
    # - 1 otherwise
    return _git(project, 'show-ref --quiet --head /', check=False) \
           .returncode == 0


def _checkout_detach(project, revision):
    log.inf(project.format(
        "Checking out revision '{r}' as detached HEAD in {name_and_path}",
        r=_sha(project, revision)))
    _git(project, 'checkout --detach --quiet ' + revision)
    # The checkout above was quiet to avoid multi line spamming when checking
    # out in detached HEAD in each project.
    # However the final line 'HEAD is now at ....' is still desired to print.
    print('HEAD is now at ' + _git(project, 'log --oneline -1',
                                   capture_stdout=True).stdout)


def _west_project():
    # Returns a Project instance for west.
    return Manifest.from_file(sections=['west']).west_project


def _update_west(rebase, keep_descendants):
    with _error_context(_FAILED_UPDATE_MSG):
        project = _west_project()
        log.dbg(project.format('Updating {name_and_path}'))

        old_sha = _sha(project, 'HEAD')
        _update(project, rebase, keep_descendants)

        if old_sha != _sha(project, 'HEAD'):
            log.inf(project.format(
                'Updated {name_and_path} to {revision} (from {url}).'))

            # Signal self-update, which will cause a restart. This is a bit
            # nicer than doing the restart here, as callers will have a
            # chance to flush file buffers, etc.
            raise WestUpdated()


def _update(project, rebase, keep_descendants):
    _fetch(project)

    branch = _current_branch(project)
    sha = _sha(project, MANIFEST_REV)
    if branch is not None:
        is_ancestor = project.is_ancestor_of(sha, branch)
        try_rebase = rebase
    else:
        # If no branch is checked out, 'rebase' and 'keep_descendants' don't
        # matter.
        is_ancestor = False
        try_rebase = False

    if keep_descendants and is_ancestor:
        # A descendant is currently checked out and keep_descendants was
        # given, so there's nothing more to do.
        log.inf('Left branch "{}", a descendant of {}, checked out'.
                format(branch, sha))
    elif try_rebase:
        # Attempt a rebase. Don't exit the program on error;
        # instead, append to the list of failed rebases and
        # continue trying to update the other projects. We'll
        # tell the user a complete list of errors when we're done.
        cp = _rebase(project, check=False)
        return cp.returncode
    else:
        # We can't keep a descendant or rebase, so just check
        # out the new detached HEAD and print helpful
        # information about things they can do with any
        # locally checked out branch.
        _checkout_detach(project, MANIFEST_REV)
        _post_checkout_help(project, branch, sha, is_ancestor)
    return 0


def _post_checkout_help(project, branch, sha, is_ancestor):
    # Print helpful information to the user about a project that
    # might have just left a branch behind.

    if branch is None:
        # If there was no branch checked out, there are no
        # additional diagnostics that need emitting.
        return

    relpath = os.path.relpath(project.abspath)
    if is_ancestor:
        # If the branch we just left behind is a descendant of
        # the new HEAD (e.g. if this is a topic branch the
        # user is working on and the remote hasn't changed),
        # print a message that makes it easy to get back,
        # no matter where in the installation os.getcwd() is.
        log.wrn(project.format(
            'left behind {name} branch "{b}"; '
            'to fast forward back, use: git -C {rp} checkout {b}',
            b=branch, rp=relpath))
        log.dbg('(To do this automatically in the future,',
                'use "west update --keep-descendants".)')
    else:
        # Tell the user how they could rebase by hand, and
        # point them at west update --rebase.
        log.wrn(project.format(
            'left behind {name} branch "{b}"; '
            'to rebase onto the new HEAD: git -C {rp} rebase {sh} {b}',
            b=branch, rp=relpath, sh=sha))
        log.dbg('(To do this automatically in the future,',
                'use "west update --rebase".)')


_FAILED_UPDATE_MSG = """
, while running automatic self-update. Pass --exclude-west to
'west update' to skip updating west for the duration of the command."""[1:]


class WestUpdated(Exception):
    '''Raised after West has updated its own source code'''


def _is_sha(s):
    try:
        int(s, 16)
    except ValueError:
        return False

    return len(s) == 40


def _git(project, cmd, extra_args=(), capture_stdout=False, check=True,
         cwd=None):
    # Wrapper for project.git() that by default calls log.die() with a
    # message about the command that failed if CalledProcessError is raised.
    #
    # If the global error context value is set, it is appended to the
    # message.

    try:
        res = project.git(cmd, extra_args=extra_args,
                          capture_stdout=capture_stdout, check=check, cwd=cwd)
    except subprocess.CalledProcessError as e:
        msg = project.format(
            "Command '{c}' failed with code {rc} for {name_and_path}",
            c=cmd, rc=e.returncode)

        if _error_context_msg:
            msg += _error_context_msg.replace('\n', ' ')

        log.die(msg)

    if capture_stdout:
        # Manual UTF-8 decoding and universal newlines. Before Python 3.6,
        # Popen doesn't seem to allow using universal newlines mode (which
        # enables decoding) with a specific encoding (because the encoding=
        # parameter is missing).
        #
        # Also strip all trailing newlines as convenience. The splitlines()
        # already means we lose a final '\n' anyway.
        res.stdout = "\n".join(
            res.stdout.decode('utf-8').splitlines()).rstrip("\n")

    return res


def _update_key(config, section, key, value):
    '''
    Updates 'key' in section 'section' in ConfigParser 'config', creating
    'section' if it does not exist and write the file afterwards.

    If value is None/empty, 'key' is left as-is.
    '''
    if not value:
        return

    if section not in config:
        config[section] = {}

    config[section][key] = value

    with open(os.path.join(util.west_dir(), 'config'), 'w') as f:
        config.write(f)


# Some Python shenanigans to be able to set up a context with
#
#   with _error_context("Doing stuff"):
#       Do the stuff
#
# The _error_context() argument is extra text that gets printed in the
# log.die() call made by _git() in case of errors.
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


def _banner(msg):
    # Prints "msg" as a "banner", i.e. prefixed with '=== ' and colorized.
    log.inf('=== ' + msg, colorize=True)
