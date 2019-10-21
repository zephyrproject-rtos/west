# Copyright (c) 2018, Nordic Semiconductor ASA
# Copyright 2018, 2019 Foundries.io Ltd
#
# SPDX-License-Identifier: Apache-2.0

'''
Parser and abstract data types for west manifests.
'''

import collections
import configparser
import errno
from functools import lru_cache
import os
from pathlib import PurePath
import shutil
import shlex
import subprocess

from packaging.version import parse as parse_version
import pykwalify.core
import yaml

from west import util, log
from west.backports import CompletedProcess
import west.configuration as cfg

#: Index in a Manifest.projects attribute where the `ManifestProject`
#: instance for the installation is stored.
MANIFEST_PROJECT_INDEX = 0

#: A git revision which points to the most recent `Project` update.
MANIFEST_REV_BRANCH = 'manifest-rev'

#: A fully qualified reference to `MANIFEST_REV_BRANCH`.
QUAL_MANIFEST_REV_BRANCH = 'refs/heads/' + MANIFEST_REV_BRANCH

#: The latest manifest schema version supported by this west program.
#:
#: This value changes when a new version of west includes new manifest
#: file features not supported by earlier versions of west.
SCHEMA_VERSION = '0.6.99'
# ^^ will be bumped to 0.7 for that release; this just marks that
# there were changes since 0.6 and we're in a development tree.

def manifest_path():
    '''Absolute path of the manifest file in the current installation.

    Exceptions raised:

        - `west.util.WestNotFound` if called from outside of a west
          installation

        - `MalformedConfig` if the configuration file has no
          ``manifest.path`` key

        - ``FileNotFoundError`` if no ``west.yml`` exists in
          ``manifest.path``
    '''
    ret = os.path.join(util.west_topdir(), _mpath(), 'west.yml')
    # It's kind of annoying to manually instantiate a FileNotFoundError.
    # This seems to be the best way.
    if not os.path.isfile(ret):
        raise OSError(errno.ENOENT, os.strerror(errno.ENOENT), ret)
    return ret

class Manifest:
    '''The parsed contents of a west manifest file.
    '''

    @staticmethod
    def from_file(source_file=None, topdir=None):
        '''Manifest object factory given a source YAML file.

        Results depend on the parameters given:

            - If both *source_file* and *topdir* are given, the
              returned Manifest object is based on the data in
              *source_file*, rooted at *topdir*. The configuration
              files are not read in this case. This allows parsing a
              manifest file "as if" its project hierarchy were rooted
              at another location in the system.

            - If neither *source_file* nor *topdir* is given, the file
              system is searched for *topdir*. That installation's
              ``manifest.path`` configuration option is used to find
              *source_file*, ``topdir/<manifest.path>/west.yml``.

            - If only *source_file* is given, *topdir* is found
              starting there. The directory containing *source_file*
              doesn't have to be ``manifest.path`` in this case.

            - If only *topdir* is given, that installation's
              ``manifest.path`` is used to find *source_file*.

        Exceptions raised:

            - `west.util.WestNotFound` if no *topdir* can be found

            - `MalformedManifest` if *source_file* contains invalid
              data

            - `MalformedConfig` if ``manifest.path`` is needed and
              can't be read

            - ``ValueError`` if *topdir* is given but is not a west
              installation root

        :param source_file: path to the manifest YAML file
        :param topdir: west installation top level directory
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
        '''Manifest object factory given parsed YAML data.

        This factory does not read any configuration files.

        Letting the return value be ``m``:

            - Unless *topdir* is given, all absolute paths in ``m``,
              like ``m.projects[1].abspath``, are ``None``.

            - Relative paths, like ``m.projects[1].path``, are taken
              from *source_data*.

            - If ``source_data['manifest']['self']['path']`` is not
              set, then ``m.projects[MANIFEST_PROJECT_INDEX].abspath``
              will be set to *manifest_path*.

        Raises `MalformedManifest` if *source_data* is not a valid
        manifest.

        :param source_data: parsed YAML data as a Python object, or a
            string with unparsed YAML data
        :param manifest_path: fallback `ManifestProject` path
            attribute
        :param topdir: used as the installation's top level directory
        '''
        if isinstance(source_data, str):
            source_data = yaml.safe_load(source_data)
        return Manifest(source_data=source_data, topdir=topdir,
                        manifest_path=manifest_path)

    def __init__(self, source_file=None, source_data=None,
                 manifest_path=None, topdir=None):
        '''
        Using `from_file` or `from_data` is usually easier than direct
        instantiation.

        Instance attributes:

            - ``projects``: sequence of `Project`

            - ``topdir``: west installation top level directory, or
              None

            - ``path``: path to the manifest file itself, or None

        Exactly one of *source_file* and *source_data* must be given.

        If *source_file* is given:

            - If *topdir* is too, ``projects`` is rooted there.

            - Otherwise, *topdir* is found starting at *source_file*.

        If *source_data* is given:

            - If *topdir* is too, ``projects`` is rooted there.

            - Otherwise, there is no root: ``projects[i].abspath`` and
              other absolute path attributes are ``None``.

            - If ``source_data['manifest']['self']['path']`` is unset,
              *manifest_path* is used as a fallback.

        Exceptions raised:

            - `MalformedManifest`: if the manifest data is invalid

            - `WestNotFound`: if *topdir* was needed and not found

            - ``ValueError``: for other invalid arguments

        :param source_file: YAML file containing manifest data
        :param source_data: parsed YAML data as a Python object, or a
            string containing unparsed YAML data
        :param manifest_path: fallback `ManifestProject` ``path``
            attribute
        :param topdir: used as the west installation top level
            directory
        '''
        if source_file and source_data:
            raise ValueError('both source_file and source_data were given')

        self.path = None
        '''Path to the file containing the manifest, or None if
        created from data rather than the file system.
        '''

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

        # Make sure this version of west can load this manifest data.
        # This has to happen before the schema check -- later schemas
        # may incompatibly extend this one.
        if 'version' in data:
            # As a convenience for the user, convert floats to strings.
            # This avoids forcing them to write:
            #
            #  version: "1.0"
            #
            # by explicitly allowing:
            #
            #  version: 1.0
            min_version_str = str(data['version'])
            min_version = parse_version(min_version_str)
            if min_version > _SCHEMA_VER:
                raise ManifestVersionError(min_version, file=source_file)
            elif min_version < _EARLIEST_VER:
                self._malformed(
                    'invalid version {}; lowest schema version is {}'.
                    format(min_version, _EARLIEST_VER_STR))

        try:
            pykwalify.core.Core(source_data=data,
                                schema_files=[_SCHEMA_PATH]).validate()
        except pykwalify.errors.SchemaError as se:
            self._malformed(se._msg, parent=se)

        self.projects = None
        '''Sequence of `Project` objects representing manifest
        projects.

        Index 0 (`MANIFEST_PROJECT_INDEX`) contains a
        `ManifestProject` representing the manifest repository. The
        rest of the sequence contains projects in manifest file order.
        '''

        self.topdir = topdir
        '''The west installation's top level directory, or None.'''

        # Set up the public attributes documented above, as well as
        # any internal attributes needed to implement the public API.
        self._load(manifest_path)

    def get_projects(self, project_ids, allow_paths=True, only_cloned=False):
        '''Get a list of `Project` objects in the manifest from
        *project_ids*.

        If *project_ids* is empty, a copy of ``self.projects``
        attribute is returned as a list. Otherwise, the returned list
        has projects in the same order as *project_ids*.

        ``ValueError`` is raised if:

            - *project_ids* contains unknown project IDs

            - (with *only_cloned*) an uncloned project was found

        The ``ValueError`` *args* attribute is a 2-tuple with a list
        of unknown *project_ids* at index 0, and a list of uncloned
        `Project` objects at index 1.

        :param project_ids: a sequence of projects, identified by name
            (these are matched first) or path (as a fallback, but only
            with *allow_paths*)
        :param allow_paths: if true, *project_ids* may also contain
            relative or absolute project paths
        :param only_cloned: raise an exception for uncloned projects
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
            project = self._projects_by_name.get(pid)
            if project is None and allow_paths:
                project = self._projects_by_cpath.get(util.canon_path(pid))

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
        '''Returns an ``OrderedDict`` representing self, but frozen.

        The value is "frozen" in that all project revisions are the
        full SHAs pointed to by `QUAL_MANIFEST_REV_BRANCH` references.

        Raises ``RuntimeError`` if a project SHA can't be resolved.
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

        r = collections.OrderedDict()
        r['manifest'] = collections.OrderedDict()
        r['manifest']['projects'] = frozen_projects
        r['manifest']['self'] = self.projects[MANIFEST_PROJECT_INDEX].as_dict()

        return r

    def _malformed(self, complaint, parent=None):
        context = (' file: {} '.format(self.path) if self.path
                   else ' data:\n{}'.format(self._data))
        args = ['Malformed manifest{}'.format(context),
                'Schema file: {}'.format(_SCHEMA_PATH)]
        if complaint:
            args.append('Hint: ' + complaint)
        exc = MalformedManifest(*args)
        if parent:
            raise exc from parent
        else:
            raise exc

    def _load(self, path_hint):
        # Initialize this instance's fields from values given in the
        # manifest data, which must be validated according to the schema.

        projects = []
        projects_by_name = {}
        projects_by_ppath = {}
        manifest = self._data['manifest']

        # Create the ManifestProject instance and install it into the
        # projects list.
        assert MANIFEST_PROJECT_INDEX == 0
        projects.append(self._load_self(path_hint))

        # Map from each remote's name onto that remote's data in the manifest.
        self._remotes_by_name = {r['name']: Remote(r['name'], r['url-base'])
                                 for r in manifest.get('remotes', [])}

        # Get any defaults out of the manifest.
        self._defaults = self._load_defaults()

        # pdata = project data (dictionary of project information parsed from
        # the manifest file)
        for pdata in manifest['projects']:
            project = self._load_project(pdata)

            # Project names must be unique.
            if project.name in projects_by_name:
                self._malformed('project name {} is already used'.
                                format(project.name))
            # Two projects cannot have the same path. We use a PurePath
            # comparison here to ensure that platform-specific canonicalization
            # rules are handled correctly.
            ppath = PurePath(project.path)
            other = projects_by_ppath.get(ppath)
            if other:
                self._malformed(
                    'project {} path "{}" is already taken by project {}'.
                    format(project.name, project.path, other.name))
            else:
                projects_by_ppath[ppath] = project

            projects.append(project)
            projects_by_name[project.name] = project

        self.projects = tuple(projects)
        self._projects_by_name = projects_by_name
        self._projects_by_cpath = {}
        if self.topdir:
            mp = self.projects[MANIFEST_PROJECT_INDEX]
            if mp.abspath:
                self._projects_by_cpath[util.canon_path(mp.abspath)] = mp
            for p in self.projects[MANIFEST_PROJECT_INDEX + 1:]:
                assert p.abspath  # sanity check a program invariant
                self._projects_by_cpath[util.canon_path(p.abspath)] = p

    def _load_self(self, path_hint):
        # "slf" because "self" is already taken
        slf = self._data['manifest'].get('self', dict())
        if self.path:
            # We're parsing a real file on disk. We currently require
            # that we are able to resolve a topdir. We may lift this
            # restriction in the future.
            assert self.topdir

        return ManifestProject(path=slf.get('path', path_hint),
                               topdir=self.topdir,
                               west_commands=slf.get('west-commands'))

    def _load_defaults(self):
        # md = manifest defaults (dictionary with values parsed from
        # the manifest)
        md = self._data['manifest'].get('defaults', dict())
        mdrem = md.get('remote')
        if mdrem:
            # The default remote name, if provided, must refer to a
            # well-defined remote.
            if mdrem not in self._remotes_by_name:
                self._malformed('default remote {} is not defined'.
                                format(mdrem))
            default_remote = self._remotes_by_name[mdrem]
        else:
            default_remote = None

        return Defaults(remote=default_remote, revision=md.get('revision'))

    def _load_project(self, pdata):
        # Validate the project name.
        name = pdata['name']

        # Validate the project remote or URL.
        remote_name = pdata.get('remote')
        url = pdata.get('url')
        repo_path = pdata.get('repo-path')
        if remote_name is None and url is None:
            if self._defaults.remote is None:
                self._malformed(
                    'project {} has no remote or URL (no default is set)'.
                    format(name))
            else:
                remote_name = self._defaults.remote.name
        if remote_name:
            if remote_name not in self._remotes_by_name:
                self._malformed('project {} remote {} is not defined'.
                                format(name, remote_name))
            remote = self._remotes_by_name[remote_name]
        else:
            remote = None

        # Create the project instance for final checking.
        try:
            project = Project(name, defaults=self._defaults,
                              path=pdata.get('path'),
                              clone_depth=pdata.get('clone-depth'),
                              revision=pdata.get('revision'),
                              west_commands=pdata.get('west-commands'),
                              remote=remote, repo_path=repo_path,
                              topdir=self.topdir, url=url)
        except ValueError as ve:
            self._malformed(ve.args[0])

        # The name "manifest" cannot be used as a project name; it
        # is reserved to refer to the manifest repository itself
        # (e.g. from "west list"). Note that this has not always
        # been enforced, but it is part of the documentation.
        if project.name == 'manifest':
            self._malformed('no project can be named "manifest"')

        return project

class MalformedManifest(Exception):
    '''Manifest parsing failed due to invalid data.
    '''

class MalformedConfig(Exception):
    '''The west configuration was malformed in a way that made a
    manifest operation fail.
    '''

class ManifestVersionError(Exception):
    '''The manifest required a version of west more recent than the
    current version.
    '''

    def __init__(self, version, file=None):
        self.version = version
        '''The minimum version of west that was required.'''

        self.file = file
        '''The file that required this version of west.'''

# Definitions for Manifest attribute types.

class Defaults:
    '''Represents default values in a manifest, either specified by
    the user or supplied by west itself. Neither comparable nor
    hashable.
    '''

    __slots__ = 'remote revision'.split()

    def __init__(self, remote=None, revision=None):
        '''Initialize a defaults value from manifest data.

        :param remote: the default `Remote`, or ``None`` (a
            ``west.manifest.Remote``, not the name of a remote).
        :param revision: default project Git revision; 'master' if not
            specified in the manifest.
        '''
        if remote is not None and not isinstance(remote, Remote):
            raise ValueError('{} is not a Remote'.format(remote))
        if revision is None:
            revision = 'master'

        self.remote = remote
        '''`Remote` corresponding to the default remote, or ``None``.'''
        self.revision = revision
        '''Default Git revision to fetch when updating projects.'''

    def __eq__(self, other):
        return NotImplemented

    def __repr__(self):
        return 'Defaults(remote={}, revision={})'.format(repr(self.remote),
                                                         repr(self.revision))

    def as_dict(self):
        '''Return a representation of this object as a dict, as it
        would be parsed from manifest YAML data.
        '''
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
        :param url_base: remote's URL base.
        '''
        if url_base.endswith('/'):
            log.wrn('Remote', name, 'URL base', url_base,
                    'ends with a slash ("/"); these are automatically',
                    'appended by West')

        self.name = name
        '''Remote ``name`` as it appears in the manifest data.'''
        self.url_base = url_base
        '''Remote ``url-base`` as it appears in the manifest data.'''

    def __eq__(self, other):
        return self.name == other.name and self.url_base == other.url_base

    def __repr__(self):
        return 'Remote(name={}, url-base={})'.format(repr(self.name),
                                                     repr(self.url_base))

    def as_dict(self):
        '''Return a representation of this object as a dict, as it
        would be parsed from manifest YAML data.
        '''
        return collections.OrderedDict(
            ((s.replace('_', '-'), getattr(self, s)) for s in self.__slots__))


class Project:
    '''Represents a project defined in a west manifest.

    Attributes:

    - ``name``: project's unique name
    - ``topdir``: the top level directory of the west installation
      the project is part of, or ``None``
    - ``path``: relative path to the project within the installation
      (i.e. from ``topdir`` if that is set)
    - ``abspath``: absolute path to the project in the native path name
      format (or ``None`` if ``topdir`` is)
    - ``posixpath``: like ``abspath``, but with slashes (``/``) as
      path separators
    - ``url``: project fetch URL
    - ``revision``: revision to fetch from ``url`` when the
      project is updated
    - ``clone_depth``: clone depth to fetch when first cloning the
      project, or ``None`` (the revision should not be a SHA
      if this is used)
    - ``west_commands``: project's ``west_commands:`` key in the
      manifest data
    '''

    def __eq__(self, other):
        return NotImplemented

    def __str__(self):
        return '<Project {} at {}>'.format(
            repr(self.name), repr(self.abspath or self.path))

    def __init__(self, name, defaults=None, path=None,
                 clone_depth=None, revision=None, west_commands=None,
                 remote=None, repo_path=None, url=None, topdir=None):
        '''
        Project constructor.

        Constructor arguments for project attributes should not be
        given unless the project's manifest data contains them.

        If *topdir* is ``None``, then absolute path attributes
        (``abspath`` and ``posixpath``) will also be ``None``.

        :param name: project's ``name:`` attribute in the manifest
        :param defaults: a `Defaults` instance to use, if the manifest
            has a ``defaults:``
        :param path: ``path:`` attribute value
        :param clone_depth: ``clone-depth:`` attribute value
        :param revision: ``revision:`` attribute value
        :param west_commands: ``west-commands:`` attribute value
        :param remote: `Remote` instance to use, if there is a
            ``remote:`` attribute
        :param repo_path: ``repo-path:`` attribute value
        :param url: ``url:`` attribute value
        :param topdir: the west installation's top level directory
        '''
        if remote and url:
            raise ValueError('project {} has both "remote: {}" and "url: {}"'.
                             format(name, remote.name, url))
        if repo_path and not remote:
            raise ValueError('project {} has repo_path={} but no remote'.
                             format(name, repo_path, remote))
        if repo_path and url:
            raise ValueError('project {} has both repo_path={} and url={}'.
                             format(name, repo_path, url))
        if not (remote or url):
            raise ValueError('project {} has neither a remote nor a URL'.
                             format(name))

        if not url:
            url = remote.url_base + '/' + (repo_path or name)

        self.name = name
        self.url = url
        self._revision = revision
        self._default_rev = defaults.revision if defaults else 'master'
        self.topdir = topdir
        self.path = path
        self.clone_depth = clone_depth
        self.west_commands = west_commands

    @property
    def revision(self):
        return self._revision or self._default_rev

    @revision.setter
    def revision(self, revision):
        self._revision = revision

    @property
    def path(self):
        return self._path or self.name

    @path.setter
    def path(self, path):
        self._path = path

        # Invalidate the absolute path attributes. They'll get
        # computed again next time they're accessed.
        self._abspath = None
        self._posixpath = None

    @property
    def abspath(self):
        if self._abspath is None and self.topdir:
            self._abspath = os.path.realpath(os.path.join(self.topdir,
                                                          self.path))
        return self._abspath

    @property
    def posixpath(self):
        if self._posixpath is None and self.topdir:
            self._posixpath = PurePath(self.abspath).as_posix()
        return self._posixpath

    def as_dict(self):
        '''Return a representation of this object as a dict, as it
        would be parsed from an equivalent YAML manifest.
        '''
        ret = collections.OrderedDict()
        ret['name'] = self.name
        ret['url'] = self.url
        if self._revision:
            ret['revision'] = self.revision
        if self.clone_depth:
            ret['clone-depth'] = self.clone_depth
        if self.west_commands:
            ret['west-commands'] = self.west_commands
        if self._path:
            ret['path'] = self.path

        return ret

    def format(self, s, *args, **kwargs):
        '''Calls ``s.format()`` with instance-related arguments.

        The formatted value is returned.

        ``s.format()`` is called with any args and kwargs passed as
        parameters to this method, and the following additional
        kwargs:

            - ``name``
            - ``url``
            - ``revision``
            - ``path``
            - ``abspath``
            - ``posixpath``
            - ``clone_depth``
            - ``west_commands``
            - ``topdir``
            - ``name_and_path=f"{self.name} ({self.path})"`` (or its
              non-f-string equivalent)

        Any kwargs passed as parameters to this method override the
        above additional kwargs.

        :param s: string (or other object) to call ``format()`` on
        '''
        kw = {s: getattr(self, s) for s in
              'name url revision path abspath posixpath '
              'clone_depth west_commands topdir'.split()}
        kw['name_and_path'] = '{} ({})'.format(self.name, self.path)
        kw.update(kwargs)
        return s.format(*args, **kw)

    #
    # Git helpers
    #

    def git(self, cmd, extra_args=(), capture_stdout=False,
            capture_stderr=False, check=True, cwd=None):
        '''Run a git command in the project repository.

        Returns a ``subprocess.CompletedProcess`` (an equivalent
        object is back-ported for Python 3.4).

        :param cmd: git command as a string (or list of strings); all
            strings are formatted using `format` before use.
        :param extra_args: sequence of additional arguments to pass to
            the git command (useful mostly if *cmd* is a string).
        :param capture_stdout: if True, git's standard output is
            captured in the ``CompletedProcess`` instead of being
            printed.
        :param capture_stderr: Like *capture_stdout*, but for standard
            error. Use with caution: this may prevent error messages
            from being shown to the user.
        :param check: if given, ``subprocess.CalledProcessError`` is
            raised if git finishes with a non-zero return code
        :param cwd: directory to run git in (default: ``self.abspath``)
        '''
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
        '''Returns the project's current revision as a SHA.

        :param rev: git revision (HEAD, v2.0.0, etc.) as a string
        :param cwd: directory to run command in (default:
            self.abspath)
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

        Returns True if rev1 is an ancestor commit of rev2 in the
        given project; rev1 and rev2 can be anything that resolves to
        a commit. (If rev1 and rev2 refer to the same commit, the
        return value is True, i.e. a commit is considered an ancestor
        of itself.) Returns False otherwise.

        :param rev1: commit that could be the ancestor of *rev2*
        :param rev2: commit that could be a descendant or *rev1*
        :param cwd: directory to run command in (default:
            ``self.abspath``)
        '''
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
        '''Check if the project is up to date with *rev*, returning
        ``True`` if so.

        This is equivalent to ``is_ancestor_of(rev, 'HEAD',
        cwd=cwd)``.

        :param rev: base revision to check if project is up to date
            with.
        :param cwd: directory to run command in (default:
            ``self.abspath``)
        '''
        return self.is_ancestor_of(rev, 'HEAD', cwd=cwd)

    def is_up_to_date(self, cwd=None):
        '''Check if the project HEAD is up to date with the manifest.

        This is equivalent to ``is_up_to_date_with(self,revision,
        cwd=cwd)``.

        :param cwd: directory to run command in (default:
            ``self.abspath``)
        '''
        return self.is_up_to_date_with(self.revision, cwd=cwd)

    def is_cloned(self, cwd=None):
        '''Returns ``True`` if ``self.abspath`` looks like a git
        repository's top-level directory, and ``False`` otherwise.

        :param cwd: directory to run command in (default:
            ``self.abspath``)
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

# FIXME: this whole class should just go away. See #327.
class ManifestProject(Project):
    '''Represents the manifest repository as a `Project`.

    Meaningful attributes:

    - ``name``: the string ``"manifest"``
    - ``topdir``: the top level directory of the west installation
      the manifest project controls, or ``None``
    - ``path``: relative path to the manifest repository within the
      installation, or ``None`` (i.e. from ``topdir`` if that is set)
    - ``abspath``: absolute path to the manifest repository in the
      native path name format (or ``None`` if ``topdir`` is)
    - ``posixpath``: like ``abspath``, but with slashes (``/``) as
      path separators
    - ``west_commands``:``west_commands:`` key in the manifest's
      ``self:`` map

    Other readable attributes included for Project compatibility:

    - ``url``: always ``None``; the west manifest is not
      version-controlled by west itself, even though 'west init'
      can fetch a manifest repository from a Git remote
    - ``revision``: ``"HEAD"``
    - ``clone_depth``: ``None``, because ``url`` is
    '''

    def __init__(self, path=None, west_commands=None, topdir=None):
        '''
        :param path: Relative path to the manifest repository in the
            west installation, if known.
        :param west_commands: path to the YAML file in the manifest
            repository configuring its extension commands, if any.
        :param topdir: Root of the west installation the manifest
            project is inside. If not given, all absolute path
            attributes (abspath and posixpath) will be None.
        '''
        self.name = 'manifest'

        # Path related attributes
        self.topdir = topdir
        self._abspath = None
        self._posixpath = None
        self._path = os.path.normpath(path) if path else None

        # Extension commands.
        self.west_commands = west_commands

    @property
    def path(self):
        return self._path

    @path.setter
    def path(self, path):
        self._path = path

        # Invalidate the absolute path attributes. They'll get
        # computed again next time they're accessed.
        self._abspath = None
        self._posixpath = None

    @property
    def abspath(self):
        if self._abspath is None and self.topdir and self.path:
            self._abspath = os.path.realpath(os.path.join(self.topdir,
                                                          self.path))
        return self._abspath

    @property
    def posixpath(self):
        if self._posixpath is None and self.abspath:
            self._posixpath = PurePath(self.abspath).as_posix()
        return self._posixpath

    @property
    def url(self):
        return None

    @url.setter
    def url(self, url):
        raise ValueError(url)

    @property
    def revision(self):
        return 'HEAD'

    @revision.setter
    def revision(self, revision):
        raise ValueError(revision)

    @property
    def clone_depth(self):
        return None

    @clone_depth.setter
    def clone_depth(self, clone_depth):
        raise ValueError(clone_depth)

    def as_dict(self):
        '''Return a representation of this object as a dict, as it would be
        parsed from an equivalent YAML manifest.'''
        ret = collections.OrderedDict()
        if self.path:
            ret['path'] = self.path
        if self.west_commands:
            ret['west-commands'] = self.west_commands
        return ret

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "manifest-schema.yml")
_SCHEMA_VER = parse_version(SCHEMA_VERSION)
_EARLIEST_VER_STR = '0.6.99'  # we introduced the version feature after 0.6
_EARLIEST_VER = parse_version(_EARLIEST_VER_STR)

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
