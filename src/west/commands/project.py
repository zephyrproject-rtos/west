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
                log.die('Unknown project name{s}/path{s}: {unknown} '
                        '(use "west list" to list all projects)'
                        .format(s='s' if len(unknown) > 1 else '',
                                unknown=', '.join(unknown)))
            elif only_cloned and uncloned:
                plural = len(uncloned) > 1
                names = [p.name for p in uncloned]
                log.die(textwrap.dedent('''\
                The following project{} not cloned: {}.
                Please clone {} first, with:
                    west update {}
                then retry.'''.format('s are' if plural else ' is',
                                      ", ".join(names),
                                      'them' if plural else 'it',
                                      " ".join(names))))
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
            log.err('{command} failed for project{s} {projects}'.
                    format(command=self.name,
                           s='s:' if len(failed) > 1 else '',
                           projects=', '.join(p.format('{name}')
                                              for p in failed)))
        else:
            log.err('{command} failed for multiple projects; see above')
        raise CommandError(1)

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

        log.banner('Initialized. Now run "west update" inside {}.'.
                   format(self.topdir))

    def local(self, args):
        if args.manifest_rev is not None:
            log.die('--mr cannot be used with -l')

        manifest_dir = util.canon_path(args.directory or os.getcwd())
        manifest_file = join(manifest_dir, 'west.yml')
        topdir = dirname(manifest_dir)
        rel_manifest = basename(manifest_dir)
        west_dir = os.path.join(topdir, WEST_DIR)

        log.banner('Initializing from existing manifest repository',
                   rel_manifest)
        if not exists(manifest_file):
            log.die('No "west.yml" found in {}'.format(manifest_dir))

        self.create(west_dir)
        os.chdir(topdir)
        # This validates the manifest. Note we cannot use
        # self.manifest from west init, as we are in the middle of
        # creating the installation right now.
        projects = self.projects(manifest_file)
        log.small_banner('Creating {} and local configuration'.
                         format(west_dir))
        update_config('manifest', 'path', rel_manifest)

        self.topdir = topdir

        return projects, manifest_dir

    def bootstrap(self, args):
        manifest_url = args.manifest_url or MANIFEST_URL_DEFAULT
        manifest_rev = args.manifest_rev or MANIFEST_REV_DEFAULT
        topdir = util.canon_path(args.directory or os.getcwd())
        west_dir = join(topdir, WEST_DIR)

        log.banner('Initializing in', topdir)
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
        log.small_banner('Cloning manifest repository from {}, rev. {}'.
                         format(url, rev))
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
            - sha: project's revision as a SHA. Note that use of this requires
              that the project has been cloned.
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
            if not project.is_cloned():
                log.die('cannot get sha for uncloned project {0}; '
                        'run "west update {0}" and retry'.
                        format(project.name))
            elif project.revision:
                return project.sha(MANIFEST_REV)
            else:
                return '{:40}'.format('N/A')

        def cloned_thunk(project):
            return "cloned" if project.is_cloned() else "not-cloned"

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
            except subprocess.CalledProcessError:
                log.die('subprocess error while listing project',
                        project.name)

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

            The --freeze operation outputs the manifest with all
            project-related values fully specified: defaults are
            applied, and all revisions are converted to SHAs based on
            the current manifest-rev branches. Note that all projects
            must be cloned for this to work.

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
        try:
            frozen = Manifest.from_file().as_frozen_dict()
        except RuntimeError as re:
            log.die(*(list(re.args) + ['(run "west update" and retry)']))

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
        failed = []
        for project in self._cloned_projects(args):
            log.banner(project.format('diff for {name_and_path}:'))
            # Use paths that are relative to the base directory to make it
            # easier to see where the changes are
            try:
                project.git('diff --src-prefix={path}/ --dst-prefix={path}/')
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
        failed = []
        for project in self._cloned_projects(args):
            log.banner(project.format('status of {name_and_path}:'))
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

            You must have already created a west installation with "west init".

            This command does not alter the manifest repository's contents.''')
        )

    def do_add_parser(self, parser_adder):
        parser = self._parser(parser_adder)

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
        if args.exclude_west:
            log.wrn('ignoring --exclude-west')
        fs = self._fetch_strategy(args)

        failed = []
        for project in self._projects(args.projects):
            if isinstance(project, ManifestProject):
                continue
            log.banner(project.format('updating {name_and_path}:'))
            try:
                _update(project, fs, args.rebase, args.keep_descendants)
            except subprocess.CalledProcessError:
                failed.append(project)
        self._handle_failed(args, failed)

    def _fetch_strategy(self, args):
        cfg = config.get('update', 'fetch', fallback=None)
        if cfg is not None and cfg not in ('always', 'smart'):
            log.wrn('ignoring invalid config update.fetch={}; '
                    'choices: always, smart'.format(cfg))
            cfg = None
        if args.fetch_strategy:
            return args.fetch_strategy
        elif cfg:
            return cfg
        else:
            return 'smart'


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
        failed = []
        for project in self._cloned_projects(args):
            log.banner(project.format('running "{c}" in {name_and_path}:',
                                      c=args.command))
            rc = subprocess.Popen(args.command, shell=True,
                                  cwd=project.abspath).wait()
            if rc:
                failed.append(project)
        self._handle_failed(args, failed)


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

def _maybe_sha(rev):
    # Return true if and only if the given revision might be a SHA.

    try:
        int(rev, 16)
    except ValueError:
        return False

    return len(rev) <= 40


def _update(project, fetch, rebase, keep_descendants):
    if not project.is_cloned():
        _clone(project)

    if fetch == 'always' or _rev_type(project) not in ('tag', 'commit'):
        _fetch(project)
    else:
        log.dbg('skipping unnecessary fetch')
        project.git('update-ref ' + QUAL_MANIFEST_REV +
                    ' {revision}^{{commit}}')

    try:
        sha = project.sha(QUAL_MANIFEST_REV)
    except subprocess.CalledProcessError:
        # This is a sign something's really wrong. Add more help.
        log.err(project.format("no SHA for branch {mr} in {name_and_path}; "
                               'was the branch deleted?', mr=MANIFEST_REV))
        raise

    cp = project.git('rev-parse --abbrev-ref HEAD', capture_stdout=True)
    current_branch = cp.stdout.decode('utf-8').strip()
    if current_branch != 'HEAD':
        is_ancestor = project.is_ancestor_of(sha, current_branch)
        try_rebase = rebase
    else:  # HEAD means no branch is checked out.
        # If no branch is checked out, 'rebase' and 'keep_descendants' don't
        # matter.
        is_ancestor = False
        try_rebase = False

    if keep_descendants and is_ancestor:
        # A descendant is currently checked out and keep_descendants was
        # given, so there's nothing more to do.
        log.small_banner(project.format(
            '{name}: left descendant branch "{b}" checked out',
            b=current_branch))
    elif try_rebase:
        # Attempt a rebase.
        log.small_banner(project.format('{name}: rebasing to ' + MANIFEST_REV))
        project.git('rebase ' + QUAL_MANIFEST_REV)
    else:
        # We can't keep a descendant or rebase, so just check
        # out the new detached HEAD, then print some helpful context.
        project.git('checkout --detach --quiet ' + sha)
        log.small_banner(project.format(
            "{name}: checked out {r} as detached HEAD", r=sha))
        _post_checkout_help(project, current_branch, sha, is_ancestor)


def _clone(project):
    log.small_banner(project.format('{name}: cloning and initializing'))
    project.git('init {abspath}', cwd=util.west_topdir())
    # This remote is only added for the user's convenience. We always fetch
    # directly from the URL specified in the manifest.
    project.git('remote add -- {remote_name} {url}')


def _rev_type(project):
    # Returns a "refined" revision type of project.revision as of the
    # following strings: 'tag', 'tree', 'blob', 'commit', 'branch',
    # 'other'.
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

    cp = project.git('cat-file -t {revision}', check=False,
                     capture_stdout=True, capture_stderr=True)
    stdout = cp.stdout.decode('utf-8').strip()
    if cp.returncode:
        return 'other'
    elif stdout in ('blob', 'tree', 'tag'):
        return stdout
    elif stdout != 'commit':    # just future-proofing
        return 'other'

    # to tell branches apart from commits, we need rev-parse.
    cp = project.git('rev-parse --verify --symbolic-full-name {revision}',
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


def _fetch(project):
    # Fetches upstream changes for 'project' and updates the 'manifest-rev'
    # branch to point to the revision specified in the manifest.

    # Fetch the revision specified in the manifest into the local ref space.
    #
    # The following two-step approach avoids a "trying to write
    # non-commit object" error when the revision is an annotated
    # tag. ^{commit} type peeling isn't supported for the <src> in a
    # <src>:<dst> refspec, so we have to do it separately.
    msg = "{name}: fetching, need revision {revision}"
    if project.clone_depth:
        msg += " with --depth {clone_depth}"
    # -f is needed to avoid errors in case multiple remotes are present,
    # at least one of which contains refs that can't be fast-forwarded to our
    # local ref space.
    #
    # --tags is required to get tags when the remote is specified as a URL.
    fetch_cmd = ('fetch -f --tags' +
                 ('--depth={clone_depth} ' if project.clone_depth else ' ') +
                 '-- {url} ')
    update_cmd = 'update-ref ' + QUAL_MANIFEST_REV + ' '
    if _maybe_sha(project.revision):
        # Don't fetch a SHA directly, as server may restrict from doing so.
        fetch_cmd += 'refs/heads/*:refs/west/*'
        update_cmd += '{revision}'
    else:
        # The revision is definitely not a SHA, so it's safe to fetch directly.
        # This avoids fetching unnecessary ref space from the remote.
        fetch_cmd += '{revision}'
        update_cmd += 'FETCH_HEAD^{{commit}}'

    log.small_banner(project.format(msg))
    project.git(fetch_cmd)
    project.git(update_cmd)

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
        project.git('checkout --detach ' + QUAL_MANIFEST_REV)


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
