# Copyright (c) 2018, 2019 Nordic Semiconductor ASA
# Copyright 2018, 2019 Foundries.io
#
# SPDX-License-Identifier: Apache-2.0

'''West project commands'''

import argparse
import collections
from functools import partial
import os
from os.path import join, relpath, basename, dirname, exists, isdir
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
    MANIFEST_PROJECT_INDEX, ManifestProject
from west.manifest import MANIFEST_REV_BRANCH as MANIFEST_REV
from west.manifest import QUAL_MANIFEST_REV_BRANCH as QUAL_MANIFEST_REV
from urllib.parse import urlparse
import posixpath


class _ProjectCommand(WestCommand):
    # Helper class which contains common code needed by various commands
    # in this file.

    def _parser(self, parser_adder, **kwargs):
        # Create and return a "standard" parser.

        kwargs['help'] = self.help
        kwargs['description'] = self.description
        kwargs['formatter_class'] = argparse.RawDescriptionHelpFormatter
        return parser_adder.add_parser(self.name, **kwargs)

    def _add_projects_arg(self, parser):
        # Adds a "projects" argument to the given parser.

        parser.add_argument('projects', metavar='PROJECT', nargs='*',
                            help='''projects (by name or path) to operate on;
                            defaults to all projects in the manifest''')

    def _cloned_projects(self, args):
        # Returns _projects(args, listed_must_be_cloned=True) if a
        # list of projects was given by the user (i.e., listed
        # projects are required to be cloned).  If no projects were
        # listed, returns all cloned projects.

        # This approach avoids redundant _cloned() checks
        return self._projects(args.projects) if args.projects else \
            [project for project in self.manifest.projects if _cloned(project)]

    def _projects(self, ids, listed_must_be_cloned=True):
        # Returns a list of project instances for the projects
        # requested in *ids* in the same order that they are specified
        # there.
        #
        # If *ids* is empty all the manifest's projects will be
        # returned. If a non-existent project was listed by the user,
        # an error is raised.
        #
        # ids:
        #   A sequence of projects, identified by name (at first priority)
        #   or path (as a fallback).
        #
        # listed_must_be_cloned (default: True):
        #   If True, an error is raised if an uncloned project was listed. This
        #   only applies to projects listed explicitly on the command line.

        projects = list(self.manifest.projects)

        if not ids:
            # No projects specified. Return all projects.
            return projects

        # Sort the projects by the length of their absolute paths,
        # with the longest path first. That way, projects within
        # projects (e.g., for submodules) are tried before their
        # parent projects, when projects are specified via their path.
        projects.sort(key=lambda project: len(project.abspath), reverse=True)

        # Listed but missing projects. Used for error reporting.
        missing_projects = []

        res = []
        uncloned = []
        for proj_id in ids:
            for project in projects:
                if project.name == proj_id:
                    # The argument is a project name
                    res.append(project)
                    if listed_must_be_cloned and not _cloned(project):
                        uncloned.append(project.name)
                    break
            else:
                # The argument is not a project name. See if it specifies
                # an absolute or relative path to a project.
                proj_arg_norm = util.canon_path(proj_id)
                for project in projects:
                    if proj_arg_norm == util.canon_path(project.abspath):
                        res.append(project)
                        break
                else:
                    # Neither a project name nor a project path. We
                    # will report an error below.
                    missing_projects.append(proj_id)

        if missing_projects:
            log.die(
                'Unknown project name{0}/path{0} {1} (available projects: {2})'
                .format('s' if len(missing_projects) > 1 else '',
                        ', '.join(missing_projects),
                        ', '.join(project.name for project in projects)))

        # Check that all listed repositories are cloned, if requested.
        if listed_must_be_cloned and uncloned:
            plural = len(uncloned) > 1
            log.die(textwrap.dedent('''\
            The following project{} not cloned: {}.
            Please clone {} first, with:
                west update {}
            then retry.'''
                                    .format('s are' if plural else ' is',
                                            ", ".join(uncloned),
                                            'them' if plural else 'it',
                                            " ".join(uncloned))))

        return res


class Init(_ProjectCommand):

    def __init__(self):
        super().__init__(
            'init',
            'create a west installation',
            textwrap.dedent('''\
            Creates a west installation as follows:

              1. Creates a .west directory and clones a manifest repository
                 from a git URL to a temporary subdirectory of .west,
                 .west/<tmpdir>.
              2. Parses the manifest file, .west/<tmpdir>/west.yml.
                 This file's contents can specify manifest.path, the location
                 of the manifest repository in the installation, like so:

                 manifest:
                    self:
                      path: <manifest.path value>

                 If left unspecified, the basename of the manifest
                 repository URL is used as manifest.path (so for example,
                 "http://.../foo/bar" results in "bar").
              3. Creates a local west configuration file, .west/config,
                 and sets the manifest.path option there to the value
                 from step 2. (Run "west config -h" for details on west
                 configuration files.)
              4. Moves the manifest repository from .west/<tmpdir> to
                 manifest.path, next to the .west directory.

            The default manifest repository URL is:

            {}

            This can be overridden using -m.

            The default revision in this repository to check out is "{}";
            override with --mr.'''.format(MANIFEST_URL_DEFAULT,
                                          MANIFEST_REV_DEFAULT)),
            requires_installation=False)

    def do_add_parser(self, parser_adder):
        parser = self._parser(parser_adder)

        parser.add_argument('-m', '--manifest-url',
                            help='''manifest repository URL to clone;
                            cannot be combined with -l''')
        parser.add_argument('--mr', '--manifest-rev', dest='manifest_rev',
                            help='''manifest revision to check out and use;
                            cannot be combined with -l''')
        parser.add_argument('-l', '--local', action='store_true',
                            help='''use an existing local manifest repository
                            instead of cloning one; cannot be combined with
                            -m or --mr.''')

        parser.add_argument(
            'directory', nargs='?', default=None,
            help='''Directory to create the installation in (default: current
                 directory). Missing intermediate directories will be created.
                 If -l is given, this is the path to the manifest repository to
                 use instead.''')

        return parser

    def do_run(self, args, ignored):
        if self.topdir:
            zb = os.environ.get('ZEPHYR_BASE')
            if zb:
                msg = textwrap.dedent('''
                Note:
                    In your environment, ZEPHYR_BASE is set to:
                    {}

                    This forces west to search for an installation there.
                    Try unsetting ZEPHYR_BASE and re-running this command.'''.
                                      format(zb))
            else:
                msg = ''
            log.die('already initialized in {}, aborting.{}'.
                    format(self.topdir, msg))

        if args.local and (args.manifest_url or args.manifest_rev):
            log.die('-l cannot be combined with -m or --mr')

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

        manifest_dir = util.canon_path(args.directory or os.getcwd())
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
        # This validates the manifest. Note we cannot use
        # self.manifest from west init, as we are in the middle of
        # creating the installation right now.
        projects = self.projects(manifest_file)
        _msg('Creating {} and local configuration'.format(west_dir))
        update_config('manifest', 'path', rel_manifest)

        self.topdir = topdir

        return projects, manifest_dir

    def bootstrap(self, args):
        manifest_url = args.manifest_url or MANIFEST_URL_DEFAULT
        manifest_rev = args.manifest_rev or MANIFEST_REV_DEFAULT
        topdir = util.canon_path(args.directory or os.getcwd())
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
        maybe_sha = _maybe_sha(rev)
        if maybe_sha:
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

        if local_rev or maybe_sha:
            subprocess.check_call(('git', 'checkout', rev), cwd=dest)
        else:
            subprocess.check_call(('git', 'checkout', 'FETCH_HEAD'), cwd=dest)


class List(_ProjectCommand):
    def __init__(self):
        super().__init__(
            'list',
            'print information about projects in the west manifest',
            textwrap.dedent('''\
            Print information about projects in the west manifest,
            using format strings.'''))

    def do_add_parser(self, parser_adder):
        default_fmt = '{name:12} {path:28} {revision:40} {url}'
        parser = self._parser(
            parser_adder,
            epilog=textwrap.dedent('''\
            FORMAT STRINGS
            --------------

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
        parser.add_argument('-a', '--all', action='store_true',
                            help='ignored for backwards compatibility'),
        parser.add_argument('-f', '--format', default=default_fmt,
                            help='''format string to use to list each
                            project; see FORMAT STRINGS below.''')

        self._add_projects_arg(parser)

        return parser

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

        for project in self._projects(args.projects):
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


class ManifestCommand(_ProjectCommand):
    # The slightly weird naming is to avoid a conflict with
    # west.manifest.Manifest.

    def __init__(self):
        super(ManifestCommand, self).__init__(
            'manifest',
            'manage the west manifest',
            textwrap.dedent('''\
            Manages the west manifest.

            The --freeze operation outputs the manifest with all
            project-related values fully specified: defaults are
            applied, and all revisions are converted to SHAs based on
            the current manifest-rev branches.

            The --validate operation validates the current manifest,
            printing an error if it cannot be successfully parsed.'''),
            accepts_unknown_args=False)

    def do_add_parser(self, parser_adder):
        parser = self._parser(parser_adder)

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

class Diff(_ProjectCommand):
    def __init__(self):
        super().__init__(
            'diff',
            '"git diff" for one or more projects',
            'Runs "git diff" on each of the specified projects.')

    def do_add_parser(self, parser_adder):
        parser = self._parser(parser_adder)
        self._add_projects_arg(parser)
        return parser

    def do_run(self, args, ignored):
        for project in self._cloned_projects(args):
            _banner(project.format('diff for {name_and_path}:'))
            # Use paths that are relative to the base directory to make it
            # easier to see where the changes are
            _git(project, 'diff --src-prefix={path}/ --dst-prefix={path}/')


class Status(_ProjectCommand):
    def __init__(self):
        super().__init__(
            'status',
            '"git status" for one or more projects',
            "Runs 'git status' for each of the specified projects.")

    def do_add_parser(self, parser_adder):
        parser = self._parser(parser_adder)
        self._add_projects_arg(parser)
        return parser

    def do_run(self, args, user_args):
        for project in self._cloned_projects(args):
            _banner(project.format('status of {name_and_path}:'))
            _git(project, 'status', extra_args=user_args)


class Update(_ProjectCommand):

    def __init__(self):
        super().__init__(
            'update',
            'update projects described in west.yml',
            textwrap.dedent('''\
            Updates each project repository to the revision specified in
            the manifest file, west.yml, as follows:

              1. Fetch the project's remote to ensure the manifest
                 revision is available locally
              2. Reset the manifest-rev branch to the revision in
                 west.yml
              3. Check out the new manifest-rev commit as a detached HEAD
                 (but see "checked out branches")

            You must have already created a west installation with "west init".

            This command does not alter the manifest repository's contents.''')
        )

    def do_add_parser(self, parser_adder):
        parser = self._parser(parser_adder)

        group = parser.add_argument_group(
            title='checked out branches',
            description=textwrap.dedent('''\
            By default, locally checked out branches are left behind
            when manifest-rev commits are checked out.'''))
        group.add_argument('-k', '--keep-descendants', action='store_true',
                           help='''if a checked out branch is a descendant
                           of the new manifest-rev, leave it checked out
                           instead (takes priority over --rebase)''')
        group.add_argument('-r', '--rebase', action='store_true',
                           help='''rebase any checked out branch onto the new
                           manifest-rev instead (leaving behind partial
                           rebases on error)''')

        group = parser.add_argument_group('deprecated options')
        group.add_argument('-x', '--exclude-west', action='store_true',
                           help='ignored for backwards compatibility')

        self._add_projects_arg(parser)

        return parser

    def do_run(self, args, user_args):
        if args.exclude_west:
            log.wrn('ignoring --exclude-west')

        failed_rebases = []

        for project in self._projects(args.projects,
                                      listed_must_be_cloned=False):
            if isinstance(project, ManifestProject):
                continue

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


class ForAll(_ProjectCommand):
    def __init__(self):
        super().__init__(
            'forall',
            'run a command in one or more local projects',
            textwrap.dedent('''\
            Runs a shell (on a Unix OS) or batch (on Windows) command
            within the repository of each of the specified PROJECTs.

            If the command has multiple words, you must quote the -c
            option to prevent the shell from splitting it up. Since
            the command is run through the shell, you can use
            wildcards and the like.

            For example, the following command will list the contents
            of proj-1's and proj-2's repositories on Linux and macOS,
            in long form:

                west forall -c "ls -l" proj-1 proj-2
            '''))

    def do_add_parser(self, parser_adder):
        parser = self._parser(parser_adder)
        parser.add_argument('-c', dest='command', metavar='COMMAND',
                            required=True)
        self._add_projects_arg(parser)
        return parser

    def do_run(self, args, user_args):
        for project in self._cloned_projects(args):
            _banner(project.format('running "{c}" in {name_and_path}:',
                                   c=args.command))

            subprocess.Popen(args.command, shell=True, cwd=project.abspath) \
                .wait()


class SelfUpdate(_ProjectCommand):
    def __init__(self):
        super().__init__(
            'selfupdate',
            'deprecated; exists for backwards compatibility',
            'Do not use. You can upgrade west with pip only from v0.6.0.')

    def do_add_parser(self, parser_adder):
        return self._parser(parser_adder)

    def do_run(self, args, user_args):
        log.die(self.description)


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
    if _maybe_sha(project.revision):
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


def _maybe_sha(rev):
    # Return true if and only if the given revision might be a SHA.

    try:
        int(rev, 16)
    except ValueError:
        return False

    return len(rev) <= 40


def _git(project, cmd, extra_args=(), capture_stdout=False, check=True,
         cwd=None):
    # Wrapper for project.git() that by default calls log.die() with a
    # message about the command that failed if CalledProcessError is raised.

    try:
        res = project.git(cmd, extra_args=extra_args,
                          capture_stdout=capture_stdout, check=check, cwd=cwd)
    except subprocess.CalledProcessError as e:
        msg = project.format(
            "Command '{c}' failed with code {rc} for {name_and_path}",
            c=cmd, rc=e.returncode)

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

def _banner(msg):
    # Prints "msg" as a "banner", i.e. prefixed with '=== ' and colorized.
    log.inf('=== ' + msg, colorize=True)

def _msg(msg):
    # Prints "msg" as a smaller banner, i.e. prefixed with '-- ' and
    # not colorized.
    log.inf('--- ' + msg, colorize=False)


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
