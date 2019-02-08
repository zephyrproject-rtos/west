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
from urllib.parse import urlparse
import posixpath


# Branch that points to the revision specified in the manifest (which might be
# an SHA). Local branches created with 'west branch' are set to track this
# branch.
_MANIFEST_REV_BRANCH = 'manifest-rev'

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
        manifest_file = os.path.join(args.local or args.cache, 'west.yml')

        project = Manifest.from_file(manifest_file)\
            .projects[MANIFEST_PROJECT_INDEX]

        if args.local is not None:
            rel_manifest = os.path.relpath(args.local, util.west_topdir())
            _update_key(config, 'manifest', 'path', rel_manifest)
        else:
            if project.path is None:
                url_path = urlparse(args.manifest_url).path
                project.path = posixpath.basename(url_path)
                project.name = project.path

            _inf(project, 'Creating repository for {name_and_path}')
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

        m = Manifest.from_file()

        # Build a 'frozen' representation of all projects, except the
        # manifest project.
        projects = list(m.projects)
        del projects[MANIFEST_PROJECT_INDEX]
        frozen_projects = []
        for project in projects:
            sha = _sha(project, '{qual_manifest_rev_branch}')
            d = project.as_dict()
            d['revision'] = sha
            frozen_projects.append(d)

        # We include the defaults value here even though all projects
        # are fully specified in order to make the resulting manifest
        # easy to extend by users who want to reuse the defaults.
        o = collections.OrderedDict()
        o['west'] = m.west_project.as_dict()
        o['west']['revision'] = _sha(m.west_project,
                                     '{qual_manifest_rev_branch}')
        o['manifest'] = collections.OrderedDict()
        o['manifest']['defaults'] = m.defaults.as_dict()
        o['manifest']['remotes'] = [r.as_dict() for r in m.remotes]
        o['manifest']['projects'] = frozen_projects
        o['manifest']['self'] = m.projects[MANIFEST_PROJECT_INDEX].as_dict()

        # This is a destructive operation, so it's done here to avoid
        # impacting code which doesn't expect this representer to be
        # in place.
        yaml.SafeDumper.add_representer(collections.OrderedDict, self._rep)

        if args.out:
            with open(args.out, 'w') as f:
                yaml.safe_dump(o, default_flow_style=False, stream=f)
        else:
            yaml.safe_dump(o, default_flow_style=False, stream=sys.stdout)

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
            _inf(project, 'status of {name_and_path}')
            _git(project, 'status', extra_args=user_args)


class Update(WestCommand):
    # Commit comment
    def __init__(self):
        super().__init__(
            'update',
            'update projects described in west.yml',
            _wrap('''
            Updates all projects according to the manifest file, `west.yml`,
            in the manifest repository.

            By default:

            1. West itself is updated to the revision in the manifest
            file. If revision is a branch, west will update to the tip
            of the branch.

            2. The local per-project manifest-rev branches (which are
            managed exclusively by west) will be hard reset to the
            updated commits fetched from each project's remote.

            3. All projects will have detached HEADs checked out at
            the manifest-rev commits. Locally checked out branches are
            unaffected but will no longer be checked out by default.
            West will print information on how to get back to those
            branches or update them to the new manifest-rev versions.

            You can avoid 1. using --no-update. You can (sometimes)
            avoid 3. using --keep-descendants and/or --rebase.
            See below for more information.

            This command does not change the contents of the manifest
            repository.
            '''))

    def do_add_parser(self, parser_adder):
        return _add_parser(parser_adder, self,
                           _no_update_arg,
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
            _update_west()

        failed_rebases = []

        for project in _projects(args, listed_must_be_cloned=False,
                                 exclude_manifest=True):
            _fetch(project)

            branch = _current_branch(project)
            sha = _sha(project, _MANIFEST_REV_BRANCH)
            if branch is not None:
                is_ancestor = _is_ancestor_of(project, sha, branch)
                try_rebase = args.rebase
            else:
                # If no branch is checked out, -k and -r don't matter.
                is_ancestor = False
                try_rebase = False

            if args.keep_descendants and is_ancestor:
                # A descendant is currently checked out and -k was
                # given, so there's nothing more to do.
                _inf(project,
                     'Left branch "{}", a descendant of {}, checked out'.
                     format(branch, sha))
            elif try_rebase:
                # Attempt a rebase. Don't exit the program on error;
                # instead, append to the list of failed rebases and
                # continue trying to update the other projects. We'll
                # tell the user a complete list of errors when we're done.
                cp = _rebase(project, check=False)
                if cp.returncode:
                    failed_rebases.append(project)
                    _err(project, '{name_and_path} failed to rebase')
            else:
                # We can't keep a descendant or rebase, so just check
                # out the new detached HEAD and print helpful
                # information about things they can do with any
                # locally checked out branch.
                _checkout_detach(project, _MANIFEST_REV_BRANCH)
                self._post_checkout_help(args, project, branch, sha,
                                         is_ancestor)

        if failed_rebases:
            # Avoid printing this message if exactly one project
            # was specified on the command line.
            if len(args.projects) != 1:
                log.err(('The following project{} failed to rebase; '
                        'see above for details: {}').format(
                            's' if len(failed_rebases) > 1 else '',
                            ', '.join(_expand_shorthands(p, '{name_and_path}')
                                      for p in failed_rebases)))
            raise CommandError(1)

    def _post_checkout_help(self, args, project, branch, sha,
                            is_ancestor):
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
            _wrn(project,
                 ('left behind {{name}} branch "{}"; '
                  'to fast forward back, use: git -C {} checkout {}').
                 format(branch, relpath, branch))
            log.dbg('(To do this automatically in the future,',
                    'use "west update --keep-descendants".)')
        else:
            # Tell the user how they could rebase by hand, and
            # point them at west update --rebase.
            _wrn(project,
                 ('left behind {{name}} branch "{}"; '
                  'to rebase onto the new HEAD: git -C {} rebase {} {}').
                 format(branch, relpath, sha, branch))
            log.dbg('(To do this automatically in the future,',
                    'use "west update --rebase".)')


class SelfUpdate(WestCommand):
    def __init__(self):
        super().__init__(
            'selfupdate',
            'selfupdate the west repository',
            _wrap('''
            Updates the manifest repository and/or the West source code
            repository. The remote to update from is taken from the
            manifest.remote and manifest.remote configuration settings, and the
            revision from manifest.revision and west.revision configuration
            settings.

            There is normally no need to run this command manually, because
            'west fetch' and 'west pull' automatically update the West and
            manifest repositories to the latest version before doing anything
            else.

            Pass --update-west or --update-manifest to update just that
            repository. With no arguments, both are updated.

            Updates are skipped (with a warning) if they can't be done via
            fast-forward, unless --reset-west, or --reset-projects is given.
            '''))

    def do_add_parser(self, parser_adder):
        return _add_parser(
            parser_adder, self,
            _arg('--reset-west',
                 action='store_true',
                 help='''Like --update-west, but run 'git reset --keep'
                      afterwards to reset the west repository to the commit
                      pointed at by the west.remote and west.revision
                      configuration settings. This is used internally when
                      changing west.remote or west.revision via
                      'west init'.'''),
                      )

    def do_run(self, args, user_args):
        if args.reset_west:
            _update_and_reset_west()
        else:
            _update_west()


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
            _inf(project, "Running '{}' in {{name_and_path}}"
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

# For 'fetch' and 'pull'
_no_update_arg = _arg(
    '--no-update',
    dest='update',
    action='store_false',
    help='do not self-update West')

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
    # _MANIFEST_REV_BRANCH), so just a textwrap.dedent() can look a bit wonky.

    # [1:] gets rid of the initial newline. It's turned into a space by
    # textwrap.fill() otherwise.
    paragraphs = textwrap.dedent(s[1:]).split("\n\n")

    return "\n\n".join(textwrap.fill(paragraph) for paragraph in paragraphs)


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
        _inf(project, 'Creating repository for {name_and_path}')
        _git_base(project, 'init {abspath}')
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

    _inf(project, msg)
    # This two-step approach avoids a "trying to write non-commit object" error
    # when the revision is an annotated tag. ^{commit} type peeling isn't
    # supported for the <src> in a <src>:<dst> refspec, so we have to do it
    # separately.
    #
    # --tags is required to get tags when the remote is specified as an URL.
    if _is_sha(project.revision):
        # Don't fetch a SHA directly, as server may restrict from doing so.
        _git(project, fetch_cmd + ' --tags -- {url} '
             'refs/heads/*:refs/west/*')
        _git(project, 'update-ref {qual_manifest_rev_branch} {revision}')
    else:
        _git(project, fetch_cmd + ' --tags -- {url} {revision}')
        _git(project,
             'update-ref {qual_manifest_rev_branch} FETCH_HEAD^{{commit}}')

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
        _git(project, 'checkout --detach {qual_manifest_rev_branch}')


def _rebase(project, **kwargs):
    # Rebases the project against the manifest-rev branch
    #
    # Any kwargs are passed on to the underlying _git() call for the
    # rebase operation. A CompletedProcess instance is returned for
    # the git rebase.
    _inf(project, 'Rebasing {name_and_path} to {manifest_rev_branch}')
    return _git(project, 'rebase {qual_manifest_rev_branch}', **kwargs)


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
    #
    # This is a special case of _is_ancestor_of() which exists for convenience.

    return _is_ancestor_of(project, rev, 'HEAD')


def _is_ancestor_of(project, rev1, rev2):
    # Returns True if rev1 is an ancestor commit of rev2 in the given
    # project; rev1 and rev2 can be anything that resolves to a
    # commit. (If rev1 and rev2 refer to the same commit, the return
    # value is True, i.e. a commit is considered an ancestor of
    # itself.) Returns False otherwise.
    returncode = _git(project,
                      'merge-base --is-ancestor {} {}'.format(rev1, rev2),
                      check=False).returncode

    if returncode == 0:
        return True
    elif returncode == 1:
        return False
    else:
        _wrn(project,
             ('_is_ancestor_of: {{name_and_path}}: '
              'git failed with exit code {}; '
              'treating as if "{}" is not an ancestor of "{}"').
             format(returncode, rev1, rev2))
        return False


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
        _inf(project, "Branch '{}' already exists in {{name_and_path}}"
                      .format(branch))
    else:
        _inf(project, "Creating branch '{}' in {{name_and_path}}"
                      .format(branch))

        _git(project,
             'branch --quiet --track -- {} {{qual_manifest_rev_branch}}'
             .format(branch))


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


def _has_branch(project, branch):
    return _ref_ok(project, 'refs/heads/' + branch)


def _ref_ok(project, ref):
    # Returns True if the reference 'ref' exists and can be resolved to a
    # commit
    return _git(project, 'show-ref --quiet --verify ' + ref, check=False) \
           .returncode == 0


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


def _checkout(project, revision):
    _inf(project,
         "Checking out revision '{}' in {{name_and_path}}".format(revision))
    _git(project, 'checkout ' + revision)


def _checkout_detach(project, revision):
    _inf(project,
         "Checking out revision '{}' as detached HEAD in {{name_and_path}}"
         .format(_sha(project, revision)))
    _git(project, 'checkout --detach --quiet ' + revision)
    # The checkout above was quiet to avoid multi line spamming when checking
    # out in detached HEAD in each project.
    # However the final line 'HEAD is now at ....' is still desired to print.
    print('HEAD is now at ' + _git(project, 'log --oneline -1',
                                   capture_stdout=True).stdout)


def _west_project():
    # Returns a Project instance for west.
    return Manifest.from_file(sections=['west']).west_project


def _update_west():
    with _error_context(_FAILED_UPDATE_MSG):
        project = _west_project()
        _dbg(project, 'Updating {name_and_path}', level=log.VERBOSE_NORMAL)

        old_sha = _sha(project, 'HEAD')

        # Only update west via fast-forward, as automatic rebasing is
        # probably more annoying than useful when working directly on it.
        #
        # --tags is required to get tags when the remote is specified as a URL.
        # --ff-only is required to ensure that the merge only takes place if it
        # can be fast-forwarded.
        if _git(project,
                'fetch --quiet --tags -- {url} {revision}',
                check=False).returncode:

            _wrn(project,
                 'Skipping automatic update of {name_and_path}. '
                 "{revision} cannot be fetched (from {url}).")

        elif _git(project,
                  'merge --quiet --ff-only FETCH_HEAD',
                  check=False).returncode:

            _wrn(project,
                 'Skipping automatic update of {name_and_path}. '
                 "Can't be fast-forwarded to {revision} (from {url}).")

        elif old_sha != _sha(project, 'HEAD'):
            _git(project, 'update-ref {qual_manifest_rev_branch} {revision}')

            _inf(project,
                 'Updated {name_and_path} to {revision} (from {url}).')

            # Signal self-update, which will cause a restart. This is a bit
            # nicer than doing the restart here, as callers will have a
            # chance to flush file buffers, etc.
            raise WestUpdated()


def _update_and_reset_west():
    # Updates west by resetting to the new revision after fetching it
    # (with 'git reset --keep').

    project = _west_project()
    with _error_context(', while updating/resetting west'):
        _inf(project,
             "Fetching and resetting {name_and_path} to '{revision}'")
        _git(project, 'fetch -- {url} {revision}')
        if _git(project, 'reset --keep FETCH_HEAD', check=False).returncode:
            _wrn(project,
                 'Failed to reset west to "{revision}" '
                 "(with 'git reset --keep')")


def _reset_projects():
    # Fetches changes in all cloned projects and then resets them the manifest
    # revision (with 'git reset --keep')

    for project in _all_projects():
        if _cloned(project):
            _fetch(project)
            _inf(project, 'Resetting {name_and_path} to {manifest_rev_branch}')
            if _git(project, 'reset --keep {manifest_rev_branch}',
                    check=False).returncode:

                _wrn(project,
                     'Failed to reset {name_and_path} to '
                     "{manifest_rev_branch} (with 'git reset --keep')")


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
        msg = "Command '{}' failed for {{name_and_path}}".format(cmd_str)
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

    return s.format(name=project.name,
                    name_and_path='{} ({})'.format(
                        project.name, os.path.join(project.path, "")),
                    remote_name=('None' if project.remote is None
                                 else project.remote.name),
                    url=project.url,
                    path=project.path,
                    abspath=project.abspath,
                    revision=project.revision,
                    manifest_rev_branch=_MANIFEST_REV_BRANCH,
                    qual_manifest_rev_branch=('refs/heads/' +
                                              _MANIFEST_REV_BRANCH),
                    clone_depth=str(project.clone_depth))


def _dbg(project, msg, level):
    # Like _wrn(), for debug messages

    log.dbg(_expand_shorthands(project, msg), level=level)


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


def _err(project, msg):
    # Error with 'msg'. Supports the same (foo) shorthands as the git commands.

    log.err(_expand_shorthands(project, msg))


def _die(project, msg):
    # Like _err(), for dying

    log.die(_expand_shorthands(project, msg))


# subprocess.CompletedProcess-alike, used instead of the real deal for Python
# 3.4 compatibility, and with two small differences:
#
# - Trailing newlines are stripped from stdout
#
# - The 'stderr' attribute is omitted, because we never capture stderr
CompletedProcess = collections.namedtuple(
    'CompletedProcess', 'args returncode stdout')
