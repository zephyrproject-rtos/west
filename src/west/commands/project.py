# Copyright (c) 2018, 2019 Nordic Semiconductor ASA
# Copyright 2018, 2019 Foundries.io
#
# SPDX-License-Identifier: Apache-2.0

'''West project commands'''

import argparse
import collections
from functools import partial
import os
from os.path import join, abspath, relpath, realpath, normpath, \
    basename, dirname, normcase, exists, isdir
import shutil
import subprocess
import sys
import textwrap
import yaml

from west.configuration import config, update_config
from west import log
from west import util
from west.commands import WestCommand, CommandError
from west.manifest import Manifest, MalformedManifest, MalformedConfig, \
    MANIFEST_PROJECT_INDEX
from west.manifest import MANIFEST_REV_BRANCH as MANIFEST_REV
from west.manifest import QUAL_MANIFEST_REV_BRANCH as QUAL_MANIFEST_REV
from urllib.parse import urlparse
import posixpath


class Init(WestCommand):

    def __init__(self):
        super().__init__(
            'init',
            'create a west installation',
            textwrap.dedent('''\
            Creates a west installation as follows:

              1. Creates a .west directory and clones the manifest repository
                 to a temporary subdirectory of it
              2. Creates a local configuration file (.west/config)
              3. Parses west.yml in the manifest repository and moves that
                 repository to the correct location in the installation.'''),
            requires_installation=False)

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(
            self.name, help=self.help, description=self.description,
            formatter_class=argparse.RawDescriptionHelpFormatter)

        mutualex_group = parser.add_mutually_exclusive_group()
        mutualex_group.add_argument(
            '-m', '--manifest-url',
            help='Manifest repository URL (default: {})'
                 .format(MANIFEST_URL_DEFAULT))
        mutualex_group.add_argument(
            '-l', '--local', action='store_true',
            help='''Use an existing local manifest repository instead of
                 cloning one. The local repository will not be modified.
                 Cannot be combined with -m or --mr.''')

        parser.add_argument(
            '--mr', '--manifest-rev', dest='manifest_rev',
            help='''Manifest revision to fetch (default: {}). Cannot be combined
                 with --local'''
                 .format(MANIFEST_REV_DEFAULT))
        parser.add_argument(
            'directory', nargs='?', default=None,
            help='''Directory to create the installation in (default: cwd).
                 Missing directories will be created. If -l is given, this is
                 the path to the manifest repository to use instead.''')

        return parser

    def do_run(self, args, ignored):
        if self.topdir:
            log.die('already in an installation ({}), aborting'.
                    format(self.topdir))

        if shutil.which('git') is None:
            log.die("can't find git; install it or ensure it's on your PATH")

        # west.manifest will try to read manifest.path and use it when
        # parsing the manifest. Clear it out for now so we can parse
        # the manifest without it; local() or bootstrap() will set it
        # properly.
        if config.get('manifest', 'path', fallback=None) is not None:
            config.remove_option('manifest', 'path')
        if args.local:
            projects, manifest_dir = self.local(args)
        else:
            projects, manifest_dir = self.bootstrap(args)

        self.fixup_zephyr_base(projects)

        _banner('Initialized. Now run "west update" inside {}.'.
                format(self.topdir))

    def local(self, args):
        if args.manifest_rev is not None:
            log.die('--mr cannot be used with -l')

        manifest_dir = canonical(args.directory or os.getcwd())
        manifest_file = join(manifest_dir, 'west.yml')
        topdir = dirname(manifest_dir)
        rel_manifest = basename(manifest_dir)
        west_dir = os.path.join(topdir, WEST_DIR)

        _banner('Initializing from existing manifest repository ' +
                rel_manifest)
        if not exists(manifest_file):
            log.die('No "west.yml" found in {}'.format(manifest_dir))

        self.create(west_dir)
        os.chdir(topdir)
        projects = self.projects(manifest_file)  # This validates the manifest.
        _msg('Creating {} and local configuration'.format(west_dir))
        update_config('manifest', 'path', rel_manifest)

        self.topdir = topdir

        return projects, manifest_dir

    def bootstrap(self, args):
        manifest_url = args.manifest_url or MANIFEST_URL_DEFAULT
        manifest_rev = args.manifest_rev or MANIFEST_REV_DEFAULT
        topdir = canonical(args.directory or os.getcwd())
        west_dir = join(topdir, WEST_DIR)

        _banner('Initializing in ' + topdir)
        if not isdir(topdir):
            self.create(topdir, exist_ok=False)
        os.chdir(topdir)

        # Clone the manifest repository into a temporary directory. It's
        # important that west_dir exists and we're under topdir, or we
        # won't be able to call self.projects() without error later.
        tempdir = join(west_dir, 'manifest-tmp')
        if exists(tempdir):
            log.dbg('removing existing temporary manifest directory', tempdir)
            shutil.rmtree(tempdir)
        try:
            self.clone_manifest(manifest_url, manifest_rev, tempdir)
            temp_manifest_file = join(tempdir, 'west.yml')
            if not exists(temp_manifest_file):
                log.die('No "west.yml" in manifest repository ({})'.
                        format(tempdir))

            projects = self.projects(temp_manifest_file)
            manifest_project = projects[MANIFEST_PROJECT_INDEX]
            if manifest_project.path:
                manifest_path = manifest_project.path
                manifest_abspath = join(topdir, manifest_path)
            else:
                url_path = urlparse(manifest_url).path
                manifest_path = posixpath.basename(url_path)
                manifest_abspath = join(topdir, manifest_path)

            shutil.move(tempdir, manifest_abspath)
            update_config('manifest', 'path', manifest_path)
        finally:
            shutil.rmtree(tempdir, ignore_errors=True)

        self.topdir = topdir

        return projects, manifest_project.abspath

    def projects(self, manifest_file):
        try:
            return Manifest.from_file(manifest_file).projects
        except MalformedManifest as mm:
            log.die(mm.args[0])
        except MalformedConfig as mc:
            log.die(mc.args[0])

    def fixup_zephyr_base(self, projects):
        for project in projects:
            if project.path == 'zephyr':
                update_config('zephyr', 'base', project.path)

    def create(self, directory, exist_ok=True):
        try:
            os.makedirs(directory, exist_ok=exist_ok)
        except PermissionError:
            log.die('Cannot initialize in {}: permission denied'.
                    format(directory))
        except FileExistsError:
            log.die('Something else created {} concurrently; quitting'.
                    format(directory))
        except Exception as e:
            log.die("Can't create directory {}: {}".format(directory, e.args))

    def clone_manifest(self, url, rev, dest, exist_ok=False):
        _msg('Cloning manifest repository from {}, rev. {}'.format(url, rev))
        if not exist_ok and exists(dest):
            log.die('refusing to clone into existing location ' + dest)

        subprocess.check_call(('git', 'init', dest))
        subprocess.check_call(('git', 'remote', 'add', 'origin', '--', url),
                              cwd=dest)
        is_sha = _is_sha(rev)
        if is_sha:
            # Fetch the ref-space and hope the SHA is contained in
            # that ref-space
            subprocess.check_call(('git', 'fetch', 'origin', '--tags',
                                   '--', 'refs/heads/*:refs/remotes/origin/*'),
                                  cwd=dest)
        else:
            # Fetch the ref-space similar to git clone plus the ref
            # given by user.  Redundancy is ok, for example if the user
            # specifies 'heads/master'. This allows users to specify:
            # pull/<no>/head for pull requests
            subprocess.check_call(('git', 'fetch', 'origin', '--tags', '--',
                                   rev, 'refs/heads/*:refs/remotes/origin/*'),
                                  cwd=dest)

        try:
            # Using show-ref to determine if rev is available in local repo.
            subprocess.check_call(('git', 'show-ref', '--', rev), cwd=dest)
            local_rev = True
        except subprocess.CalledProcessError:
            local_rev = False

        if local_rev or is_sha:
            subprocess.check_call(('git', 'checkout', rev), cwd=dest)
        else:
            subprocess.check_call(('git', 'checkout', 'FETCH_HEAD'), cwd=dest)


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
        default_fmt = '{name:12} {path:28} {revision:40} {url}'
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
            - revision: project's revision as it appears in the manifest
            - sha: project's revision as a SHA
            - cloned: "cloned" if the project has been cloned, "not-cloned"
              otherwise
            - clone_depth: project clone depth if specified, "None" otherwise
            '''.format(default_fmt)))

    def do_run(self, args, user_args):
        def sha_thunk(project):
            if project.revision:
                return _sha(project, MANIFEST_REV)
            else:
                return '{:40}'.format('N/A')

        def cloned_thunk(project):
            return "cloned" if _cloned(project) else "not-cloned"

        def delay(func, project):
            return DelayFormat(partial(func, project))

        for project in _projects(args):
            # Spelling out the format keys explicitly here gives us
            # future-proofing if the internal Project representation
            # ever changes.
            #
            # Using DelayFormat delays computing derived values, such
            # as SHAs, unless they are specifically requested, and then
            # ensures they are only computed once.
            try:
                result = args.format.format(
                    name=project.name,
                    url=project.url or 'N/A',
                    path=project.path,
                    abspath=project.abspath,
                    posixpath=project.posixpath,
                    revision=project.revision or 'N/A',
                    clone_depth=project.clone_depth or "None",
                    cloned=delay(cloned_thunk, project),
                    sha=delay(sha_thunk, project))
            except KeyError as e:
                # The raised KeyError seems to just put the first
                # invalid argument in the args tuple, regardless of
                # how many unrecognizable keys there were.
                log.die('unknown key "{}" in format string "{}"'.
                        format(e.args[0], args.format))
            except IndexError:
                self.parser.print_usage()
                log.die('invalid format string', args.format)

            log.inf(result, colorize=False)  # don't use _msg()!


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
        group.add_argument('--validate', action='store_true',
                           help='''validate the current manifest,
                           exiting with an error if there are issues''')

        group = parser.add_argument_group('options for --freeze')
        group.add_argument('-o', '--out',
                           help='output file, default is standard output')

        return parser

    def do_run(self, args, user_args):
        if args.freeze:
            self._freeze(args)
        else:
            # --validate. The exception block in main() handles errors.
            Manifest.from_file()

    def _freeze(self, args):
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
            _banner(project.format('diff for {name_and_path}:'))
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
            _banner(project.format('status of {name_and_path}:'))
            _git(project, 'status', extra_args=user_args)


class Update(WestCommand):
    # Commit comment
    def __init__(self):
        super().__init__(
            'update',
            'update projects described in west.yml',
            _wrap('''
            Updates all project repositories according to the
            manifest file, `west.yml`, in the manifest repository.

            By default:

            1. The revisions in the manifest file are fetched.
            2. The local manifest-rev branches in each repository
               are reset to the updated revisions.
            3. All repositories will have detached HEADs checked out
               at the new manifest-rev commits.

            You can influence the behavior when local branches are
            checked out using --keep-descendants and/or --rebase.

            This command does not change the contents of the manifest
            repository.'''))

    def do_add_parser(self, parser_adder):
        return _add_parser(parser_adder, self,
                           _arg('-x', '--exclude-west',
                                action='store_true',
                                help='''deprecated and ignored; exists
                                for backwards compatibility'''),
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
        if args.exclude_west:
            log.wrn('ignoring --exclude-west')

        failed_rebases = []

        for project in _projects(args, listed_must_be_cloned=False,
                                 exclude_manifest=True):
            _banner(project.format('updating {name_and_path}:'))

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
            _banner(project.format('running "{c}" in {name_and_path}:',
                                   c=args.command))

            subprocess.Popen(args.command, shell=True, cwd=project.abspath) \
                .wait()


class SelfUpdate(WestCommand):
    def __init__(self):
        super().__init__(
            'selfupdate',
            'deprecated; exists for backwards compatibility',
            'Do not use. You can upgrade west with pip only from v0.6.0.')

    def do_add_parser(self, parser_adder):
        return _add_parser(parser_adder, self)

    def do_run(self, args, user_args):
        log.die(self.description)


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


def _projects(args, listed_must_be_cloned=True, exclude_manifest=False):
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
    # exclude_manifest (default: False):
    #   If True, the manifest project will not be included in the returned
    #   list.

    projects = _all_projects()

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
        return normcase(realpath(path))

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
        return list(Manifest.from_file().projects)
    except MalformedManifest as m:
        log.die(m.args[0])


def _fetch(project):
    # Fetches upstream changes for 'project' and updates the 'manifest-rev'
    # branch to point to the revision specified in the manifest. If the
    # project's repository does not already exist, it is created first.

    if not _cloned(project):
        _msg(project.format('{name}: cloning and initializing'))
        _git(project, 'init {abspath}', cwd=util.west_topdir())
        # This remote is only added for the user's convenience. We always fetch
        # directly from the URL specified in the manifest.
        _git(project, 'remote add -- {remote_name} {url}')

    # Fetch the revision specified in the manifest into the manifest-rev branch

    msg = "{name}: fetching changes"
    if project.clone_depth:
        fetch_cmd = "fetch --depth={clone_depth}"
        msg += " with --depth {clone_depth}"
    else:
        fetch_cmd = "fetch"

    _msg(project.format(msg))
    # This two-step approach avoids a "trying to write non-commit object" error
    # when the revision is an annotated tag. ^{commit} type peeling isn't
    # supported for the <src> in a <src>:<dst> refspec, so we have to do it
    # separately.
    #
    # --tags is required to get tags when the remote is specified as an URL.
    if _is_sha(project.revision):
        # Don't fetch a SHA directly, as server may restrict from doing so.
        _git(project, fetch_cmd + ' -f --tags '
             '-- {url} refs/heads/*:refs/west/*')
        _git(project, 'update-ref ' + QUAL_MANIFEST_REV + ' {revision}')
    else:
        _git(project, fetch_cmd + ' -f --tags -- {url} {revision}')
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
    _msg(project.format('{name}: rebasing to ' + MANIFEST_REV))
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

    if not isdir(project.abspath):
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
    _git(project, 'checkout --detach --quiet ' + revision)
    _msg(project.format("{name}: checked out {r} as detached HEAD",
                        r=_sha(project, revision)))


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
        _msg(project.format(
            '{name}: left descendant branch "{b}" checked out',
            b=branch))
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

    rel = relpath(project.abspath)
    if is_ancestor:
        # If the branch we just left behind is a descendant of
        # the new HEAD (e.g. if this is a topic branch the
        # user is working on and the remote hasn't changed),
        # print a message that makes it easy to get back,
        # no matter where in the installation os.getcwd() is.
        log.wrn(project.format(
            'left behind {name} branch "{b}"; to switch '
            'back to it (fast forward), use: git -C {rp} checkout {b}',
            b=branch, rp=rel))
        log.dbg('(To do this automatically in the future,',
                'use "west update --keep-descendants".)')
    else:
        # Tell the user how they could rebase by hand, and
        # point them at west update --rebase.
        log.wrn(project.format(
            'left behind {name} branch "{b}"; '
            'to rebase onto the new HEAD: git -C {rp} rebase {sh} {b}',
            b=branch, rp=rel, sh=sha))
        log.dbg('(To do this automatically in the future,',
                'use "west update --rebase".)')


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

def _msg(msg):
    # Prints "msg" as a smaller banner, i.e. prefixed with '-- ' and
    # not colorized.
    log.inf('--- ' + msg, colorize=False)

def canonical(path):
    return normpath(abspath(path))


#
# Special files and directories in the west installation.
#
# These are given variable names for clarity, but they can't be
# changed without propagating the changes into west itself.
#

# Top-level west directory, containing west itself and the manifest.
WEST_DIR = '.west'

# Manifest repository directory under WEST_DIR.
MANIFEST = 'manifest'
# Default manifest repository URL.
MANIFEST_URL_DEFAULT = 'https://github.com/zephyrproject-rtos/zephyr'
# Default revision to check out of the manifest repository.
MANIFEST_REV_DEFAULT = 'master'

#
# Helper class for creating format string keys that are expensive or
# undesirable to compute if not needed.
#

class DelayFormat:
    '''Delays formatting an object.'''

    def __init__(self, obj):
        '''Delay formatting `obj` until a format operation using it.

        :param obj: object to format

        If callable(obj) returns True, then obj() will be used as the
        string to be formatted. Otherwise, str(obj) is used.'''
        self.obj = obj
        self.as_str = None

    def __format__(self, format_spec):
        if self.as_str is None:
            if callable(self.obj):
                self.as_str = self.obj()
                assert isinstance(self.as_str, str)
            else:
                self.as_str = str(self.obj)
        return ('{:' + format_spec + '}').format(self.as_str)
