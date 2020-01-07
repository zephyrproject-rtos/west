# Copyright (c) 2018, 2019 Nordic Semiconductor ASA
# Copyright 2018, 2019 Foundries.io Ltd
#
# SPDX-License-Identifier: Apache-2.0

'''
Parser and abstract data types for west manifests.
'''

import collections
import configparser
import enum
import errno
from functools import lru_cache
import os
from pathlib import PurePath, Path
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

#: Git ref space used by west for internal purposes.
QUAL_REFS_WEST = 'refs/west/'

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
    ret = os.path.join(util.west_topdir(), _mpath(), _WEST_YML)
    # It's kind of annoying to manually instantiate a FileNotFoundError.
    # This seems to be the best way.
    if not os.path.isfile(ret):
        raise OSError(errno.ENOENT, os.strerror(errno.ENOENT), ret)
    return ret

def validate(data):
    '''Validate manifest data

    Returns if the manifest data is valid and can be loaded by this
    version of west (though this may fail if the manifest contains
    imports which cannot be resolved).

    Raises an exception otherwise.

    :param data: YAML manifest data as a string or object
    '''
    if isinstance(data, str):
        data = yaml.safe_load(data)

    if 'manifest' not in data:
        raise MalformedManifest('manifest data contains no "manifest" key')

    data = data['manifest']

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
            raise ManifestVersionError(min_version)
        elif min_version < _EARLIEST_VER:
            raise MalformedManifest(
                'invalid version {}; lowest schema version is {}'.
                format(min_version, _EARLIEST_VER_STR))

    try:
        pykwalify.core.Core(source_data=data,
                            schema_files=[_SCHEMA_PATH]).validate()
    except pykwalify.errors.SchemaError as se:
        raise MalformedManifest(se._msg) from se

# TODO rewrite without enum.IntFlag if we can't move to python 3.6+
class ImportFlag(enum.IntFlag):
    '''Bit flags for handling imports when resolving a manifest.

    The DEFAULT (0) value allows reading the file system to resolve
    "self: import:", and running git to resolve a "projects:" import.
    Other flags:

    - IGNORE: ignore all "import:" attributes in "self:" and "projects:"
    - FORCE_PROJECTS: always invoke importer callback for "projects:" imports
    '''

    DEFAULT = 0
    IGNORE = 1
    FORCE_PROJECTS = 2

class Manifest:
    '''The parsed contents of a west manifest file.
    '''

    @staticmethod
    def from_file(source_file=None, **kwargs):
        '''Manifest object factory given a source YAML file.

        The default behavior is to find the current west installation's
        manifest file and resolve it.

        Results depend on the keyword arguments given in *kwargs*:

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

            - `ManifestVersionError` if this version of west is too
              old to parse the manifest.

            - `MalformedConfig` if ``manifest.path`` is needed and
              can't be read

            - ``ValueError`` if *topdir* is given but is not a west
              installation root

        :param source_file: source file to load
        :param kwargs: Manifest.__init__ keyword arguments
        '''
        topdir = kwargs.get('topdir')

        if topdir is None:
            if source_file is None:
                # neither source_file nor topdir: search the filesystem
                # for the installation and use its manifest.path.
                topdir = util.west_topdir()
                kwargs.update({
                    'topdir': topdir,
                    'source_file': os.path.join(topdir, _mpath(topdir=topdir),
                                                _WEST_YML)
                })
            else:
                # Just source_file: find topdir starting there.
                # We need source_file in kwargs as that's what gets used below.
                kwargs.update({
                    'source_file': source_file,
                    'topdir':
                    util.west_topdir(start=os.path.dirname(source_file))
                })
        elif source_file is None:
            # Just topdir.

            # Verify topdir is a real west installation root.
            msg = 'topdir {} is not a west installation root'.format(topdir)
            try:
                real_topdir = util.west_topdir(start=topdir, fall_back=False)
            except util.WestNotFound:
                raise ValueError(msg)
            if PurePath(topdir) != PurePath(real_topdir):
                raise ValueError(msg + '; but {} is'.format(real_topdir))

            # Read manifest.path from topdir/.west/config, and use it
            # to locate source_file.
            mpath = _mpath(topdir=topdir)
            source_file = os.path.join(topdir, mpath, _WEST_YML)
            kwargs.update({
                'source_file': source_file,
                'manifest_path': mpath,
            })
        else:
            # Both source_file and topdir.
            kwargs['source_file'] = source_file

        return Manifest(**kwargs)

    @staticmethod
    def from_data(source_data, **kwargs):
        '''Manifest object factory given parsed YAML data.

        This factory does not read any configuration files.

        Letting the return value be ``m``. Results then depend on
        keyword arguments in *kwargs*:

            - Unless *topdir* is given, all absolute paths in ``m``,
              like ``m.projects[1].abspath``, are ``None``.

            - Relative paths, like ``m.projects[1].path``, are taken
              from *source_data*.

            - If ``source_data['manifest']['self']['path']`` is not
              set, then ``m.projects[MANIFEST_PROJECT_INDEX].abspath``
              will be set to *manifest_path* if given.

        Returns the same exceptions as the Manifest constructor.

        :param source_data: parsed YAML data as a Python object, or a
            string with unparsed YAML data
        :param kwargs: Manifest.__init__ keyword arguments
        '''
        kwargs.update({'source_data': source_data})
        return Manifest(**kwargs)

    def __init__(self, source_file=None, source_data=None,
                 manifest_path=None, topdir=None, importer=None,
                 import_flags=0):
        '''
        Using `from_file` or `from_data` is usually easier than direct
        instantiation.

        Instance attributes:

            - ``projects``: sequence of `Project`

            - ``topdir``: west installation top level directory, or
              None

            - ``path``: path to the manifest file itself, or None

            - ``has_imports``: bool, True if the manifest contains
              an "import:" attribute in "self:" or "projects:"; False
              otherwise

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

        The *importer* kwarg, if given, is a callable. It is called
        when *source_file* requires importing manifest data that
        aren't found locally. It will be called as:

        ``importer(project, file)``

        where ``project`` is a `Project` and ``file`` is the missing
        file. The file's contents at refs/heads/manifest-rev should
        usually be returned, potentially after fetching the project's
        revision from its remote URL and updating that ref.

        The return value should be a string containing manifest data,
        or a list of strings if ``file`` is a directory containing
        YAML files. A return value of None will cause the import to be
        ignored.

        Exceptions raised:

            - `MalformedManifest`: if the manifest data is invalid

            - `ManifestVersionError`: if this version of west is too
              old to parse the manifest

            - `WestNotFound`: if *topdir* was needed and not found

            - ``ValueError``: for other invalid arguments

        :param source_file: YAML file containing manifest data
        :param source_data: parsed YAML data as a Python object, or a
            string containing unparsed YAML data
        :param manifest_path: fallback `ManifestProject` ``path``
            attribute
        :param topdir: used as the west installation top level
            directory
        :param importer: callback to resolve missing manifest import
            data
        :param import_flags: bit mask, controls import resolution
        '''
        if source_file and source_data:
            raise ValueError('both source_file and source_data were given')

        self.path = None
        '''Path to the file containing the manifest, or None if
        created from data rather than the file system.
        '''

        if source_file:
            with open(source_file, 'r') as f:
                source_data = f.read()
            self.path = os.path.abspath(source_file)

        if not source_data:
            self._malformed('manifest contains no data')

        if isinstance(source_data, str):
            source_data = yaml.safe_load(source_data)

        # Validate the manifest. Wrap a couple of the exceptions with
        # extra context about the problematic file in case of errors,
        # to help debugging.
        try:
            validate(source_data)
        except ManifestVersionError as mv:
            raise ManifestVersionError(mv.version, file=source_file) from mv
        except MalformedManifest as mm:
            self._malformed(mm.args[0], parent=mm)

        self.projects = None
        '''Sequence of `Project` objects representing manifest
        projects.

        Index 0 (`MANIFEST_PROJECT_INDEX`) contains a
        `ManifestProject` representing the manifest repository. The
        rest of the sequence contains projects in manifest file order
        (or resolution order if the manifest contains imports).
        '''

        self.topdir = topdir
        '''The west installation's top level directory, or None.'''

        self.has_imports = False

        # Set up the public attributes documented above, as well as
        # any internal attributes needed to implement the public API.
        self._importer = importer or _default_importer
        self._import_flags = import_flags
        self._load(source_data['manifest'], manifest_path)

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
            if pid == 'manifest':
                project = self.projects[MANIFEST_PROJECT_INDEX]
            else:
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

    def _as_dict_helper(self, pdict=None):
        # pdict: a function which is given a project, and returns its
        #   dict representation. By default, it's Project.as_dict.
        if pdict is None:
            pdict = Project.as_dict

        projects = list(self.projects)
        del projects[MANIFEST_PROJECT_INDEX]
        project_dicts = [pdict(p) for p in projects]

        r = collections.OrderedDict()
        r['manifest'] = collections.OrderedDict()
        r['manifest']['projects'] = project_dicts
        r['manifest']['self'] = self.projects[MANIFEST_PROJECT_INDEX].as_dict()

        return r

    def as_dict(self):
        '''Returns an ``OrderedDict`` representing self, fully
        resolved.

        The value is "resolved" in that the result is as if all
        projects had been defined in a single manifest without any
        import attributes.
        '''
        return self._as_dict_helper()

    def as_frozen_dict(self):
        '''Returns an ``OrderedDict`` representing self, but frozen.

        The value is "frozen" in that all project revisions are the
        full SHAs pointed to by `QUAL_MANIFEST_REV_BRANCH` references.

        Raises ``RuntimeError`` if a project SHA can't be resolved.
        '''
        def pdict(p):
            if not p.is_cloned():
                raise RuntimeError(f'cannot freeze; project {p.name} '
                                   'is uncloned')
            try:
                sha = p.sha(QUAL_MANIFEST_REV_BRANCH)
            except subprocess.CalledProcessError as e:
                raise RuntimeError(f'cannot freeze; project {p.name} '
                                   f'ref {QUAL_MANIFEST_REV_BRANCH} '
                                   'cannot be resolved to a SHA') from e
            d = p.as_dict()
            d['revision'] = sha
            return d

        return self._as_dict_helper(pdict=pdict)

    def _malformed(self, complaint, parent=None):
        context = ('file: {} '.format(self.path) if self.path
                   else 'data')
        args = ['Malformed manifest {}'.format(context),
                'Schema file: {}'.format(_SCHEMA_PATH)]
        if complaint:
            args.append('Hint: ' + complaint)
        exc = MalformedManifest(*args)
        if parent:
            raise exc from parent
        else:
            raise exc

    def _load(self, manifest, path_hint):
        # Initialize this instance's fields from values given in the
        # manifest data, which must be validated according to the schema.

        if self.path:
            log.dbg('loading manifest file:',
                    self.path, level=log.VERBOSE_EXTREME)

        # We want to make an ordered map from project names to
        # corresponding Project instances. Insertion order into this
        # map should reflect the final project order including
        # manifest import resolution, which is:
        #
        # 1. Imported projects from "manifest: self: import:"
        # 2. "manifest: projects:"
        # 3. Imported projects from "manifest: projects: ... import:"
        projects = collections.OrderedDict()

        # Create the ManifestProject, and import projects from "self:".
        mp = self._load_self(manifest, path_hint, projects)

        # Add this manifest's projects to the map, then project imports.
        url_bases = {r['name']: r['url-base'] for r in
                     manifest.get('remotes', [])}
        defaults = self._load_defaults(manifest.get('defaults', {}), url_bases)
        self._load_projects(manifest, url_bases, defaults, projects)

        # The manifest is resolved. Make sure paths are unique.
        self._check_paths_are_unique(mp, projects)

        # Save the results.
        self.projects = list(projects.values())
        self.projects.insert(MANIFEST_PROJECT_INDEX, mp)
        self._projects_by_name = {'manifest': mp}
        self._projects_by_name.update(projects)
        self._projects_by_cpath = {}
        if self.topdir:
            for i, p in enumerate(self.projects):
                if i == MANIFEST_PROJECT_INDEX and not p.abspath:
                    # When from_data() is called without a path hint, mp
                    # can have a topdir but no path, and thus no abspath.
                    continue
                self._projects_by_cpath[util.canon_path(p.abspath)] = p

    def _load_self(self, manifest, path_hint, projects):
        # Handle the "self:" section in the manifest data.

        slf = manifest.get('self', {})
        path = slf.get('path', path_hint)
        mp = ManifestProject(path=path, topdir=self.topdir,
                             west_commands=slf.get('west-commands'))

        imp = slf.get('import')
        if imp is not None:
            if self._import_flags & ImportFlag.IGNORE:
                log.dbg('manifest {} self import {}: ignored'.
                        format(mp, imp),
                        level=log.VERBOSE_EXTREME)
            else:
                log.dbg('resolving self imports for:', self.path,
                        level=log.VERBOSE_EXTREME)
                self._import_from_self(mp, imp, projects)
                log.dbg('done resolving self imports for:', self.path,
                        level=log.VERBOSE_EXTREME)

        return mp

    def _assert_imports_ok(self):
        # Sanity check that we aren't calling code that does importing
        # if the flags tell us not to.
        #
        # Could be deleted if this feature stabilizes and we never hit
        # this assertion.

        assert not self._import_flags & ImportFlag.IGNORE

    def _import_from_self(self, mp, imp, projects):
        # Recursive helper to import projects from the manifest repository.
        #
        # - mp: the ManifestProject
        # - imp: "self: import: <imp>" value from manifest data
        # - projects: ordered map of Project instances we've already got
        #
        # All data is read from the file system. Requests to read
        # files which don't exist or aren't ordinary files/directories
        # raise MalformedManifest.
        #
        # This is unlike importing from projects -- for projects, data
        # are read from Git (treating it as a content-addressable file
        # system) with a fallback on self._importer.

        self._assert_imports_ok()

        self.has_imports = True

        imptype = type(imp)
        if imptype == bool:
            self._malformed('got "self: import: {}" of boolean'.format(imp))
        elif imptype == str:
            self._import_path_from_self(mp, imp, projects)
        elif imptype == list:
            for subimp in imp:
                self._import_from_self(mp, subimp, projects)
        elif imptype == dict:
            self._import_map_from_self(mp, imp, projects)
        else:
            self._malformed('{}: "self: import: {}" has invalid type {}'.
                            format(mp.abspath, imp, imptype))

    def _import_path_from_self(self, mp, imp, projects):
        if mp.abspath:
            # Fast path, when we're working inside a fully initialized
            # topdir.
            log.dbg('manifest repository root:', mp.abspath,
                    level=log.VERBOSE_EXTREME)
            repo_root = Path(mp.abspath)
        else:
            # Fallback path, which is needed by at least west init. If
            # this happens too often, something may be wrong with how
            # we've implemented this. We'd like to avoid too many git
            # commands, as subprocesses are slow on windows.
            log.dbg('searching for the manifest repository root',
                    level=log.VERBOSE_EXTREME)
            repo_root = Path(mp.git('rev-parse --show-toplevel',
                                    capture_stdout=True,
                                    cwd=str(Path(self.path).parent)).
                             stdout[:-1].      # chop off newline
                             decode('utf-8'))  # hopefully this is safe
        p = Path(repo_root) / imp

        if p.is_file():
            log.dbg('found submanifest: {}'.format(p),
                    level=log.VERBOSE_EXTREME)
            self._import_pathobj_from_self(mp, p, projects)
        elif p.is_dir():
            log.dbg('found directory of submanifests: {}'.format(p),
                    level=log.VERBOSE_EXTREME)
            for yml in filter(_is_yml, sorted(p.iterdir())):
                self._import_pathobj_from_self(mp, p / yml, projects)
        else:
            # This also happens for special files like character
            # devices, but it doesn't seem worth handling that error
            # separately. Who would call mknod in their manifest repo?
            self._malformed('{}: "self: import: {}": file {} not found'.
                            format(mp.abspath, imp, p))

    def _import_map_from_self(self, mproject, map, projects):     # TODO
        raise NotImplementedError('import: <map> is not yet implemented')

    def _import_pathobj_from_self(self, mp, pathobj, projects):
        # Import a Path object, which is a manifest file in the
        # manifest repository whose ManifestProject is mp.

        submp = Manifest(source_file=str(pathobj),
                         manifest_path=mp.path,
                         topdir=self.topdir,
                         importer=self._importer,
                         import_flags=self._import_flags)

        for i, project in enumerate(submp.projects):
            if i == MANIFEST_PROJECT_INDEX:
                # If the submanifest has west commands, add them
                # to mp's.
                subcmds = project.west_commands
                if not subcmds:
                    continue

                if isinstance(subcmds, str):
                    subcmds = [subcmds]

                if isinstance(mp.west_commands, str):
                    mp.west_commands = [mp.west_commands]
                elif not mp.west_commands:
                    mp.west_commands = []
                mp.west_commands.extend(project.west_commands)
            else:
                self._add_project(project, projects)

    def _load_defaults(self, md, url_bases):
        # md = manifest defaults (dictionary with values parsed from
        # the manifest)
        mdrem = md.get('remote')
        if mdrem:
            # The default remote name, if provided, must refer to a
            # well-defined remote.
            if mdrem not in url_bases:
                self._malformed('default remote {} is not defined'.
                                format(mdrem))
        return _defaults(mdrem, md.get('revision', _DEFAULT_REV))

    def _load_projects(self, manifest, url_bases, defaults, projects,
                       path_hint=None):
        # Load projects and add them to the list, returning
        # information about which ones have imports that need to be
        # processed next.

        if not path_hint:
            path_hint = self.path

        have_imports = []
        names = set()
        for pd in manifest['projects']:
            project = self._load_project(pd, url_bases, defaults)
            name = project.name

            if name in names:
                # Project names must be unique within a manifest.
                self._malformed('project name {} used twice in {}'.
                                format(name, path_hint or 'the same manifest'))
            names.add(name)

            # Add the project to the map if it's new.
            added = self._add_project(project, projects)
            if added:
                log.dbg('manifest file {}: added {}'.
                        format(self.path, project),
                        level=log.VERBOSE_EXTREME)
                # Track project imports unless we are ignoring those.
                imp = pd.get('import')
                if imp:
                    if self._import_flags & ImportFlag.IGNORE:
                        log.dbg('project {} import {} ignored'.
                                format(project, imp),
                                level=log.VERBOSE_EXTREME)
                    else:
                        have_imports.append((project, imp))

        # Handle imports from new projects in our "projects:" section.
        for project, imp in have_imports:
            self._import_from_project(project, imp, projects)

    def _load_project(self, pd, url_bases, defaults):
        # pd = project data (dictionary with values parsed from the
        # manifest)

        name = pd['name']

        # The name "manifest" cannot be used as a project name; it
        # is reserved to refer to the manifest repository itself
        # (e.g. from "west list"). Note that this has not always
        # been enforced, but it is part of the documentation.
        if name == 'manifest':
            self._malformed('no project can be named "manifest"')

        # Figure out the project's fetch URL:
        #
        # - url is tested first (and can't be used with remote or repo-path)
        # - remote is tested next (and must be defined if present)
        # - default remote is tested last, if there is one
        url = pd.get('url')
        remote = pd.get('remote')
        repo_path = pd.get('repo-path')
        if remote and url:
            self._malformed('project {} has both "remote: {}" and "url: {}"'.
                            format(name, remote, url))
        if defaults.remote and not (remote or url):
            remote = defaults.remote

        if url:
            if repo_path:
                self._malformed('project {} has "repo_path: {}" and "url: {}"'.
                                format(name, repo_path, url))
        elif remote:
            if remote not in url_bases:
                self._malformed('project {} remote {} is not defined'.
                                format(name, remote))
            url = url_bases[remote] + '/' + (repo_path or name)
        else:
            self._malformed(
                'project {} has no remote or url and no default remote is set'.
                format(name))

        return Project(name, url, pd.get('revision', defaults.revision),
                       pd.get('path', name), clone_depth=pd.get('clone-depth'),
                       west_commands=pd.get('west-commands'),
                       topdir=self.topdir)

    def _import_from_project(self, project, imp, projects):
        # Recursively resolve a manifest import from 'project'.
        #
        # - project: Project instance to import from
        # - imp: the parsed value of project's import key (string, list, etc.)
        # - projects: ordered dictionary of projects

        self._assert_imports_ok()

        self.has_imports = True

        imptype = type(imp)
        if imptype == bool:
            if imp is False:
                return
            self._import_path_from_project(project, _WEST_YML, projects)
        elif imptype == str:
            self._import_path_from_project(project, imp, projects)
        elif imptype == list:
            for subimp in imp:
                self._import_from_project(project, subimp, projects)
        elif imptype == dict:
            self._import_map_from_project(project, imp, projects)
        else:
            self._malformed(project.format(
                '{name_and_path}: invalid import {imp} type: {imptype}',
                imp=imp, imptype=imptype))

    def _import_path_from_project(self, project, path, projects):
        # Import data from git at the given path at revision manifest-rev.
        # Fall back on self._importer if that fails.

        imported = self._import_content_from_project(project, path)
        if imported is None:
            # This can happen if self._importer returns None.
            # It means there's nothing to do.
            return

        has_wc = bool(project.west_commands)
        inherited_wc = []
        for data in imported:
            if isinstance(data, str):
                data = yaml.safe_load(data)
            try:
                # Force a fallback onto manifest_path=project.path.
                # The subpath to the manifest file itself will not be
                # available, so that's the best we can do.
                del data['manifest']['self']['path']
            except KeyError:
                pass
            submp = Manifest(source_data=data,
                             manifest_path=project.path,
                             topdir=self.topdir,
                             importer=self._importer,
                             import_flags=self._import_flags)

            for i, subp in enumerate(submp.projects):
                if i == MANIFEST_PROJECT_INDEX:
                    # If the project has no west commands, inherit them
                    # from imported manifest data inside the project.
                    if not has_wc and subp.west_commands:
                        if isinstance(subp.west_commands, str):
                            inherited_wc.append(subp.west_commands)
                        else:
                            inherited_wc.extend(subp.west_commands)
                else:
                    self._add_project(subp, projects)
        if not has_wc and inherited_wc:
            project.west_commands = inherited_wc

    def _import_content_from_project(self, project, path):
        log.dbg(f'manifest file {self.path}: resolving import {path} '
                f'for {project}',
                level=log.VERBOSE_EXTREME)
        if not (self._import_flags & ImportFlag.FORCE_PROJECTS) and \
           project.is_cloned():
            try:
                content = _manifest_content_at(project, path)
            except MalformedManifest as mm:
                self._malformed(mm.args[0])
            except FileNotFoundError:
                # We may need to fetch a new manifest-rev, e.g. if
                # revision is a branch that didn't used to have a
                # west.yml, but now does.
                content = self._importer(project, path)
            except subprocess.CalledProcessError:
                # We may need a new manifest-rev, e.g. if revision is
                # a SHA we don't have yet.
                content = self._importer(project, path)
        else:
            # We need to clone this project, or we were specifically
            # asked to use the importer.
            content = self._importer(project, path)

        if isinstance(content, str):
            content = [content]

        return content

    def _import_map_from_project(self, project, map, projects):  # TODO
        raise NotImplementedError('import: <map> from project unimplemented')

    def _add_project(self, project, projects):
        # Add the project to our map if we don't already know about it.
        # Return the result.

        if project.name not in projects:
            projects[project.name] = project
            return True
        else:
            return False

    def _check_paths_are_unique(self, mp, projects):
        ppaths = {}
        if mp.path:
            mppath = PurePath(mp.path)
        else:
            mppath = None
        for name, project in projects.items():
            pp = PurePath(project.path)
            if pp == mppath:
                self._malformed('project {} path "{}" '
                                'is taken by the manifest repository'.
                                format(name, project.path))
            other = ppaths.get(pp)
            if other:
                self._malformed('project {} path "{}" is taken by project {}'.
                                format(name, project.path, other.name))
            ppaths[pp] = project


class MalformedManifest(Exception):
    '''Manifest parsing failed due to invalid data.
    '''

class MalformedConfig(Exception):
    '''The west configuration was malformed in a way that made a
    manifest operation fail.
    '''

class ManifestImportFailed(Exception):
    '''An operation required to resolve a manifest failed.

    Attributes:

    - ``project``: the Project instance with the missing manifest data
    - ``filename``: the missing file
    '''

    def __init__(self, project, filename):
        self.project = project
        self.filename = filename

class ManifestVersionError(Exception):
    '''The manifest required a version of west more recent than the
    current version.
    '''

    def __init__(self, version, file=None):
        self.version = version
        '''The minimum version of west that was required.'''

        self.file = file
        '''The file that required this version of west.'''

class Project:
    '''Represents a project defined in a west manifest.

    Attributes:

    - ``name``: project's unique name
    - ``url``: project fetch URL
    - ``revision``: revision to fetch from ``url`` when the
      project is updated
    - ``path``: relative path to the project within the installation
      (i.e. from ``topdir`` if that is set)
    - ``abspath``: absolute path to the project in the native path name
      format (or ``None`` if ``topdir`` is)
    - ``posixpath``: like ``abspath``, but with slashes (``/``) as
      path separators
    - ``clone_depth``: clone depth to fetch when first cloning the
      project, or ``None`` (the revision should not be a SHA
      if this is used)
    - ``west_commands``: list of places to find extension commands in
      the project
    - ``topdir``: the top level directory of the west installation
      the project is part of, or ``None``
    '''

    def __eq__(self, other):
        return NotImplemented

    def __repr__(self):
        return ('Project("{}", "{}", revision="{}", path="{}", '
                'clone_depth={}, west_commands={}, topdir={})').format(
                    self.name, self.url, self.revision, self.path,
                    self.clone_depth, _quote_maybe(self.west_commands),
                    _quote_maybe(self.topdir))

    def __str__(self):
        return '<Project {} ({}) at {}>'.format(
            self.name, repr(self.abspath or self.path), self.revision)

    def __init__(self, name, url, revision=None, path=None,
                 clone_depth=None, west_commands=None, topdir=None):
        '''Project constructor.

        If *topdir* is ``None``, then absolute path attributes
        (``abspath`` and ``posixpath``) will also be ``None``.

        :param name: project's ``name:`` attribute in the manifest
        :param url: fetch URL
        :param revision: fetch revision
        :param path: path (relative to topdir), or None for *name*
        :param clone_depth: depth to use for initial clone
        :param west_commands: path to west commands directory in the
            project, relative to its own base directory, topdir / path,
            or list of these
        :param topdir: the west installation's top level directory
        '''

        self.name = name
        self.url = url
        self.revision = revision or _DEFAULT_REV
        self.path = path or name
        self.clone_depth = clone_depth
        self.west_commands = west_commands
        self.topdir = topdir

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
        ret['revision'] = self.revision
        if self.path != self.name:
            ret['path'] = self.path
        if self.clone_depth:
            ret['clone-depth'] = self.clone_depth
        if self.west_commands:
            ret['west-commands'] = self.west_commands

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
        '''Get the SHA for a project revision.

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
        log.dbg(self.format('{name}: checking if cloned'),
                level=log.VERBOSE_EXTREME)
        res = self.git('rev-parse --show-cdup', check=False,
                       capture_stderr=True, capture_stdout=True)

        return not (res.returncode or res.stdout.strip())

    def read_at(self, path, rev=None, cwd=None):
        '''Read file contents in the project at a specific revision.

        The file contents are returned as a bytes object. The caller
        should decode them if necessary.

        :param path: relative path to file in this project
        :param rev: revision to read *path* from (default: ``self.revision``)
        :param cwd:  directory to run command in (default: ``self.abspath``)
        '''
        if rev is None:
            rev = self.revision
        cp = self.git(['show', rev + ':' + path], capture_stdout=True,
                      capture_stderr=True, cwd=cwd)
        return cp.stdout

    def listdir_at(self, path, rev=None, cwd=None, encoding=None):
        '''List directory contents in the project at a specific revision.

        The return value is a list of the directory's contents as
        strings.

        :param path: relative path to file in this project
        :param rev: revision to read *path* from (default: ``self.revision``)
        :param cwd: directory to run command in (default: ``self.abspath``)
        :param encoding: directory contents encoding (default: 'utf-8')
        '''
        if rev is None:
            rev = self.revision
        if encoding is None:
            encoding = 'utf-8'

        # git-ls-tree -z means we get NUL-separated output with no quoting
        # of the file names. Using 'git-show' or 'git-cat-file -p'
        # wouldn't work for files with special characters in their names.
        out = self.git(['ls-tree', '-z', "{}:{}".format(rev, path)],
                       capture_stdout=True, capture_stderr=True).stdout

        # A tab character separates the SHA from the file name in each
        # NUL-separated entry.
        return [f.decode(encoding).split('\t', 1)[1:]
                for f in out.split(b'\x00') if f]

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
      ``self:`` map. This may be a list of such if the self
      section imports multiple additional files with west commands.

    Other readable attributes included for Project compatibility:

    - ``url``: always ``None``; the west manifest is not
      version-controlled by west itself, even though 'west init'
      can fetch a manifest repository from a Git remote
    - ``revision``: ``"HEAD"``
    - ``clone_depth``: ``None``, because ``url`` is
    '''

    def __repr__(self):
        return 'ManifestProject({}, path={}, west_commands={}, topdir={})'. \
            format(self.name, self.path, self.west_commands, self.topdir)

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

_defaults = collections.namedtuple('_defaults', 'remote revision')
_YML_EXTS = ['yml', 'yaml']
_WEST_YML = 'west.yml'
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "manifest-schema.yml")
_SCHEMA_VER = parse_version(SCHEMA_VERSION)
_EARLIEST_VER_STR = '0.6.99'  # we introduced the version feature after 0.6
_EARLIEST_VER = parse_version(_EARLIEST_VER_STR)
_DEFAULT_REV = 'master'

def _quote_maybe(string):
    if string:
        return f'"{string}"'
    else:
        return None

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
    return os.path.join(topdir, _mpath(topdir=topdir), _WEST_YML)

def _manifest_content_at(project, path, rev=QUAL_MANIFEST_REV_BRANCH):
    # Get a list of manifest data from project at path
    #
    # The data are loaded from Git at ref QUAL_MANIFEST_REV_BRANCH,
    # *NOT* the file system.
    #
    # If path is a tree at that ref, the contents of the YAML files
    # inside path are returned, as strings. If it's a file at that
    # ref, it's a string with its contents.
    #
    # Though this module and the "west update" implementation share
    # this code, it's an implementation detail, not API.

    log.dbg(project.format('{name}: looking up path {path} type at {rev}',
                           path=path, rev=rev),
            level=log.VERBOSE_EXTREME)

    # Returns 'blob', 'tree', etc. for path at revision, if it exists.
    out = project.git(['ls-tree', rev, path], capture_stdout=True,
                      capture_stderr=True).stdout

    if not out:
        # It's a bit inaccurate to raise FileNotFoundError for
        # something that isn't actually file, but this is internal
        # API, and git is a content addressable file system, so close
        # enough!
        raise OSError(errno.ENOENT, os.strerror(errno.ENOENT), path)

    ptype = out.decode('utf-8').split()[1]

    if ptype == 'blob':
        # Importing a file: just return its content.
        return project.read_at(path, rev=rev).decode('utf-8')
    elif ptype == 'tree':
        # Importing a tree: return the content of the YAML files inside it.
        ret = []
        pathobj = PurePath(path)
        for f in filter(_is_yml, project.listdir_at(path)):
            ret.append(project.read_at(str(pathobj / f),
                                       rev=rev).decode('utf-8'))
        return ret
    else:
        raise MalformedManifest(
            "can't decipher project {} path {} revision {} (git type: {})".
            format(project.name, path, rev, ptype))

def _is_yml(path):
    return os.path.splitext(str(path))[1][1:] in _YML_EXTS

def _default_importer(project, file):
    raise ManifestImportFailed(project, file)
