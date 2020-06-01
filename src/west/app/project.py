# Copyright (c) 2018, 2019 Nordic Semiconductor ASA
# Copyright 2018, 2019 Foundries.io
#
# SPDX-License-Identifier: Apache-2.0

'''West project commands'''

import argparse
from functools import partial, lru_cache
import logging
import os
from os.path import join, relpath, basename, dirname, exists, isdir
from pathlib import PurePath
import shutil
import shlex
import subprocess
import sys
import textwrap
from time import perf_counter
from urllib.parse import urlparse

from west.configuration import config, update_config
from west import log
from west import util
from west.commands import WestCommand, CommandError
from west.manifest import ImportFlag, Manifest, MANIFEST_PROJECT_INDEX, \
    ManifestProject, _manifest_content_at, ManifestImportFailed, \
    _ManifestImportDepth, ManifestVersionError, MalformedManifest
from west.manifest import MANIFEST_REV_BRANCH as MANIFEST_REV
from west.manifest import QUAL_MANIFEST_REV_BRANCH as QUAL_MANIFEST_REV
from west.manifest import QUAL_REFS_WEST as QUAL_REFS

#
# Project-related or multi-repo commands, like "init", "update",
# "diff", etc.
#

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
        # Returns _projects(args.projects, only_cloned=True) if
        # args.projects is not empty (i.e., explicitly given projects
        # are required to be cloned). Otherwise, returns all cloned
        # projects.
        if args.projects:
            return self._projects(args.projects, only_cloned=True)
        else:
            return [p for p in self.manifest.projects if p.is_cloned()]

    def _projects(self, ids, only_cloned=False):
        try:
            return self.manifest.get_projects(ids, only_cloned=only_cloned)
        except ValueError as ve:
            if len(ve.args) != 2:
                raise          # not directly raised by get_projects()

            # Die with an error message on unknown or uncloned projects.
            unknown, uncloned = ve.args
            if unknown:
                die_unknown(unknown)
            elif only_cloned and uncloned:
                s = 's' if len(uncloned) > 1 else ''
                names = ' '.join(p.name for p in uncloned)
                log.die(f'uncloned project{s}: {names}.\n'
                        '  Hint: run "west update" and retry.')
            else:
                # Should never happen, but re-raise to fail fast and
                # preserve a stack trace, to encourage a bug report.
                raise

    def _handle_failed(self, args, failed):
        # Shared code for commands (like status, diff, update) that need
        # to do the same thing to multiple projects, but collect
        # and report errors if anything failed.

        if not failed:
            return
        elif len(failed) < 20:
            s = 's:' if len(failed) > 1 else ''
            projects = ', '.join(f'{p.name}' for p in failed)
            log.err(f'{self.name} failed for project{s} {projects}')
        else:
            log.err(f'{self.name} failed for multiple projects; see above')
        raise CommandError(1)

    def _setup_logging(self, args):
        logger = logging.getLogger('west.manifest')

        verbose = min(args.verbose, log.VERBOSE_EXTREME)
        if verbose >= log.VERBOSE_NORMAL:
            level = logging.DEBUG
        else:
            level = logging.INFO

        logger.setLevel(level)
        logger.addHandler(ProjectCommandLogHandler())

class Init(_ProjectCommand):

    def __init__(self):
        super().__init__(
            'init',
            'create a west workspace',
            textwrap.dedent(f'''\
            Creates a west workspace as follows:

              1. Creates a .west directory and clones a manifest repository
                 from a git URL to a temporary subdirectory of .west,
                 .west/<tmpdir>.
              2. Parses the manifest file, .west/<tmpdir>/west.yml.
                 This file's contents can specify manifest.path, the location
                 of the manifest repository in the workspace, like so:

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

            {MANIFEST_URL_DEFAULT}

            This can be overridden using -m.

            The default revision in this repository to check out is
            "{MANIFEST_REV_DEFAULT}"; override with --mr.'''),
            requires_workspace=False)

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
            help='''Directory to create the workspace in (default: current
                 directory). Missing intermediate directories will be created.
                 If -l is given, this is the path to the manifest repository to
                 use instead.''')

        return parser

    def do_run(self, args, ignored):
        if self.topdir:
            zb = os.environ.get('ZEPHYR_BASE')
            if zb:
                msg = textwrap.dedent(f'''
                Note:
                    In your environment, ZEPHYR_BASE is set to:
                    {zb}

                    This forces west to search for a workspace there.
                    Try unsetting ZEPHYR_BASE and re-running this command.''')
            else:
                msg = ''
            self.die_already(self.topdir, msg)

        if args.local and (args.manifest_url or args.manifest_rev):
            log.die('-l cannot be combined with -m or --mr')

        die_if_no_git()

        self._setup_logging(args)

        if args.local:
            topdir = self.local(args)
        else:
            topdir = self.bootstrap(args)

        log.banner(f'Initialized. Now run "west update" inside {topdir}.')

    def die_already(self, where, also=None):
        log.die(f'already initialized in {where}, aborting.{also or ""}')

    def local(self, args):
        if args.manifest_rev is not None:
            log.die('--mr cannot be used with -l')

        manifest_dir = util.canon_path(args.directory or os.getcwd())
        manifest_file = join(manifest_dir, 'west.yml')
        topdir = dirname(manifest_dir)
        rel_manifest = basename(manifest_dir)
        west_dir = os.path.join(topdir, WEST_DIR)

        if not exists(manifest_file):
            log.die(f'can\'t init: no "west.yml" found in {manifest_dir}')

        log.banner('Initializing from existing manifest repository',
                   rel_manifest)
        log.small_banner(f'Creating {west_dir} and local configuration file')
        self.create(west_dir)
        os.chdir(topdir)
        update_config('manifest', 'path', rel_manifest)

        return topdir

    def bootstrap(self, args):
        manifest_url = args.manifest_url or MANIFEST_URL_DEFAULT
        manifest_rev = args.manifest_rev or MANIFEST_REV_DEFAULT
        topdir = util.canon_path(args.directory or os.getcwd())
        west_dir = join(topdir, WEST_DIR)

        try:
            already = util.west_topdir(topdir, fall_back=False)
            self.die_already(already)
        except util.WestNotFound:
            pass

        log.banner('Initializing in', topdir)
        if not isdir(topdir):
            self.create(topdir, exist_ok=False)

        # Clone the manifest repository into a temporary directory.
        tempdir = join(west_dir, 'manifest-tmp')
        if exists(tempdir):
            log.dbg('removing existing temporary manifest directory', tempdir)
            shutil.rmtree(tempdir)
        try:
            self.clone_manifest(manifest_url, manifest_rev, tempdir)
        except subprocess.CalledProcessError:
            shutil.rmtree(tempdir, ignore_errors=True)
            raise

        # Verify the manifest file exists.
        temp_manifest = join(tempdir, 'west.yml')
        if not exists(temp_manifest):
            log.die(f'can\'t init: no "west.yml" found in {tempdir}\n'
                    f'  Hint: check --manifest-url={manifest_url} and '
                    f'--manifest-rev={manifest_rev}\n'
                    f'  You may need to remove {west_dir} before retrying.')

        # Parse the manifest to get the manifest path, if it declares one.
        # Otherwise, use the URL. Ignore imports -- all we really
        # want to know is if there's a "self: path:" or not.
        projects = Manifest.from_file(temp_manifest,
                                      import_flags=ImportFlag.IGNORE,
                                      topdir=topdir).projects
        manifest_project = projects[MANIFEST_PROJECT_INDEX]
        if manifest_project.path:
            manifest_path = manifest_project.path
        else:
            # We use PurePath() here in case manifest_url is a
            # windows-style path. That does the right thing in that
            # case, without affecting POSIX platforms, where PurePath
            # is PurePosixPath.
            manifest_path = PurePath(urlparse(manifest_url).path).name

        manifest_abspath = join(topdir, manifest_path)

        log.dbg('moving', tempdir, 'to', manifest_abspath,
                level=log.VERBOSE_EXTREME)
        try:
            shutil.move(tempdir, manifest_abspath)
        except shutil.Error as e:
            log.die(e)
        log.small_banner('setting manifest.path to', manifest_path)
        update_config('manifest', 'path', manifest_path, topdir=topdir)

        return topdir

    def create(self, directory, exist_ok=True):
        try:
            os.makedirs(directory, exist_ok=exist_ok)
        except PermissionError:
            log.die(f'Cannot initialize in {directory}: permission denied')
        except FileExistsError:
            log.die(f'Something else created {directory} concurrently')
        except Exception as e:
            log.die(f"Can't create {directory}: {e}")

    def check_call(self, args, cwd=None):
        cmd_str = util.quote_sh_list(args)
        log.dbg(f"running '{cmd_str}' in {cwd or os.getcwd()}",
                level=log.VERBOSE_VERY)
        subprocess.check_call(args, cwd=cwd)

    def clone_manifest(self, url, rev, dest, exist_ok=False):
        log.small_banner(f'Cloning manifest repository from {url}, rev. {rev}')
        if not exist_ok and exists(dest):
            log.die(f'refusing to clone into existing location {dest}')

        self.check_call(('git', 'init', dest))
        self.check_call(('git', 'remote', 'add', 'origin', '--', url),
                        cwd=dest)
        maybe_sha = _maybe_sha(rev)
        if maybe_sha:
            # Fetch the ref-space and hope the SHA is contained in
            # that ref-space
            self.check_call(('git', 'fetch', 'origin', '--tags',
                             '--', 'refs/heads/*:refs/remotes/origin/*'),
                            cwd=dest)
        else:
            # Fetch the ref-space similar to git clone plus the ref
            # given by user.  Redundancy is ok, for example if the user
            # specifies 'heads/master'. This allows users to specify:
            # pull/<no>/head for pull requests
            self.check_call(('git', 'fetch', 'origin', '--tags', '--',
                             rev, 'refs/heads/*:refs/remotes/origin/*'),
                            cwd=dest)

        try:
            # Using show-ref to determine if rev is available in local repo.
            self.check_call(('git', 'show-ref', '--', rev), cwd=dest)
            local_rev = True
        except subprocess.CalledProcessError:
            local_rev = False

        if local_rev or maybe_sha:
            self.check_call(('git', 'checkout', rev), cwd=dest)
        else:
            self.check_call(('git', 'checkout', 'FETCH_HEAD'), cwd=dest)

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
            epilog=textwrap.dedent(f'''\
            FORMAT STRINGS
            --------------

            Projects are listed using a Python 3 format string. Arguments
            to the format string are accessed by name.

            The default format string is:

            "{default_fmt}"

            The following arguments are available:

            - name: project name in the manifest
            - url: full remote URL as specified by the manifest
            - path: the relative path to the project from the top level,
              as specified in the manifest where applicable
            - abspath: absolute and normalized path to the project
            - posixpath: like abspath, but in posix style, that is, with '/'
              as the separator character instead of '\\'
            - revision: project's revision as it appears in the manifest
            - sha: project's revision as a SHA. Note that use of this requires
              that the project has been cloned.
            - cloned: "cloned" if the project has been cloned, "not-cloned"
              otherwise
            - clone_depth: project clone depth if specified, "None" otherwise
            '''))
        parser.add_argument('-a', '--all', action='store_true',
                            help='ignored for backwards compatibility'),
        parser.add_argument('-f', '--format', default=default_fmt,
                            help='''format string to use to list each
                            project; see FORMAT STRINGS below.''')

        self._add_projects_arg(parser)

        return parser

    def do_run(self, args, user_args):
        def sha_thunk(project):
            die_if_no_git()

            if not project.is_cloned():
                log.die(f'cannot get sha for uncloned project {project.name}; '
                        f'run "west update {project.name}" and retry')
            elif project.revision:
                return project.sha(MANIFEST_REV)
            else:
                return f'{"N/A":40}'

        def cloned_thunk(project):
            die_if_no_git()

            return "cloned" if project.is_cloned() else "not-cloned"

        def delay(func, project):
            return DelayFormat(partial(func, project))

        self._setup_logging(args)

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
                log.die(f'unknown key "{e.args[0]}" in format string '
                        f'{shlex.quote(args.format)}')
            except IndexError:
                self.parser.print_usage()
                log.die(f'invalid format string {shlex.quote(args.format)}')
            except subprocess.CalledProcessError:
                log.die(f'subprocess failed while listing {project.name}')

            log.inf(result, colorize=False)

class ManifestCommand(_ProjectCommand):
    # The slightly weird naming is to avoid a conflict with
    # west.manifest.Manifest.

    def __init__(self):
        super(ManifestCommand, self).__init__(
            'manifest',
            'manage the west manifest',
            textwrap.dedent('''\
            Manages the west manifest.

            The following actions are available. You must give exactly one.

            - --resolve: print the current manifest with all imports applied,
              as an equivalent single manifest file. Any imported manifests
              must be cloned locally (with "west update").

            - --freeze: like --resolve, but with all project revisions
              converted to their current SHAs, based on the latest manifest-rev
              branches. All projects must be cloned (with "west update").

            - --validate: print an error and exit the process unsuccessfully
              if the current manifest cannot be successfully parsed.
              If the manifest can be parsed, print nothing and exit
              successfully.

            If the manifest file does not use imports, and all project
            revisions are SHAs, the --freeze and --resolve output will
            be identical after a "west update".
            '''),
            accepts_unknown_args=False)

    def do_add_parser(self, parser_adder):
        parser = self._parser(parser_adder)

        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--resolve', action='store_true',
                           help='print the manifest with all imports resolved')
        group.add_argument('--freeze', action='store_true',
                           help='''print the resolved manifest with SHAs for
                           all project revisions''')
        group.add_argument('--validate', action='store_true',
                           help='''validate the current manifest,
                           exiting with an error if there are issues''')

        group = parser.add_argument_group('options for --resolve and --freeze')
        group.add_argument('-o', '--out',
                           help='output file, default is standard output')

        return parser

    def do_run(self, args, user_args):
        self._setup_logging(args)

        # Since the user is explicitly managing the manifest, we are
        # deliberately loading it again instead of using self.manifest
        # to emit debug logs if enabled, which are turned off when the
        # manifest is initially parsed in main.py.
        #
        # The code in main.py is usually responsible for handling any
        # errors and printing useful messages. We re-do error checking
        # for manifest-related errors that it won't handle.
        try:
            manifest = Manifest.from_file(topdir=self.topdir)
        except _ManifestImportDepth:
            log.die("cannot resolve manifest -- is there a loop?")
        except (MalformedManifest, ManifestImportFailed,
                ManifestVersionError) as e:
            log.die('\n  '.join(str(arg) for arg in e.args))
        dump_kwargs = {'default_flow_style': False,
                       'sort_keys': False}

        if args.validate:
            pass              # nothing more to do
        elif args.resolve:
            self._dump(args, manifest.as_yaml(**dump_kwargs))
        elif args.freeze:
            self._dump(args, manifest.as_frozen_yaml(**dump_kwargs))
        else:
            # Can't happen.
            raise RuntimeError(f'internal error: unhandled args {args}')

    def _dump(self, args, to_dump):
        if args.out:
            with open(args.out, 'w') as f:
                f.write(to_dump)
        else:
            sys.stdout.write(to_dump)

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
        die_if_no_git()
        self._setup_logging(args)

        failed = []
        for project in self._cloned_projects(args):
            log.banner(f'diff for {project.name_and_path}:')
            # Use paths that are relative to the base directory to make it
            # easier to see where the changes are
            try:
                project.git(['diff', f'--src-prefix={project.path}/',
                             f'--dst-prefix={project.path}/'])
            except subprocess.CalledProcessError:
                failed.append(project)
        self._handle_failed(args, failed)

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
        die_if_no_git()
        self._setup_logging(args)

        failed = []
        for project in self._cloned_projects(args):
            log.banner(f'status of {project.name_and_path}:')
            try:
                project.git('status', extra_args=user_args)
            except subprocess.CalledProcessError:
                failed.append(project)
        self._handle_failed(args, failed)

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

            You must have already created a west workspace with "west init".

            This command does not alter the manifest repository's contents.''')
        )

    def do_add_parser(self, parser_adder):
        parser = self._parser(parser_adder)

        parser.add_argument('--stats', action='store_true',
                            help='''print performance statistics for
                            update operations''')

        group = parser.add_argument_group(
            title='fetching behavior',
            description='By default, west update tries to avoid fetching.')
        group.add_argument('-f', '--fetch', dest='fetch_strategy',
                           choices=['always', 'smart'],
                           help='''how to fetch projects when updating:
                           "always" fetches every project before update,
                           while "smart" (default) skips fetching projects
                           whose revisions are SHAs or tags available
                           locally''')

        group = parser.add_argument_group(
            title='checked out branch behavior',
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
        die_if_no_git()
        self._setup_logging(args)

        self.args = args
        if args.exclude_west:
            log.wrn('ignoring --exclude-west')

        # We can't blindly call self._projects() here: manifests with
        # imports are limited to plain 'west update', and cannot use
        # 'west update PROJECT [...]'.
        self.fs = self.fetch_strategy(args)
        if not args.projects:
            self.update_all(args)
        else:
            self.update_some(args)

    def update_all(self, args):
        # Plain 'west update' is the 'easy' case: since the user just
        # wants us to update everything, we don't have to keep track
        # of projects appearing or disappearing as a result of fetching
        # new revisions from projects with imports.
        #
        # So we just re-parse the manifest, but force west.manifest to
        # call our importer whenever it encounters an import statement
        # in a project, allowing us to control the recursion so it
        # always uses the latest manifest data.
        self.updated = set()
        self.args = args

        manifest = Manifest.from_file(importer=self.update_importer,
                                      import_flags=ImportFlag.FORCE_PROJECTS)

        failed = []
        for project in manifest.projects:
            if (isinstance(project, ManifestProject) or
                    project.name in self.updated):
                continue
            try:
                self.update(project)
                self.updated.add(project.name)
            except subprocess.CalledProcessError:
                failed.append(project)
        self._handle_failed(args, failed)

    def update_importer(self, project, path):
        if isinstance(project, ManifestProject):
            if not project.is_cloned():
                log.die("manifest repository {project.abspath} was deleted")
        else:
            self.update(project)
        self.updated.add(project.name)

        try:
            return _manifest_content_at(project, path)
        except FileNotFoundError:
            # FIXME we need each project to have back-pointers
            # to the manifest file where it was defined, so we can
            # tell the user better context than just "run -vvv", which
            # is a total fire hose.
            name = project.name
            sha = project.sha(QUAL_MANIFEST_REV)
            if log.VERBOSE < log.VERBOSE_EXTREME:
                suggest_vvv = ('\n'
                               '        Use "west -vvv update" to debug.')
            else:
                suggest_vvv = ''
            log.die(f"can't import from project {name}\n"
                    f'  Expected to import from {path} at revision {sha}\n'
                    f'  Hint: possible manifest file fixes for {name}:\n'
                    f'          - set "revision:" to a git ref with this file '
                    f'at URL {project.url}\n'
                    '          - remove the "import:"' + suggest_vvv)

    def update_some(self, args):
        # The 'west update PROJECT [...]' style invocation is only
        # implemented for projects defined within the manifest
        # repository.
        #
        # It's unclear how to do this properly in the case of
        # a project A whose definition is imported from
        # another project B, especially when B.revision is not
        # a fixed SHA. Do we forcibly need to update B first?
        # Should we skip it? Should it be configurable? Etc.
        #
        # For now, just refuse to do so. We can try to relax
        # this restriction if it proves cumbersome.

        if not self.has_manifest or self.manifest.has_imports:
            projects = self.toplevel_projects(args)
        else:
            projects = self._projects(args.projects)

        failed = []
        for project in projects:
            if isinstance(project, ManifestProject):
                continue
            try:
                self.update(project)
            except subprocess.CalledProcessError:
                failed.append(project)
        self._handle_failed(args, failed)

    def toplevel_projects(self, args):
        # Return a list of projects from args.projects, or scream and
        # die if any projects are either unknown or not defined in the
        # manifest repository.

        ids = args.projects
        assert ids

        mr_projects, mr_unknown = projects_unknown(
            Manifest.from_file(import_flags=ImportFlag.IGNORE_PROJECTS), ids)

        if not mr_unknown:
            return mr_projects

        try:
            manifest = Manifest.from_file()
        except ManifestImportFailed:
            log.die('one or more projects are unknown or defined via '
                    'imports; please run plain "west update".')

        projects, unknown = projects_unknown(manifest, ids)
        if unknown:
            die_unknown(unknown)
        else:
            # All of the ids are known projects, but some of them
            # are not defined in the manifest repository.
            mr_unknown_set = set(mr_unknown)
            from_projects = [p for p in ids if p in mr_unknown_set]
            log.die('refusing to update project: ' +
                    " ".join(from_projects) + '\n' +
                    '  It or they were resolved via project imports.\n'
                    '  Only plain "west update" can currently update them.')

    def fetch_strategy(self, args):
        cfg = config.get('update', 'fetch', fallback=None)
        if cfg is not None and cfg not in ('always', 'smart'):
            log.wrn(f'ignoring invalid config update.fetch={cfg}; '
                    'choices: always, smart')
            cfg = None
        if args.fetch_strategy:
            return args.fetch_strategy
        elif cfg:
            return cfg
        else:
            return 'smart'

    def fetch_missing_imports(self, args):
        self.fs = 'always'      # just to be safe -- TODO needed?
        self.manifest = Manifest.from_file(topdir=self.topdir,
                                           importer=self.update_importer)

    def update(self, project):
        if self.args.stats:
            stats = dict()
            update_start = perf_counter()
        else:
            stats = None
        take_stats = stats is not None

        log.banner(f'updating {project.name_and_path}:')

        # Make sure we've got a project to work with.
        self.ensure_cloned(project, stats, take_stats)

        # Point refs/heads/manifest-rev at project.revision,
        # fetching it from the remote if necessary.
        self.set_new_manifest_rev(project, stats, take_stats)

        # Clean up refs/west/*. At some point, we should only do this
        # if we've fetched, but we're leaving it here to clean up
        # garbage in people's repositories introduced by previous
        # versions of west that left refs in place here.
        self.clean_refs_west(project, stats, take_stats)

        # Make sure HEAD is pointing at *something*.
        self.ensure_head_ok(project, stats, take_stats)

        # Convert manifest-rev to a SHA.
        sha = self.manifest_rev_sha(project, stats, take_stats)

        # Based on the new manifest-rev SHA, HEAD, and the --rebase
        # and --keep-descendants options, decide what we need to do
        # now.
        current_branch, is_ancestor, try_rebase = self.decide_update_strategy(
            project, sha, stats, take_stats)

        # Finish the update. This may be a nop if we're keeping
        # descendants.
        if self.args.keep_descendants and is_ancestor:
            # A descendant is currently checked out and keep_descendants was
            # given, so there's nothing more to do.
            log.inf(f'west update: left descendant branch '
                    f'"{current_branch}" checked out; current status:')
            if take_stats:
                start = perf_counter()
            project.git('status')
            if take_stats:
                stats['get current status'] = perf_counter - start
        elif try_rebase:
            # Attempt a rebase.
            log.inf(f'west update: rebasing to {MANIFEST_REV} {sha}')
            if take_stats:
                start = perf_counter()
            project.git('rebase ' + QUAL_MANIFEST_REV)
            if take_stats:
                stats['rebase onto new manifest-rev'] = perf_counter() - start
        else:
            # We can't keep a descendant or rebase, so just check
            # out the new detached HEAD, then print some helpful context.
            if take_stats:
                start = perf_counter()
            project.git('checkout --detach ' + sha)
            if take_stats:
                stats['checkout new manifest-rev'] = perf_counter() - start
            _post_checkout_help(project, current_branch, sha, is_ancestor)

        # Print performance statistics.
        if take_stats:
            update_total = perf_counter() - update_start
            slop = update_total - sum(stats.values())
            stats['other work'] = slop
            stats['TOTAL'] = update_total
            log.inf('performance statistics:')
            for stat, value in stats.items():
                log.inf(f'  {stat}: {value} sec')

    @staticmethod
    def ensure_cloned(project, stats, take_stats):
        # update() helper. Make sure project is cloned and initialized.

        if take_stats:
            start = perf_counter()
        cloned = project.is_cloned()
        if take_stats:
            stats['check if cloned'] = perf_counter() - start
        if not cloned:
            if take_stats:
                start = perf_counter()
            _init_project(project)
            if take_stats:
                stats['init'] = perf_counter() - start

    def set_new_manifest_rev(self, project, stats, take_stats):
        # update() helper. Make sure project's manifest-rev is set to
        # the latest value it should be.

        if self.fs == 'always' or _rev_type(project) not in ('tag', 'commit'):
            if take_stats:
                start = perf_counter()
            _fetch(project)
            if take_stats:
                stats['fetch and set manifest-rev'] = perf_counter() - start
        else:
            log.dbg('skipping unnecessary fetch')
            if take_stats:
                start = perf_counter()
            _update_manifest_rev(project, f'{project.revision}^{{commit}}')
            if take_stats:
                stats['set manifest-rev'] = perf_counter() - start

    @staticmethod
    def clean_refs_west(project, stats, take_stats):
        # update() helper. Make sure refs/west/* is empty after
        # setting the new manifest-rev.
        #
        # Head of manifest-rev is now pointing to current manifest revision.
        # Thus it is safe to unconditionally clear out the refs/west space.
        #
        # Doing this here instead of in _fetch() ensures that it gets cleaned
        # up when users upgrade from older versions of west (like 0.6.x) that
        # didn't handle this properly.
        # In future, this can be moved into _fetch() after the install base of
        # older west versions is expected to be smaller.
        if take_stats:
            start = perf_counter()
        _clean_west_refspace(project)
        if take_stats:
            stats['clean up refs/west/*'] = perf_counter() - start

    @staticmethod
    def ensure_head_ok(project, stats, take_stats):
        # update() helper. Ensure HEAD points at something reasonable.

        if take_stats:
            start = perf_counter()
        head_ok = _head_ok(project)
        if take_stats:
            stats['check HEAD is ok'] = perf_counter() - start
        if not head_ok:
            # If nothing is checked out (which usually only happens if
            # we called _init_project(project) above), check out
            # 'manifest-rev' in a detached HEAD state.
            #
            # Otherwise, the initial state would have nothing checked
            # out, and HEAD would point to a non-existent
            # refs/heads/master branch (that would get created if the
            # user makes an initial commit). Among other things, this
            # ensures the rev-parse --abbrev-ref HEAD which happens
            # later in the update() will always succeed.
            #
            # The --detach flag is strictly redundant here, because
            # the refs/heads/<branch> form already detaches HEAD, but
            # it avoids a spammy detached HEAD warning from Git.
            if take_stats:
                start = perf_counter()
            project.git('checkout --detach ' + QUAL_MANIFEST_REV)
            if take_stats:
                stats['checkout new manifest-rev'] = perf_counter() - start

    @staticmethod
    def manifest_rev_sha(project, stats, take_stats):
        # update() helper. Get the SHA for manifest-rev.

        try:
            if take_stats:
                start = perf_counter()
            return project.sha(QUAL_MANIFEST_REV)
            if take_stats:
                stats['get new manifest-rev SHA'] = perf_counter() - start
        except subprocess.CalledProcessError:
            # This is a sign something's really wrong. Add more help.
            log.err(f'no SHA for branch {MANIFEST_REV} '
                    f'in {project.name_and_path}; was the branch deleted?')
            raise

    def decide_update_strategy(self, project, sha, stats, take_stats):
        # update() helper. Decide on whether we have an ancestor
        # branch or whether we should try to rebase.

        if take_stats:
            start = perf_counter()
        cp = project.git('rev-parse --abbrev-ref HEAD', capture_stdout=True)
        if take_stats:
            stats['get current branch HEAD'] = perf_counter() - start
        current_branch = cp.stdout.decode('utf-8').strip()
        if current_branch != 'HEAD':
            if take_stats:
                start = perf_counter()
            is_ancestor = project.is_ancestor_of(sha, current_branch)
            if take_stats:
                stats['check if HEAD is ancestor of manifest-rev'] = \
                    perf_counter() - start
            try_rebase = self.args.rebase
        else:  # HEAD means no branch is checked out.
            # If no branch is checked out, 'rebase' and
            # 'keep_descendants' don't matter.
            is_ancestor = False
            try_rebase = False

        return current_branch, is_ancestor, try_rebase

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
        parser.add_argument('-c', dest='subcommand', metavar='COMMAND',
                            required=True)
        self._add_projects_arg(parser)
        return parser

    def do_run(self, args, user_args):
        self._setup_logging(args)

        failed = []
        for project in self._cloned_projects(args):
            log.banner(
                f'running "{args.subcommand}" in {project.name_and_path}:')
            rc = subprocess.Popen(args.subcommand, shell=True,
                                  cwd=project.abspath).wait()
            if rc:
                failed.append(project)
        self._handle_failed(args, failed)

class Topdir(_ProjectCommand):
    def __init__(self):
        super().__init__(
            'topdir',
            'print the top level directory of the workspace',
            textwrap.dedent('''\
            Prints the absolute path of the current west workspace's
            top directory.

            This is the directory containing .west. All project
            paths in the manifest are relative to this top directory.'''))

    def do_add_parser(self, parser_adder):
        return self._parser(parser_adder)

    def do_run(self, args, user_args):
        log.inf(PurePath(self.topdir).as_posix())

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

#
# Private helper routines.
#

def _clean_west_refspace(project):
    # Clean the refs/west space to ensure they do not show up in 'git log'.

    # Get all the ref names that start with refs/west/.
    list_refs_cmd = ('for-each-ref --format="%(refname)" -- ' +
                     QUAL_REFS + '**')
    cp = project.git(list_refs_cmd, capture_stdout=True)
    west_references = cp.stdout.decode('utf-8').strip()

    # Safely delete each one.
    for ref in west_references.splitlines():
        delete_ref_cmd = 'update-ref -d ' + ref
        project.git(delete_ref_cmd)

def _update_manifest_rev(project, new_manifest_rev):
    project.git(['update-ref',
                 '-m', f'west update: moving to {new_manifest_rev}',
                 QUAL_MANIFEST_REV, new_manifest_rev])

def _maybe_sha(rev):
    # Return true if and only if the given revision might be a SHA.

    try:
        int(rev, 16)
    except ValueError:
        return False

    return len(rev) <= 40

def _init_project(project):
    log.small_banner(f'{project.name}: initializing')
    project.git(['init', project.abspath], cwd=util.west_topdir())
    # This remote is added as a convenience for the user.
    # However, west always fetches project data by URL, not remote name.
    # The user is therefore free to change the URL of this remote.
    project.git(f'remote add -- {project.remote_name} {project.url}')

def _rev_type(project, rev=None):
    # Returns a "refined" revision type of rev (default:
    # project.revision) as one of the following strings: 'tag', 'tree',
    # 'blob', 'commit', 'branch', 'other'.
    #
    # The approach combines git cat-file -t and git rev-parse because,
    # while cat-file can for sure tell us a blob, tree, or tag, it
    # doesn't have a way to disambiguate between branch names and
    # other types of commit-ishes, like SHAs, things like "HEAD" or
    # "HEAD~2", etc.
    #
    # We need this extra layer of refinement to be able to avoid
    # fetching SHAs that are already available locally.
    #
    # This doesn't belong in manifest.py because it contains "west
    # update" specific logic.
    if not rev:
        rev = project.revision
    cp = project.git(['cat-file', '-t', rev], check=False,
                     capture_stdout=True, capture_stderr=True)
    stdout = cp.stdout.decode('utf-8').strip()
    if cp.returncode:
        return 'other'
    elif stdout in ('blob', 'tree', 'tag'):
        return stdout
    elif stdout != 'commit':    # just future-proofing
        return 'other'

    # to tell branches apart from commits, we need rev-parse.
    cp = project.git(['rev-parse', '--verify', '--symbolic-full-name', rev],
                     check=False, capture_stdout=True, capture_stderr=True)
    if cp.returncode:
        # This can happen if the ref name is ambiguous, e.g.:
        #
        # $ git update-ref ambiguous-ref HEAD~2
        # $ git checkout -B ambiguous-ref
        #
        # Which creates both .git/ambiguous-ref and
        # .git/refs/heads/ambiguous-ref.
        return 'other'

    stdout = cp.stdout.decode('utf-8').strip()
    if stdout.startswith('refs/heads'):
        return 'branch'
    elif not stdout:
        return 'commit'
    else:
        return 'other'

def _fetch(project, rev=None):
    # Fetches rev (or project.revision) from project.url in a way that
    # guarantees any branch, tag, or SHA (that's reachable from a
    # branch or a tag) available on project.url is part of what got
    # fetched.
    #
    # Returns a git revision which hopefully can be peeled to the
    # newly-fetched SHA corresponding to rev. "Hopefully" because
    # there are many ways to spell a revision, and they haven't all
    # been extensively tested.

    if not rev:
        rev = project.revision

    # Fetch the revision into the local ref space.
    #
    # The following two-step approach avoids a "trying to write
    # non-commit object" error when the revision is an annotated
    # tag. ^{commit} type peeling isn't supported for the <src> in a
    # <src>:<dst> refspec, so we have to do it separately.
    msg = f'{project.name}: fetching, need revision {rev}'
    if project.clone_depth:
        msg += f' with --depth {project.clone_depth}'
        depth = ['--depth', str(project.clone_depth)]
    else:
        depth = []
    if _maybe_sha(rev):
        # We can't in general fetch a SHA from a remote, as many hosts
        # (GitHub included) forbid it for security reasons. Let's hope
        # it's reachable from some branch.
        refspec = f'refs/heads/*:{QUAL_REFS}*'
        next_manifest_rev = project.revision
    else:
        # The revision is definitely not a SHA, so it's safe to fetch directly.
        # This avoids fetching unnecessary refs from the remote.
        #
        # We update manifest-rev to FETCH_HEAD instead of using a
        # refspec in case the revision is a tag, which we can't use
        # from a refspec.
        refspec = project.revision
        next_manifest_rev = 'FETCH_HEAD^{commit}'

    # -f is needed to avoid errors in case multiple remotes are present,
    # at least one of which contains refs that can't be fast-forwarded to our
    # local ref space.
    #
    # --tags is required to get tags, since the remote is specified as a URL.
    log.small_banner(msg)
    project.git(['fetch', '-f', '--tags'] + depth +
                ['--', project.url, refspec])
    _update_manifest_rev(project, next_manifest_rev)

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
    return project.git('show-ref --quiet --head /',
                       check=False).returncode == 0

def _post_checkout_help(project, branch, sha, is_ancestor):
    # Print helpful information to the user about a project that
    # might have just left a branch behind.

    if branch == 'HEAD':
        # If there was no branch checked out, there are no
        # additional diagnostics that need emitting.
        return

    rel = relpath(project.abspath)
    if is_ancestor:
        # If the branch we just left behind is a descendant of
        # the new HEAD (e.g. if this is a topic branch the
        # user is working on and the remote hasn't changed),
        # print a message that makes it easy to get back,
        # no matter where in the workspace os.getcwd() is.
        log.wrn(f'left behind {project.name} branch "{branch}"; '
                f'to switch back to it (fast forward), use: '
                f'git -C {rel} checkout {branch}')
        log.dbg('(To do this automatically in the future,',
                'use "west update --keep-descendants".)')
    else:
        # Tell the user how they could rebase by hand, and
        # point them at west update --rebase.
        log.wrn(f'left behind {project.name} branch "{branch}"; '
                f'to rebase onto the new HEAD: '
                f'git -C {rel} rebase {sha} {branch}')
        log.dbg('(To do this automatically in the future,',
                'use "west update --rebase".)')

def projects_unknown(manifest, projects):
    # Retrieve the projects with get_projects(project,
    # only_cloned=False). Return a pair: (projects, unknown)
    # containing either a projects list and None or None and a list of
    # unknown project IDs.

    try:
        return (manifest.get_projects(projects, only_cloned=False), None)
    except ValueError as ve:
        if len(ve.args) != 2:
            raise          # not directly raised by get_projects()
        unknown = ve.args[0]
        if not unknown:
            raise   # only_cloned is False, so this "can't happen"
        return (None, unknown)

def die_unknown(unknown):
    # Scream and die about unknown projects.

    s = 's' if len(unknown) > 1 else ''
    names = ' '.join(unknown)
    log.die(f'unknown project name{s}/path{s}: {names}\n'
            '  Hint: use "west list" to list all projects.')

@lru_cache(maxsize=1)
def warn_once_if_no_git():
    # Using an LRU cache means this gets called once. Afterwards, the
    # memoized return value (None) is simply returned from the cache,
    # so the warning is emitted only once per process invocation.
    if shutil.which('git') is None:
        log.wrn('git is not installed or cannot be found; this may fail')

@lru_cache(maxsize=1)
def die_if_no_git():
    # Using an LRU cache means this only calls shutil.which() once.
    # This is useful when the function is called multiple times, e.g.
    # from the west list thunk for computing a SHA.
    if shutil.which('git') is None:
        log.die("can't find git; install it or ensure it's on your PATH")

#
# Special files and directories in the west workspace.
#
# These are given variable names for clarity, but they can't be
# changed without propagating the changes into west itself.
#

# Top-level west directory, containing west itself and the manifest.
WEST_DIR = '.west'

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

#
# Logging helpers
#


class ProjectCommandLogFormatter(logging.Formatter):

    def __init__(self):
        super().__init__(fmt='%(name)s: %(message)s')


class ProjectCommandLogHandler(logging.Handler):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFormatter(ProjectCommandLogFormatter())

    def emit(self, record):
        fmt = self.format(record)
        lvl = record.levelno
        if lvl > logging.CRITICAL:
            log.die(fmt)
        elif lvl >= logging.ERROR:
            log.err(fmt)
        elif lvl >= logging.WARNING:
            log.wrn(fmt)
        elif lvl >= logging.INFO:
            log.inf(fmt)
        elif lvl >= logging.DEBUG:
            log.dbg(fmt)
        else:
            log.dbg(fmt, level=log.VERBOSE_EXTREME)
