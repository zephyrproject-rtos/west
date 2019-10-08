# Copyright (c) 2018, Nordic Semiconductor ASA
# Copyright 2018, 2019 Foundries.io Ltd
#
# SPDX-License-Identifier: Apache-2.0

'''Parser and abstract data types for west manifests.

The main class is Manifest. The recommended method for creating a
Manifest instance is via its ``from_file()`` or ``from_data()`` helper
methods.

There are additionally Defaults, Remote, and Project types defined,
which represent the values by the same names in a west
manifest. (I.e. "Remote" represents one of the elements in the
"remote" sequence in the manifest, and so on.) Some Default values,
such as the default project revision, may be supplied by this module
if they are not present in the manifest data.'''

import collections
import configparser
import errno
from functools import lru_cache
import os
from pathlib import PurePath
import shutil
import shlex
import subprocess

import pykwalify.core
import yaml

from west import util, log
from west.backports import CompletedProcess
import west.configuration as cfg


#: Index in projects where the project with contains project manifest file is
#: located
MANIFEST_PROJECT_INDEX = 0

#: The name of the branch that points to the revision specified in the
#: manifest
MANIFEST_REV_BRANCH = 'manifest-rev'

#: A qualified reference to MANIFEST_REV_BRANCH, i.e.
#: refs/heads/manifest-rev.
QUAL_MANIFEST_REV_BRANCH = 'refs/heads/' + MANIFEST_REV_BRANCH


def manifest_path():
    '''Return the path to the manifest file.

    Raises: WestNotFound if called from outside of a west installation,
    MalformedConfig if the configuration file is missing a manifest.path key,
    and FileNotFoundError if the manifest.path file doesn't exist.'''
    ret = os.path.join(util.west_topdir(), _mpath(), 'west.yml')
    # It's kind of annoying to manually instantiate a FileNotFoundError.
    # This seems to be the best way.
    if not os.path.isfile(ret):
        raise OSError(errno.ENOENT, os.strerror(errno.ENOENT), ret)
    return ret


class Manifest:
    '''Represents the contents of a West manifest file.

    The most convenient way to construct an instance is using the
    from_file and from_data helper methods.'''

    @staticmethod
    def from_file(source_file=None, topdir=None):
        '''Create and return a new Manifest object given a source YAML file.

        :param source_file: Path to a YAML file containing the manifest.
        :param topdir: If given, the returned Manifest has a project
                       hierarchy rooted at this directory.

        If neither *source_file* nor *topdir* is given, a search is
        performed in the filesystem for a west installation, which is
        used as topdir. Its manifest.path configuration option is used
        to find source_file, ``topdir/<manifest.path>/west.yml``.

        If only *source_file* is given, the search for the
        corresponding topdir is done starting from its parent
        directory.  This directory containing *source_file* does NOT
        have to be manifest.path in this case, allowing parsing of
        additional manifest files besides the "main" one in an
        installation.

        If only *topdir* is given, that directory must be a west
        installation root, and its manifest.path will be used to find
        the source file.

        If both *source_file* and *topdir* are given, the returned
        Manifest object is based on the data in *source_file*, rooted
        at *topdir*. The configuration files are not read in this
        case.  This allows parsing a manifest file "as if" its project
        hierarchy were rooted at another location in the system.

        Exceptions raised:

        - `west.util.WestNotFound` if a .west directory is
          needed but cannot be found.
        - `MalformedManifest` in case of validation errors.
        - `MalformedConfig` in case of a missing manifest.path
          configuration option or otherwise malformed configuration.
        - `ValueError` if topdir is not a west installation root
          and needs to be

        '''
        if source_file is None and topdir is None:
            # neither source_file nor topdir: search the filesystem
            # for the installation and use its manifest.path.
            topdir = util.west_topdir()
            source_file = _west_yml(topdir)
            return Manifest(source_file=source_file, topdir=topdir)
        elif source_file is not None and topdir is None:
            # just source_file: find topdir starting there.
            # fall_back is the default value -- this is just for emphasis.
            topdir = util.west_topdir(start=os.path.dirname(source_file),
                                      fall_back=True)
        elif topdir is not None and source_file is None:
            # just topdir. verify it's a real west installation root.
            msg = 'topdir {} is not a west installation root'.format(topdir)
            try:
                real_topdir = util.west_topdir(start=topdir, fall_back=False)
            except util.WestNotFound:
                raise ValueError(msg)
            if PurePath(topdir) != PurePath(real_topdir):
                raise ValueError(msg + '; but {} is'.format(real_topdir))
            # find west.yml based on manifest.path.
            source_file = _west_yml(topdir)
        else:
            # both source_file and topdir. nothing more to do, but
            # let's check the invariant.
            assert source_file
            assert topdir

        return Manifest(source_file=source_file, topdir=topdir)

    @staticmethod
    def from_data(source_data, manifest_path=None, topdir=None):
        '''Create and return a new Manifest object given parsed YAML data.

        :param source_data: Parsed YAML data as a Python object.
        :param manifest_path: fallback ManifestProject path attribute
        :param topdir: If given, absolute paths in the result will be
                       rooted here.

        This factory allows construction of Manifest objects
        without needing an on-disk west installation to exist.
        This can be useful when creating new ones, for example,
        or thinking about what a new one would look like.
        It does not read any west configuration files on disk.

        Unless *topdir* is given, any values in the returned `Manifest`
        which are absolute paths will be None. Relative paths, such as
        `Project` path attributes, will be parsed from the manifest.

        If ``source_data['manifest']['self']['path']`` is not set, the
        `ManifestProject` path attribute in the returned Manifest is
        *manifest_path*.

        Raises MalformedManifest in case of validation errors.
        '''
        return Manifest(source_data=source_data, topdir=topdir,
                        manifest_path=manifest_path)

    def __init__(self, source_file=None, source_data=None,
                 manifest_path=None, topdir=None):
        '''Create a new Manifest object.

        :param source_file: Path to a YAML file containing the manifest.
        :param source_data: Parsed YAML data as a Python object.
        :param manifest_path: fallback ManifestProject path attribute if
                              *source_data* is given, ignored otherwise
        :param topdir: If given, absolute paths in the manifest will be
                       rooted here, as if *topdir* contained the .west
                       directory.

        It is usually more convenient to use the `from_file` and
        `from_data` factories than to call this constructor directly.

        Exactly one of the *source_file* and *source_data* parameters must
        be given.

        If *source_file* is given:

        - If *topdir* is also given, the Project hierarchy will be
          rooted there.
        - Otherwise, topdir is discovered by searching the file system
          for a west installation root, starting at the directory containing
          *source_file*. The Project hierarchy will be rooted at the discovered
          location.

        Otherwise (*source_data* must be given):

        - If *topdir* is given, the Project hierarchy is rooted there.
        - Otherwise, the hierarchy will have no root: all absolute path
          attributes will be None.

        If *source_data* does not specify the manifest repository
        path, *manifest_path* is the fallback value if given.

        - `MalformedManifest` in case of validation errors.
        - `WestNotFound` in case a west installation search was
          performed, and failed.
        - `ValueError`: on invalid arguments
        '''
        if source_file and source_data:
            raise ValueError('both source_file and source_data were given')

        self.path = None
        '''Path to the file containing the manifest, or None if created
        from data rather than the file system.'''

        if source_file:
            with open(source_file, 'r') as f:
                self._data = yaml.safe_load(f.read())
            self.path = os.path.abspath(source_file)
        else:
            self._data = source_data

        if not self._data:
            self._malformed('manifest contains no data')

        if self._data.get('manifest') is None:
            self._malformed('manifest contains no manifest element')
        data = self._data['manifest']

        try:
            pykwalify.core.Core(source_data=data,
                                schema_files=[_SCHEMA_PATH]).validate()
        except pykwalify.errors.SchemaError as se:
            self._malformed(se._msg, parent=se)

        self.defaults = None
        '''west.manifest.Defaults object representing default values
        in the manifest, either as specified by the user or west itself.'''

        self.remotes = None
        '''Sequence of west.manifest.Remote objects representing manifest
        remotes. Note that not all projects have a remote.'''

        self.projects = None
        '''Sequence of west.manifest.Project objects representing manifest
        projects.

        Each element's values are fully initialized; there is no need
        to consult the defaults field to supply missing values.

        Note: The index MANIFEST_PROJECT_INDEX in sequence will hold the
        project which contains the project manifest file.'''

        # Set up the public attributes documented above, as well as
        # any internal attributes needed to implement the public API.
        self._load(self._data, topdir, manifest_path)

    def get_remote(self, name):
        '''Get a manifest Remote, given its name.'''
        return self._remotes_dict[name]

    def get_projects(self, project_ids, allow_paths=True, only_cloned=False):
        '''Get a list of Projects in the manifest from given *project_ids*.

        If *project_ids* is empty, a list containing all the
        manifest's projects is returned. The manifest is present
        as a project in this list at *MANIFEST_PROJECT_INDEX*, and the
        other projects follow in the order they appear in the manifest
        file.

        Otherwise, projects in the returned list are in the same order
        as specified in *project_ids*.

        Either of these situations raises a ValueError:

        - One or more non-existent projects is in *project_ids*
        - only_cloned is True, and the returned list would have
          contained one or more uncloned projects

        On error, the *args* attribute of the ValueError is a 2-tuple,
        containing a list of unknown project_ids at index 0, and a
        list of uncloned Projects at index 1.

        :param project_ids: A sequence of projects, identified by name
                            (at first priority) or path (as a
                            fallback, when allow_paths=True).
        :param allow_paths: If False, project_ids must be a sequence of
                            project names only; paths are not allowed.
        :param only_cloned: If True, ValueError is raised if an
                            uncloned project would have been returned.

        '''
        projects = list(self.projects)
        unknown = []   # all project_ids which don't resolve to a Project
        uncloned = []  # if only_cloned, resolved Projects which aren't cloned
        ret = []       # result list of resolved Projects

        # If no project_ids are specified, use all projects.
        if not project_ids:
            if only_cloned:
                uncloned = [p for p in projects if not p.is_cloned()]
                if uncloned:
                    raise ValueError(unknown, uncloned)
            return projects

        # Otherwise, resolve each of the project_ids to a project,
        # returning the result or raising ValueError.
        for pid in project_ids:
            project = self._proj_name_map.get(pid)
            if project is None and allow_paths:
                project = self._proj_canon_path_map.get(util.canon_path(pid))

            if project is None:
                unknown.append(pid)
            else:
                ret.append(project)

                if only_cloned and not project.is_cloned():
                    uncloned.append(project)

        if unknown or (only_cloned and uncloned):
            raise ValueError(unknown, uncloned)
        return ret

    def as_frozen_dict(self):
        '''Returns an OrderedDict representing this manifest, frozen.

        The manifest is 'frozen' in that all Git revisions in the
        original data are replaced with the corresponding SHAs.

        Note that this requires that all projects are checked out.
        '''
        # Build a 'frozen' representation of all projects, except the
        # manifest project.
        projects = list(self.projects)
        del projects[MANIFEST_PROJECT_INDEX]
        frozen_projects = []
        for project in projects:
            if not project.is_cloned():
                raise RuntimeError('cannot freeze; project {} is uncloned'.
                                   format(project.name))
            try:
                sha = project.sha(QUAL_MANIFEST_REV_BRANCH)
            except subprocess.CalledProcessError as e:
                raise RuntimeError('cannot freeze; project {} ref {} '
                                   'cannot be resolved to a SHA'.
                                   format(project.name,
                                          QUAL_MANIFEST_REV_BRANCH)) from e
            d = project.as_dict()
            d['revision'] = sha
            frozen_projects.append(d)

        # We include the defaults value here even though all projects
        # are fully specified in order to make the resulting manifest
        # easy to extend by users who want to reuse the defaults.
        r = collections.OrderedDict()
        r['manifest'] = collections.OrderedDict()
        r['manifest']['defaults'] = self.defaults.as_dict()
        r['manifest']['remotes'] = [r.as_dict() for r in self.remotes]
        r['manifest']['projects'] = frozen_projects
        r['manifest']['self'] = self.projects[MANIFEST_PROJECT_INDEX].as_dict()

        return r

    def _malformed(self, complaint, parent=None):
        context = (' file {} '.format(self.path) if self.path
                   else ' data:\n{}\n'.format(self._data))
        exc = MalformedManifest('Malformed manifest{}(schema: {}):\n{}'
                                .format(context, _SCHEMA_PATH,
                                        complaint))
        if parent:
            raise exc from parent
        else:
            raise exc

    def _load(self, data, topdir, path_hint):
        # Initialize this instance's fields from values given in the
        # manifest data, which must be validated according to the schema.
        projects = []
        project_names = set()
        project_paths = set()

        # Create the ManifestProject instance and install it into the
        # Project hierarchy.
        manifest = data.get('manifest')
        slf = manifest.get('self', dict())  # the self name is already taken
        if self.path:
            # We're parsing a real file on disk. We currently require
            # that we are able to resolve a topdir. We may lift this
            # restriction in the future.
            assert topdir
        mproj = ManifestProject(path=slf.get('path', path_hint),
                                topdir=topdir,
                                west_commands=slf.get('west-commands'))
        projects.insert(MANIFEST_PROJECT_INDEX, mproj)

        # Set the topdir attribute based on the results of the above.
        self.topdir = topdir

        # Map from each remote's name onto that remote's data in the manifest.
        remotes = tuple(Remote(r['name'], r['url-base']) for r in
                        manifest.get('remotes', []))
        remotes_dict = {r.name: r for r in remotes}

        # Get any defaults out of the manifest.
        #
        # md = manifest defaults (dictionary with values parsed from
        # the manifest)
        md = manifest.get('defaults', dict())
        mdrem = md.get('remote')
        if mdrem:
            # The default remote name, if provided, must refer to a
            # well-defined remote.
            if mdrem not in remotes_dict:
                self._malformed('default remote {} is not defined'.
                                format(mdrem))
            default_remote = remotes_dict[mdrem]
            default_remote_name = mdrem
        else:
            default_remote = None
            default_remote_name = None
        defaults = Defaults(remote=default_remote, revision=md.get('revision'))

        # pdata = project data (dictionary of project information parsed from
        # the manifest file)
        for pdata in manifest['projects']:
            # Validate the project name.
            name = pdata['name']

            # Validate the project remote or URL.
            remote_name = pdata.get('remote')
            url = pdata.get('url')
            repo_path = pdata.get('repo-path')
            if remote_name is None and url is None:
                if default_remote_name is None:
                    self._malformed(
                        'project {} has no remote or URL (no default is set)'.
                        format(name))
                else:
                    remote_name = default_remote_name
            if remote_name:
                if remote_name not in remotes_dict:
                    self._malformed('project {} remote {} is not defined'.
                                    format(name, remote_name))
                remote = remotes_dict[remote_name]
            else:
                remote = None

            # Create the project instance for final checking.
            try:
                project = Project(name,
                                  defaults,
                                  path=pdata.get('path'),
                                  clone_depth=pdata.get('clone-depth'),
                                  revision=pdata.get('revision'),
                                  west_commands=pdata.get('west-commands'),
                                  remote=remote,
                                  repo_path=repo_path,
                                  topdir=topdir,
                                  url=url)
            except ValueError as ve:
                self._malformed(ve.args[0])

            # The name "manifest" cannot be used as a project name; it
            # is reserved to refer to the manifest repository itself
            # (e.g. from "west list"). Note that this has not always
            # been enforced, but it is part of the documentation.
            if project.name == 'manifest':
                self._malformed('no project can be named "manifest"')
            # Project names must be unique.
            if project.name in project_names:
                self._malformed('project name {} is already used'.
                                format(project.name))
            # Two projects cannot have the same path. We use a PurePath
            # comparison here to ensure that platform-specific canonicalization
            # rules are handled correctly.
            if PurePath(project.path) in project_paths:
                self._malformed('project {} path {} is already in use'.
                                format(project.name, project.path))
            else:
                project_paths.add(PurePath(project.path))

            project_names.add(project.name)
            projects.append(project)

        self.defaults = defaults
        self.remotes = remotes
        self._remotes_dict = remotes_dict
        self.projects = tuple(projects)
        self._proj_name_map = {p.name: p for p in self.projects}
        pmap = dict()
        if self.topdir:
            if mproj.abspath:
                pmap[util.canon_path(mproj.abspath)] = mproj
            for p in self.projects[MANIFEST_PROJECT_INDEX + 1:]:
                assert p.abspath  # sanity check a program invariant
                pmap[util.canon_path(p.abspath)] = p
        self._proj_canon_path_map = pmap


class MalformedManifest(Exception):
    '''Exception indicating that west manifest parsing failed due to a
    malformed value.'''


class MalformedConfig(Exception):
    '''Exception indicating that west config is malformed and thus causing west
       manifest parsing to fail.'''


# Definitions for Manifest attribute types.

class Defaults:
    '''Represents default values in a manifest, either specified by the
    user or by west itself.

    Defaults are neither comparable nor hashable.'''

    __slots__ = 'remote revision'.split()

    def __init__(self, remote=None, revision=None):
        '''Initialize a defaults value from manifest data.

        :param remote: Remote instance corresponding to the default remote,
                       or None (an actual Remote object, not the name of
                       a remote as a string).
        :param revision: Default Git revision; 'master' if not given.'''
        if remote is not None and not isinstance(remote, Remote):
            raise ValueError('{} is not a Remote'.format(remote))
        if revision is None:
            revision = 'master'

        self.remote = remote
        '''`Remote` corresponding to the default remote, or None.'''
        self.revision = revision
        '''Revision to applied to projects without an explicit value.'''

    def __eq__(self, other):
        return NotImplemented

    def __repr__(self):
        return 'Defaults(remote={}, revision={})'.format(repr(self.remote),
                                                         repr(self.revision))

    def as_dict(self):
        '''Return a representation of this object as a dict, as it would be
        parsed from an equivalent YAML manifest.'''
        ret = collections.OrderedDict()
        if self.remote and isinstance(self.remote, Remote):
            ret['remote'] = self.remote.name
        if self.revision:
            ret['revision'] = self.revision
        return ret


class Remote:
    '''Represents a remote defined in a west manifest.

    Remotes may be compared for equality, but are not hashable.'''

    __slots__ = 'name url_base'.split()

    def __init__(self, name, url_base):
        '''Initialize a remote from manifest data.

        :param name: remote's name
        :param url_base: remote's URL base.'''
        if url_base.endswith('/'):
            log.wrn('Remote', name, 'URL base', url_base,
                    'ends with a slash ("/"); these are automatically',
                    'appended by West')

        self.name = name
        '''Remote name as it appears in the manifest.'''
        self.url_base = url_base
        '''Remote url-base value as it appears in the manifest.'''

    def __eq__(self, other):
        return self.name == other.name and self.url_base == other.url_base

    def __repr__(self):
        return 'Remote(name={}, url-base={})'.format(repr(self.name),
                                                     repr(self.url_base))

    def as_dict(self):
        '''Return a representation of this object as a dict, as it would be
        parsed from an equivalent YAML manifest.'''
        return collections.OrderedDict(
            ((s.replace('_', '-'), getattr(self, s)) for s in self.__slots__))


class Project:
    '''Represents a project defined in a west manifest.

    Projects are neither comparable nor hashable.'''

    __slots__ = ('name remote url path topdir abspath posixpath clone_depth '
                 'revision west_commands').split()

    def __init__(self, name, defaults=None, path=None,
                 clone_depth=None, revision=None, west_commands=None,
                 remote=None, repo_path=None, url=None, topdir=None):
        '''Specify a Project by name and other optional information.

        :param name: Project's user-defined name in the manifest.
        :param defaults: If the revision parameter is not given, the project's
                         revision is set to defaults.revision if defaults is
                         not None, or the west-wide default otherwise.
        :param path: Relative path to the project in the west
                     installation, if present in the manifest. If not given,
                     the project's ``name`` is used.
        :param clone_depth: Nonnegative integer clone depth if present in
                            the manifest.
        :param revision: Project revision as given in the manifest, if present.
                         If not given, defaults.revision is used instead.
        :param west_commands: path to a YAML file in the project containing
                              a description of west extension commands provided
                              by the project, if given.
        :param remote: Remote instance corresponding to this Project as
                       specified in the manifest. This is used to build
                       the project's URL, and is also stored as an attribute.
        :param repo_path: If this and *remote* are not None, then
                          ``remote.url_base + repo_path`` (instead of
                          ``remote.url_base + name``) is used as the project's
                          fetch URL.
        :param url: The project's fetch URL. This cannot be given with *remote*
                    or *repo_path*.
        :param topdir: Root of the west installation the Project is inside.
                       If not given, all absolute path attributes (abspath
                       and posixpath) will be None.
        '''
        if remote and url:
            raise ValueError('got both remote={} and url={}'.
                             format(remote, url))
        if repo_path and not remote:
            raise ValueError('got repo_path={} but no remote'.
                             format(repo_path, remote))
        if repo_path and url:
            raise ValueError('got both repo_path={} and url={}'.
                             format(repo_path, url))
        if not (remote or url):
            raise ValueError('got neither a remote nor a URL')

        if defaults is None:
            defaults = _DEFAULTS
        if repo_path is None:
            repo_path = name

        self.name = name
        '''Project name as it appears in the manifest.'''

        # Path related attributes
        self.path = os.path.normpath(path or name)
        '''Relative path to the project in the installation.'''
        self.topdir = topdir
        '''Root directory of the west installation this project is inside,
        or None.'''
        self.abspath = (os.path.realpath(os.path.join(topdir, self.path))
                        if topdir else None)
        '''Absolute path to the project on disk, or None.'''
        self.posixpath = (PurePath(self.abspath).as_posix()
                          if topdir else None)
        '''Absolute path to the project, POSIX style (with forward slashes).'''

        # Git related attributes
        self.url = url or (remote.url_base + '/' + repo_path)
        '''Complete fetch URL for the project, either as given by the url kwarg
        or computed from the remote URL base and the project name.'''
        self.clone_depth = clone_depth
        '''Project's clone depth, or None'''
        self.revision = revision or defaults.revision
        '''Revision to check out for this project, as given in the manifest,
        from manifest defaults, or from the default supplied by west.'''
        self.remote = remote
        '''`Remote` instance corresponding to the project's remote, or None.'''

        # Extension commands in the project
        self.west_commands = west_commands
        '''Path to project's "west-commands", or None.'''

    def __eq__(self, other):
        return NotImplemented

    def __repr__(self):
        reprs = ['{}={}'.format(s, repr(getattr(self, s)))
                 for s in self.__slots__]
        return 'Project(' + ', '.join(reprs) + ')'

    def as_dict(self):
        '''Return a representation of this object as a dict, as it would be
        parsed from an equivalent YAML manifest.'''
        ret = collections.OrderedDict(
            (('name', self.name),
             ('remote', self.remote.name if self.remote else 'None'),
             ('revision', self.revision)))
        if self.path != self.name:
            ret['path'] = self.path
        if self.clone_depth:
            ret['clone-depth'] = self.clone_depth
        if self.west_commands:
            ret['west-commands'] = self.west_commands
        return ret

    def format(self, s, *args, **kwargs):
        '''Calls s.format() with instance-related format keys.

        The formatted value is returned.

        :param s: string (or other object) whose format() method to call

        The format method is called with ``*args`` and the following
        ``kwargs``:

        - this object's ``__slots__`` / values (name, url, etc.)
        - name_and_path: "self.name + (self.path)"
        - remote_name: "None" if no remote, otherwise self.remote.name
        - any additional kwargs passed as parameters

        The kwargs passed as parameters override the other values.
        '''
        kwargs = self._format_kwargs(kwargs)
        return s.format(*args, **kwargs)

    def _format_kwargs(self, kwargs):
        ret = {s: getattr(self, s) for s in self.__slots__}
        ret['name_and_path'] = '{} ({})'.format(self.name, self.path)
        ret['remote_name'] = ('None' if self.remote is None
                              else self.remote.name)
        ret.update(kwargs)
        return ret

    def git(self, cmd, extra_args=(), capture_stdout=False,
            capture_stderr=False, check=True, cwd=None):
        '''Helper for running a git command using metadata from a Project
        instance.

        :param cmd: git command as a string (or list of strings); all strings
                    are formatted using self.format() before use.
        :param extra_args: sequence of additional arguments to pass to the git
                           command (useful mostly if cmd is a string).
        :param capture_stdout: True if stdout should be captured into the
                               returned object instead of being printed.
        :param capture_stderr: Like capture_stdout, but for stderr. Use with
                               caution as it prevents error messages from being
                               shown to the user.
        :param check: True if a subprocess.CalledProcessError should be raised
                      if the git command finishes with a non-zero return code.
        :param cwd: directory to run command in (default: self.abspath)

        Returns a CompletedProcess (which is back-ported for Python 3.4).'''
        _warn_once_if_no_git()

        if isinstance(cmd, str):
            cmd_list = shlex.split(cmd)
        else:
            cmd_list = list(cmd)

        extra_args = list(extra_args)

        if cwd is None:
            if self.abspath is not None:
                cwd = self.abspath
            else:
                raise ValueError('no abspath; cwd must be given')

        args = ['git'] + [self.format(arg) for arg in cmd_list] + extra_args
        cmd_str = util.quote_sh_list(args)

        log.dbg("running '{}'".format(cmd_str), 'in', cwd,
                level=log.VERBOSE_VERY)
        popen = subprocess.Popen(
            args, cwd=cwd,
            stdout=subprocess.PIPE if capture_stdout else None,
            stderr=subprocess.PIPE if capture_stderr else None)

        stdout, stderr = popen.communicate()

        dbg_msg = "'{}' in {} finished with exit status {}".format(
            cmd_str, cwd, popen.returncode)
        if capture_stdout:
            dbg_msg += " and wrote {} to stdout".format(stdout)
        if capture_stderr:
            dbg_msg += " and wrote {} to stderr".format(stderr)
        log.dbg(dbg_msg, level=log.VERBOSE_VERY)

        if check and popen.returncode:
            raise subprocess.CalledProcessError(popen.returncode, cmd_list,
                                                output=stdout, stderr=stderr)
        else:
            return CompletedProcess(popen.args, popen.returncode,
                                    stdout, stderr)

    def sha(self, rev, cwd=None):
        '''Returns the SHA of the given revision in the current project.

        :param rev: git revision (HEAD, v2.0.0, etc.) as a string
        :param cwd: directory to run command in (default: self.abspath)
        '''
        # Though we capture stderr, it will be available as the stderr
        # attribute in the CalledProcessError raised by git() in
        # Python 3.5 and above if this call fails.
        #
        # That's missing for Python 3.4, which at time of writing is
        # still supported by west, but since 3.4 has hit EOL as a
        # mainline Python version, that's an acceptable tradeoff.
        cp = self.git('rev-parse ' + rev, capture_stdout=True, cwd=cwd,
                      capture_stderr=True)
        # Assumption: SHAs are hex values and thus safe to decode in ASCII.
        # It'll be fun when we find out that was wrong and how...
        return cp.stdout.decode('ascii').strip()

    def is_ancestor_of(self, rev1, rev2, cwd=None):
        '''Check if 'rev1' is an ancestor of 'rev2' in this project.

        :param rev1: commit that could be the ancestor
        :param rev2: commit that could be a descendant
        :param cwd: directory to run command in (default: self.abspath)

        Returns True if rev1 is an ancestor commit of rev2 in the
        given project; rev1 and rev2 can be anything that resolves to
        a commit. (If rev1 and rev2 refer to the same commit, the
        return value is True, i.e. a commit is considered an ancestor
        of itself.) Returns False otherwise.'''
        returncode = self.git('merge-base --is-ancestor {} {}'.
                              format(rev1, rev2),
                              check=False, cwd=cwd).returncode

        if returncode == 0:
            return True
        elif returncode == 1:
            return False
        else:
            log.wrn(self.format(
                '{name_and_path}: git failed with exit code {rc}; '
                'treating as if "{r1}" is not an ancestor of "{r2}"',
                rc=returncode, r1=rev1, r2=rev2))
            return False

    def is_up_to_date_with(self, rev, cwd=None):
        '''Check if a project is up to date with revision 'rev'.

        :param rev: base revision to check if project is up to date with.
        :param cwd: directory to run command in (default: self.abspath)

        Returns True if all commits in 'rev' are also in HEAD. This
        can be used to check if this project needs updates, rebasing,
        etc.; 'rev' can be anything that resolves to a commit.

        This is a special case of is_ancestor_of() provided for convenience.
        '''
        return self.is_ancestor_of(rev, 'HEAD', cwd=cwd)

    def is_up_to_date(self, cwd=None):
        '''Returns is_up_to_date_with(self.revision).

        :param cwd: directory to run command in (default: self.abspath)
        '''
        return self.is_up_to_date_with(self.revision, cwd=cwd)

    def is_cloned(self, cwd=None):
        '''Returns True if the project's path is a directory that looks
        like the top-level directory of a Git repository, and False
        otherwise.

        :param cwd: directory to run command in (default: self.abspath)
        '''
        if not os.path.isdir(self.abspath):
            return False

        # --is-inside-work-tree doesn't require that the directory is
        # the top-level directory of a Git repository. Use --show-cdup
        # instead, which prints an empty string (i.e., just a newline,
        # which we strip) for the top-level directory.
        res = self.git('rev-parse --show-cdup', check=False,
                       capture_stderr=True, capture_stdout=True)

        return not (res.returncode or res.stdout.strip())


class ManifestProject(Project):
    '''Represents the manifest repository as a Project.

    The different role of the manifest repository within an
    installation means it won't be perfectly representable as such,
    but several attributes also available for ordinary projects
    are also available.
    '''

    def __init__(self, path=None, revision=None, url=None,
                 west_commands=None, topdir=None):
        '''
        :param path: Relative path to the project in the west
                     installation, if present in the manifest. If None,
                     the project's ``name`` is used.
        :param revision: deprecated and ignored; do not use
        :param url: deprecated and ignored; do not use
        :param west_commands: path to the YAML file in the manifest repository
                              configuring its extension commands, if any.
        :param topdir: Root of the west installation the manifest project
                       is inside. If not given, all absolute path attributes
                       (abspath and posixpath) will be None.
        '''
        self.name = 'manifest'
        '''Name given to the manifest repository (the string "manifest").'''

        # Path related attributes
        self.path = os.path.normpath(path) if path else None
        '''Normalized relative path to the manifest repository in the
        installation, or None.'''
        self.topdir = topdir
        '''Root directory of the west installation, or None.'''
        self.abspath = (os.path.realpath(os.path.join(topdir, self.path))
                        if topdir and path else None)
        '''Absolute path to the manifest repository, or None.'''
        self.posixpath = (PurePath(self.abspath).as_posix()
                          if self.abspath else None)
        '''Absolute path, POSIX style (with forward slashes).'''

        # Git related attributes.
        self.url = None
        '''None; the manifest URL is not defined, as it's not saved by west
        init, and west init -l initializes from a local repository.'''
        self.clone_depth = None
        '''None; the manifest history is always cloned in its entirety.'''
        self.revision = 'HEAD'
        '''The string "HEAD"; provided for analogy with Project.
        Commands which operate on the manifest repository always
        use it as-is on disk and do not change its contents.'''
        self.remote = None
        '''None, for the same reason the url attribute is.'''

        # Extension commands in the manifest repository.
        self.west_commands = west_commands
        '''Path to project's "west-commands", for the manifest project, or
        None.'''

    def as_dict(self):
        '''Return a representation of this object as a dict, as it would be
        parsed from an equivalent YAML manifest.'''
        ret = collections.OrderedDict({'path': self.path})
        if self.west_commands:
            ret['west-commands'] = self.west_commands
        return ret


_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "manifest-schema.yml")
_DEFAULTS = Defaults()


@lru_cache(maxsize=1)
def _warn_once_if_no_git():
    # Using an LRU cache means this gets called once. Afterwards, the
    # (nonexistent) memoized result is simply returned from the cache,
    # so the warning is emitted only once per process invocation.
    if shutil.which('git') is None:
        log.wrn('Git is not installed or cannot be found')

def _mpath(cp=None, topdir=None):
    # Return the value of the manifest.path configuration option
    # in *cp*, a ConfigParser. If not given, create a new one and
    # load configuration options with the given *topdir* as west
    # installation root.
    #
    # TODO: write a cfg.get(section, key)
    # wrapper, with friends for update and delete, to avoid
    # requiring this boilerplate.
    if cp is None:
        cp = cfg._configparser()
    cfg.read_config(configfile=cfg.ConfigFile.LOCAL, config=cp, topdir=topdir)

    try:
        return cp.get('manifest', 'path')
    except (configparser.NoOptionError, configparser.NoSectionError) as e:
        raise MalformedConfig('no "manifest.path" config option is set') from e

def _west_yml(topdir):
    return os.path.join(topdir, _mpath(topdir=topdir), 'west.yml')
