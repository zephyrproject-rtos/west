# Copyright (c) 2018, 2019, 2020 Nordic Semiconductor ASA
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
import logging
import os
from pathlib import PurePath, PurePosixPath, Path
import shlex
import subprocess

from packaging.version import parse as parse_version
import pykwalify.core
import yaml

from west import util
import west.configuration as cfg

#: Index in a Manifest.projects attribute where the `ManifestProject`
#: instance for the workspace is stored.
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
SCHEMA_VERSION = '0.7'
# MAINTAINERS:
#
# If you want to update the schema version, you need to make sure that
# it has the exact same value as west.version.__version__ when the
# next release is cut.

_logger = logging.getLogger(__name__)

def manifest_path():
    '''Absolute path of the manifest file in the current workspace.

    Exceptions raised:

        - `west.util.WestNotFound` if called from outside of a west
          workspace

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
        as_str = data
        data = _load(data)
        if not isinstance(data, dict):
            raise MalformedManifest(f'{as_str} is not a YAML dictionary')
    elif not isinstance(data, dict):
        raise TypeError(f'{data} has type {type(data)}, '
                        'expected valid manifest data')

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
                f'invalid version {min_version}; '
                f'lowest schema version is {_EARLIEST_VER_STR}')

    try:
        pykwalify.core.Core(source_data=data,
                            schema_files=[_SCHEMA_PATH]).validate()
    except pykwalify.errors.SchemaError as se:
        raise MalformedManifest(se._msg) from se

class ImportFlag(enum.IntFlag):
    '''Bit flags for handling imports when resolving a manifest.

    The DEFAULT (0) value allows reading the file system to resolve
    "self: import:", and running git to resolve a "projects:" import.
    Other flags:

    - IGNORE: ignore all "import:" attributes in "self:" and "projects:"
    - FORCE_PROJECTS: always invoke importer callback for "projects:" imports
    - IGNORE_PROJECTS: ignore "import:" attributes in "projects:" only;
      still respect "import:" in "self:"
    '''

    DEFAULT = 0
    IGNORE = 1
    FORCE_PROJECTS = 2
    IGNORE_PROJECTS = 4

def _flags_ok(flags):
    # Sanity-check the combination of flags.
    F_I = ImportFlag.IGNORE
    F_FP = ImportFlag.FORCE_PROJECTS
    F_IP = ImportFlag.IGNORE_PROJECTS

    if (flags & F_I) or (flags & F_IP):
        return not (flags & F_FP)
    elif flags & (F_FP | F_IP):
        return (flags & F_FP) ^ (flags & F_IP)
    else:
        return True

class Manifest:
    '''The parsed contents of a west manifest file.
    '''

    @staticmethod
    def from_file(source_file=None, **kwargs):
        '''Manifest object factory given a source YAML file.

        The default behavior is to find the current west workspace's
        manifest file and resolve it.

        Results depend on the keyword arguments given in *kwargs*:

            - If both *source_file* and *topdir* are given, the
              returned Manifest object is based on the data in
              *source_file*, rooted at *topdir*. The configuration
              files are not read in this case. This allows parsing a
              manifest file "as if" its project hierarchy were rooted
              at another location in the system.

            - If neither *source_file* nor *topdir* is given, the file
              system is searched for *topdir*. That workspace's
              ``manifest.path`` configuration option is used to find
              *source_file*, ``topdir/<manifest.path>/west.yml``.

            - If only *source_file* is given, *topdir* is found
              starting there. The directory containing *source_file*
              doesn't have to be ``manifest.path`` in this case.

            - If only *topdir* is given, that workspace's
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
              workspace root

        :param source_file: source file to load
        :param kwargs: Manifest.__init__ keyword arguments
        '''
        topdir = kwargs.get('topdir')

        if topdir is None:
            if source_file is None:
                # neither source_file nor topdir: search the filesystem
                # for the workspace and use its manifest.path.
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

            # Verify topdir is a real west workspace root.
            msg = f'topdir {topdir} is not a west workspace root'
            try:
                real_topdir = util.west_topdir(start=topdir, fall_back=False)
            except util.WestNotFound:
                raise ValueError(msg)
            if PurePath(topdir) != PurePath(real_topdir):
                raise ValueError(f'{msg}; but {real_topdir} is')

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
                 import_flags=0, **kwargs):
        '''
        Using `from_file` or `from_data` is usually easier than direct
        instantiation.

        Instance attributes:

            - ``projects``: sequence of `Project`

            - ``topdir``: west workspace top level directory, or
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

            - `ManifestImportFailed`: if the manifest could not be
              resolved due to import errors

            - `ManifestVersionError`: if this version of west is too
              old to parse the manifest

            - `WestNotFound`: if *topdir* was needed and not found

            - ``ValueError``: for other invalid arguments

        :param source_file: YAML file containing manifest data
        :param source_data: parsed YAML data as a Python object, or a
            string containing unparsed YAML data
        :param manifest_path: fallback `ManifestProject` ``path``
            attribute
        :param topdir: used as the west workspace top level
            directory
        :param importer: callback to resolve missing manifest import
            data
        :param import_flags: bit mask, controls import resolution
        '''
        if source_file and source_data:
            raise ValueError('both source_file and source_data were given')
        if not _flags_ok(import_flags):
            raise ValueError(f'bad import_flags {import_flags:x}')

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
            source_data = _load(source_data)

        # Validate the manifest. Wrap a couple of the exceptions with
        # extra context about the problematic file in case of errors,
        # to help debugging.
        try:
            validate(source_data)
        except ManifestVersionError as mv:
            raise ManifestVersionError(mv.version, file=source_file) from mv
        except MalformedManifest as mm:
            self._malformed(mm.args[0], parent=mm)
        except TypeError as te:
            self._malformed(te.args[0], parent=te)

        self.projects = None
        '''Sequence of `Project` objects representing manifest
        projects.

        Index 0 (`MANIFEST_PROJECT_INDEX`) contains a
        `ManifestProject` representing the manifest repository. The
        rest of the sequence contains projects in manifest file order
        (or resolution order if the manifest contains imports).
        '''

        self.topdir = topdir
        '''The west workspace's top level directory, or None.'''

        self.has_imports = False

        # Set up the public attributes documented above, as well as
        # any internal attributes needed to implement the public API.
        self._importer = importer or _default_importer
        self._import_flags = import_flags
        self._load(source_data['manifest'], manifest_path,
                   kwargs.get('import-context', _import_ctx({}, None)))

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
                if pid == self.projects[MANIFEST_PROJECT_INDEX].path:
                    project = self.projects[MANIFEST_PROJECT_INDEX]
                else:
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

        # This relies on insertion-ordered dictionaries for
        # predictability, which is a CPython 3.6 implementation detail
        # and Python 3.7+ guarantee.
        r = {}
        r['manifest'] = {}
        r['manifest']['projects'] = project_dicts
        r['manifest']['self'] = self.projects[MANIFEST_PROJECT_INDEX].as_dict()

        return r

    def as_dict(self):
        '''Returns a dict representing self, fully resolved.

        The value is "resolved" in that the result is as if all
        projects had been defined in a single manifest without any
        import attributes.
        '''
        return self._as_dict_helper()

    def as_frozen_dict(self):
        '''Returns a dict representing self, but frozen.

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

    def as_yaml(self, **kwargs):
        '''Returns a YAML representation for self, fully resolved.

        The value is "resolved" in that the result is as if all
        projects had been defined in a single manifest without any
        import attributes.

        :param kwargs: passed to yaml.safe_dump()
        '''
        return yaml.safe_dump(self.as_dict(), **kwargs)

    def as_frozen_yaml(self, **kwargs):
        '''Returns a YAML representation for self, but frozen.

        The value is "frozen" in that all project revisions are the
        full SHAs pointed to by `QUAL_MANIFEST_REV_BRANCH` references.

        Raises ``RuntimeError`` if a project SHA can't be resolved.

        :param kwargs: passed to yaml.safe_dump()
        '''
        return yaml.safe_dump(self.as_frozen_dict(), **kwargs)

    def _malformed(self, complaint, parent=None):
        context = (f'file: {self.path} ' if self.path else 'data')
        args = [f'Malformed manifest {context}',
                f'Schema file: {_SCHEMA_PATH}']
        if complaint:
            args.append('Hint: ' + complaint)
        exc = MalformedManifest(*args)
        if parent:
            raise exc from parent
        else:
            raise exc

    def _load(self, manifest, path_hint, ctx):
        # Initialize this instance.
        #
        # - manifest: manifest data, parsed and validated
        # - path_hint: optional hint about where the manifest repo lives
        # - ctx: import context, an _import_ctx tuple

        top_level = not bool(ctx.projects)

        if self.path:
            loading_what = self.path
        else:
            loading_what = 'data (no file)'

        _logger.debug(f'loading {loading_what}')

        # We want to make an ordered map from project names to
        # corresponding Project instances. Insertion order into this
        # map should reflect the final project order including
        # manifest import resolution, which is:
        #
        # 1. Imported projects from "manifest: self: import:"
        # 2. "manifest: projects:"
        # 3. Imported projects from "manifest: projects: ... import:"

        # Create the ManifestProject, and import projects from "self:".
        mp = self._load_self(manifest, path_hint, ctx)

        # Add this manifest's projects to the map, then project imports.
        url_bases = {r['name']: r['url-base'] for r in
                     manifest.get('remotes', [])}
        defaults = self._load_defaults(manifest.get('defaults', {}), url_bases)
        self._load_projects(manifest, url_bases, defaults, ctx)

        # The manifest is resolved. Make sure paths are unique.
        self._check_paths_are_unique(mp, ctx.projects, top_level)

        # Save the results.
        self.projects = list(ctx.projects.values())
        self.projects.insert(MANIFEST_PROJECT_INDEX, mp)
        self._projects_by_name = {'manifest': mp}
        self._projects_by_name.update(ctx.projects)
        self._projects_by_cpath = {}
        if self.topdir:
            for i, p in enumerate(self.projects):
                if i == MANIFEST_PROJECT_INDEX and not p.abspath:
                    # When from_data() is called without a path hint, mp
                    # can have a topdir but no path, and thus no abspath.
                    continue
                self._projects_by_cpath[util.canon_path(p.abspath)] = p

        _logger.debug(f'loaded {loading_what}')

    def _load_self(self, manifest, path_hint, ctx):
        # Handle the "self:" section in the manifest data.

        slf = manifest.get('self', {})
        path = slf.get('path', path_hint)
        mp = ManifestProject(path=path, topdir=self.topdir,
                             west_commands=slf.get('west-commands'))

        imp = slf.get('import')
        if imp is not None:
            if self._import_flags & ImportFlag.IGNORE:
                _logger.debug('ignored self import')
            else:
                _logger.debug(f'resolving self import {imp}')
                self._import_from_self(mp, imp, ctx)
                _logger.debug('resolved self import')

        return mp

    def _assert_imports_ok(self):
        # Sanity check that we aren't calling code that does importing
        # if the flags tell us not to.
        #
        # Could be deleted if this feature stabilizes and we never hit
        # this assertion.

        assert not self._import_flags & ImportFlag.IGNORE

    def _import_from_self(self, mp, imp, ctx):
        # Recursive helper to import projects from the manifest repository.
        #
        # - mp: the ManifestProject
        # - imp: "self: import: <imp>" value from manifest data
        # - projects: ordered map of Project instances we've already got
        # - filter_fn: predicate for whether it's OK to add a project,
        #   or None if all projects are OK
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
            self._malformed(f'got "self: import: {imp}" of boolean')
        elif imptype == str:
            self._import_path_from_self(mp, imp, ctx)
        elif imptype == list:
            for subimp in imp:
                self._import_from_self(mp, subimp, ctx)
        elif imptype == dict:
            imap = self._load_imap(mp, imp)
            # Since 'imap' may introduce additional filtering
            # requirements on top of the existing 'filter_fn', we need
            # to compose them, e.g. to respect import map whitelists
            # and blacklists from higher up in the recursion tree.
            self._import_path_from_self(mp, imap.file,
                                        _new_ctx(ctx, _imap_filter(imap)))
        else:
            self._malformed(f'{mp.abspath}: "self: import: {imp}" '
                            f'has invalid type {imptype}')

    def _import_path_from_self(self, mp, imp, ctx):
        if mp.abspath:
            # Fast path, when we're working inside a fully initialized
            # topdir.
            repo_root = Path(mp.abspath)
        else:
            # Fallback path, which is needed by at least west init. If
            # this happens too often, something may be wrong with how
            # we've implemented this. We'd like to avoid too many git
            # commands, as subprocesses are slow on windows.
            start = Path(self.path).parent
            _logger.debug(
                f'searching for manifest repository root from {start}')
            repo_root = Path(mp.git('rev-parse --show-toplevel',
                                    capture_stdout=True,
                                    cwd=start).
                             stdout[:-1].      # chop off newline
                             decode('utf-8'))  # hopefully this is safe
        p = repo_root / imp

        if p.is_file():
            _logger.debug(f'found submanifest file: {p}')
            self._import_pathobj_from_self(mp, p, ctx)
        elif p.is_dir():
            _logger.debug(f'found submanifest directory: {p}')
            for yml in filter(_is_yml, sorted(p.iterdir())):
                self._import_pathobj_from_self(mp, p / yml, ctx)
        else:
            # This also happens for special files like character
            # devices, but it doesn't seem worth handling that error
            # separately. Who would call mknod in their manifest repo?
            self._malformed(f'{mp.abspath}: "self: import: {imp}": '
                            f'file {p} not found')

    def _import_pathobj_from_self(self, mp, pathobj, ctx):
        # Import a Path object, which is a manifest file in the
        # manifest repository whose ManifestProject is mp.

        # Destructively add the imported content into our 'projects'
        # map, passing along our context. The intermediate manifest is
        # thrown away; we're basically just using __init__ as a
        # function here.
        #
        # The only thing we need to do with it is check if the
        # submanifest has west commands, add them to mp's if so.
        try:
            submp = Manifest(source_file=str(pathobj),
                             manifest_path=mp.path,
                             topdir=self.topdir,
                             importer=self._importer,
                             import_flags=self._import_flags,
                             **{'import-context':
                                ctx}).projects[MANIFEST_PROJECT_INDEX]
        except RecursionError as e:
            raise _ManifestImportDepth(mp, pathobj) from e

        # submp.west_commands comes first because we
        # logically treat imports from self as if they are
        # defined before the contents in the higher level
        # manifest.
        mp.west_commands = self._merge_wcs(submp.west_commands,
                                           mp.west_commands)

    def _load_defaults(self, md, url_bases):
        # md = manifest defaults (dictionary with values parsed from
        # the manifest)
        mdrem = md.get('remote')
        if mdrem:
            # The default remote name, if provided, must refer to a
            # well-defined remote.
            if mdrem not in url_bases:
                self._malformed(f'default remote {mdrem} is not defined')
        return _defaults(mdrem, md.get('revision', _DEFAULT_REV))

    def _load_projects(self, manifest, url_bases, defaults, ctx):
        # Load projects and add them to the list, returning
        # information about which ones have imports that need to be
        # processed next.

        have_imports = []
        names = set()
        for pd in manifest['projects']:
            project = self._load_project(pd, url_bases, defaults)
            name = project.name

            if not _filter_ok(ctx.filter_fn, project):
                _logger.debug(f'project {name} in file {self.path} ' +
                              'ignored due to filters')
                continue

            if name in names:
                # Project names must be unique within a manifest.
                self._malformed(f'project name {name} used twice in ' +
                                (self.path or 'the same manifest'))
            names.add(name)

            # Add the project to the map if it's new.
            added = self._add_project(project, ctx.projects)
            if added:
                # Track project imports unless we are ignoring those.
                imp = pd.get('import')
                if imp:
                    if self._import_flags & (ImportFlag.IGNORE |
                                             ImportFlag.IGNORE_PROJECTS):
                        _logger.debug(
                            f'project {project}: ignored import ({imp})')
                    else:
                        have_imports.append((project, imp))

        # Handle imports from new projects in our "projects:" section.
        for project, imp in have_imports:
            self._import_from_project(project, imp, ctx)

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
            self._malformed(f'project {name} has both "remote: {remote}" '
                            f'and "url: {url}"')
        if defaults.remote and not (remote or url):
            remote = defaults.remote

        if url:
            if repo_path:
                self._malformed(f'project {name} has "repo_path: {repo_path}" '
                                f'and "url: {url}"')
        elif remote:
            if remote not in url_bases:
                self._malformed(f'project {name} remote {remote} '
                                'is not defined')
            url = url_bases[remote] + '/' + (repo_path or name)
        else:
            self._malformed(
                f'project {name} '
                'has no remote or url and no default remote is set')

        return Project(name, url, pd.get('revision', defaults.revision),
                       pd.get('path', name), clone_depth=pd.get('clone-depth'),
                       west_commands=pd.get('west-commands'),
                       topdir=self.topdir, remote_name=remote)

    def _import_from_project(self, project, imp, ctx):
        # Recursively resolve a manifest import from 'project'.
        #
        # - project: Project instance to import from
        # - imp: the parsed value of project's import key (string, list, etc.)
        # - ctx: import context, an _import_ctx tuple

        self._assert_imports_ok()

        self.has_imports = True

        imptype = type(imp)
        if imptype == bool:
            # We should not have been called unless the import was truthy.
            assert imp
            self._import_path_from_project(project, _WEST_YML, ctx)
        elif imptype == str:
            self._import_path_from_project(project, imp, ctx)
        elif imptype == list:
            for subimp in imp:
                self._import_from_project(project, subimp, ctx)
        elif imptype == dict:
            imap = self._load_imap(project, imp)
            # Similar comments about composing filters apply here as
            # they do in _import_from_self().
            self._import_path_from_project(project, imap.file,
                                           _new_ctx(ctx, _imap_filter(imap)))
        else:
            self._malformed(f'{project.name_and_path}: invalid import {imp} '
                            f'type: {imptype}')

    def _import_path_from_project(self, project, path, ctx):
        # Import data from git at the given path at revision manifest-rev.
        # Fall back on self._importer if that fails.

        _logger.debug(f'resolving import {path} for {project}')
        imported = self._import_content_from_project(project, path)
        if imported is None:
            # This can happen if self._importer returns None.
            # It means there's nothing to do.
            return

        for data in imported:
            if isinstance(data, str):
                data = _load(data)
                validate(data)
            try:
                # Force a fallback onto manifest_path=project.path.
                # The subpath to the manifest file itself will not be
                # available, so that's the best we can do.
                del data['manifest']['self']['path']
            except KeyError:
                pass

            # Destructively add the imported content into our 'projects'
            # map, passing along our context.
            try:
                submp = Manifest(source_data=data,
                                 manifest_path=project.path,
                                 topdir=self.topdir,
                                 importer=self._importer,
                                 import_flags=self._import_flags,
                                 **{'import-context': ctx}
                                 ).projects[MANIFEST_PROJECT_INDEX]
            except RecursionError as e:
                raise _ManifestImportDepth(project, path) from e

            # If the submanifest has west commands, merge them
            # into project's.
            project.west_commands = self._merge_wcs(
                project.west_commands, submp.west_commands)
        _logger.debug(f'done resolving import {path} for {project}')

    def _import_content_from_project(self, project, path):
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

    def _load_imap(self, project, imp):
        # Convert a parsed self or project import value from YAML into
        # an _import_map namedtuple.

        # Work on a copy in case the caller needs the full value.
        copy = dict(imp)
        ret = _import_map(copy.pop('file', _WEST_YML),
                          copy.pop('name-whitelist', []),
                          copy.pop('path-whitelist', []),
                          copy.pop('name-blacklist', []),
                          copy.pop('path-blacklist', []))

        # Find a useful name for the project on error.
        if isinstance(project, ManifestProject):
            what = f'manifest file {project.abspath}'
        else:
            what = f'project {project.name}'

        # Check that the value is OK.
        if copy:
            # We popped out all of the valid keys already.
            self._malformed(f'{what}: invalid import contents: {copy}')
        elif not _is_imap_list(ret.name_whitelist):
            self._malformed(f'{what}: bad import name-whitelist '
                            f'{ret.name_whitelist}')
        elif not _is_imap_list(ret.path_whitelist):
            self._malformed(f'{what}: bad import path-whitelist '
                            f'{ret.path_whitelist}')
        elif not _is_imap_list(ret.name_blacklist):
            self._malformed(f'{what}: bad import name-blacklist '
                            f'{ret.name_blacklist}')
        elif not _is_imap_list(ret.path_blacklist):
            self._malformed(f'{what}: bad import path-blacklist '
                            f'{ret.path_blacklist}')

        return ret

    def _add_project(self, project, projects):
        # Add the project to our map if we don't already know about it.
        # Return the result.

        if project.name not in projects:
            projects[project.name] = project
            _logger.debug(f'added project {project.name} '
                          f'revision {project.revision}' +
                          (f' from {self.path}' if self.path else ''))
            return True
        else:
            return False

    def _check_paths_are_unique(self, mp, projects, top_level):
        # TODO: top_level can probably go away when #327 is done.

        ppaths = {}
        if mp.path:
            mppath = PurePath(mp.path)
        else:
            mppath = None
        for name, project in projects.items():
            pp = PurePath(project.path)
            if top_level and pp == mppath:
                self._malformed(f'project {name} path "{project.path}" '
                                'is taken by the manifest repository')
            other = ppaths.get(pp)
            if other:
                self._malformed(f'project {name} path "{project.path}" '
                                f'is taken by project {other.name}')
            ppaths[pp] = project

    @staticmethod
    def _merge_wcs(wc1, wc2):
        # Merge two west_commands attributes. Try to keep the result a
        # str if possible, but upgrade it to a list if both wc1 and
        # wc2 are truthy.
        #
        # Filter out duplicates to make sure that if the user imports
        # a manifest and redundantly specifies its west-commands,
        # we don't get the same entries twice.
        if wc1 and wc2:
            wc1 = _ensure_list(wc1)
            return wc1 + [wc for wc in _ensure_list(wc2) if wc not in wc1]
        else:
            return wc1 or wc2


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

    def __str__(self):
        return (f'ManifestImportFailed: project {self.project} '
                f'file {self.filename}')

class _ManifestImportDepth(ManifestImportFailed):
    # A hack to signal to main.py what happened.
    pass

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
    - ``path``: relative path to the project within the workspace
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
    - ``topdir``: the top level directory of the west workspace
      the project is part of, or ``None``
    - ``remote_name``: the name of the remote which should be set up
      when the project is being cloned (default: 'origin')
    '''

    def __eq__(self, other):
        return NotImplemented

    def __repr__(self):
        return (f'Project("{self.name}", "{self.url}", '
                f'revision="{self.revision}", path={repr(self.path)}, '
                f'clone_depth={self.clone_depth}, '
                f'west_commands={self.west_commands}, '
                f'topdir={repr(self.topdir)})')

    def __str__(self):
        path_repr = repr(self.abspath or self.path)
        return f'<Project {self.name} ({path_repr}) at {self.revision}>'

    def __init__(self, name, url, revision=None, path=None,
                 clone_depth=None, west_commands=None, topdir=None,
                 remote_name=None):
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
        :param topdir: the west workspace's top level directory
        :param remote_name: the name of the remote which should be
            set up if the project is being cloned (default: 'origin')
        '''

        self.name = name
        self.url = url
        self.revision = revision or _DEFAULT_REV
        self.path = path or name
        self.clone_depth = clone_depth
        self.west_commands = west_commands
        self.topdir = topdir
        self.remote_name = remote_name or 'origin'

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

    @property
    def name_and_path(self):
        return f'{self.name} ({self.path})'

    def as_dict(self):
        '''Return a representation of this object as a dict, as it
        would be parsed from an equivalent YAML manifest.
        '''
        ret = {}
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

    #
    # Git helpers
    #

    def git(self, cmd, extra_args=(), capture_stdout=False,
            capture_stderr=False, check=True, cwd=None):
        '''Run a git command in the project repository.

        Returns a ``subprocess.CompletedProcess``.

        :param cmd: git command as a string (or list of strings)
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

        args = ['git'] + cmd_list + extra_args
        cmd_str = util.quote_sh_list(args)

        _logger.debug(f"running '{cmd_str}' in {cwd}")
        popen = subprocess.Popen(
            args, cwd=cwd,
            stdout=subprocess.PIPE if capture_stdout else None,
            stderr=subprocess.PIPE if capture_stderr else None)

        stdout, stderr = popen.communicate()

        # We use logger style % formatting here to avoid the
        # potentially expensive overhead of formatting long
        # stdout/stderr strings if the current log level isn't DEBUG,
        # which is the usual case.
        _logger.debug('"%s" exit code: %d stdout: %r stderr: %r',
                      cmd_str, popen.returncode, stdout, stderr)

        if check and popen.returncode:
            raise subprocess.CalledProcessError(popen.returncode, cmd_list,
                                                output=stdout, stderr=stderr)
        else:
            return subprocess.CompletedProcess(popen.args, popen.returncode,
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
        cp = self.git(f'rev-parse {rev}', capture_stdout=True, cwd=cwd,
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
        rc = self.git(f'merge-base --is-ancestor {rev1} {rev2}',
                      check=False, cwd=cwd).returncode

        if rc == 0:
            return True
        elif rc == 1:
            return False
        else:
            raise RuntimeError(f'unexpected git merge-base result {rc}')

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

        This is equivalent to ``is_up_to_date_with(self.revision,
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
        _logger.debug(f'{self.name}: checking if cloned')
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
        cp = self.git(['show', f'{rev}:{path}'], capture_stdout=True,
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
        out = self.git(['ls-tree', '-z', f'{rev}:{path}'],
                       capture_stdout=True, capture_stderr=True).stdout

        # A tab character separates the SHA from the file name in each
        # NUL-separated entry.
        return [f.decode(encoding).split('\t', 1)[1]
                for f in out.split(b'\x00') if f]

# FIXME: this whole class should just go away. See #327.
class ManifestProject(Project):
    '''Represents the manifest repository as a `Project`.

    Meaningful attributes:

    - ``name``: the string ``"manifest"``
    - ``topdir``: the top level directory of the west workspace
      the manifest project controls, or ``None``
    - ``path``: relative path to the manifest repository within the
      workspace, or ``None`` (i.e. from ``topdir`` if that is set)
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
        return (f'ManifestProject({self.name}, path={repr(self.path)}, '
                f'west_commands={self.west_commands}, '
                f'topdir={repr(self.topdir)})')

    def __init__(self, path=None, west_commands=None, topdir=None):
        '''
        :param path: Relative path to the manifest repository in the
            west workspace, if known.
        :param west_commands: path to the YAML file in the manifest
            repository configuring its extension commands, if any.
        :param topdir: Root of the west workspace the manifest
            project is inside. If not given, all absolute path
            attributes (abspath and posixpath) will be None.
        '''
        self.name = 'manifest'

        # Path related attributes
        self.topdir = topdir
        self._abspath = None
        self._posixpath = None
        self._path = path

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
        ret = {}
        if self.path:
            ret['path'] = self.path
        if self.west_commands:
            ret['west-commands'] = self.west_commands
        return ret

_defaults = collections.namedtuple('_defaults', 'remote revision')
_import_map = collections.namedtuple('_import_map',
                                     'file '
                                     'name_whitelist path_whitelist '
                                     'name_blacklist path_blacklist')
_import_ctx = collections.namedtuple('_import_ctx', [
    # Known projects map, from name to Project:
    'projects',
    # Project -> Bool. True if OK to add a project to 'projects'. A
    # None value is treated as a function which always returns True.
    'filter_fn'])
_YML_EXTS = ['yml', 'yaml']
_WEST_YML = 'west.yml'
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "manifest-schema.yml")
_SCHEMA_VER = parse_version(SCHEMA_VERSION)
_EARLIEST_VER_STR = '0.6.99'  # we introduced the version feature after 0.6
_EARLIEST_VER = parse_version(_EARLIEST_VER_STR)
_DEFAULT_REV = 'master'

def _mpath(cp=None, topdir=None):
    # Return the value of the manifest.path configuration option
    # in *cp*, a ConfigParser. If not given, create a new one and
    # load configuration options with the given *topdir* as west
    # workspace root.
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

    _logger.debug(f'{project.name}: looking up path {path} type at {rev}')

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
        # Use a PurePosixPath because that's the form git seems to
        # store internally, even on Windows. This breaks on Windows if
        # you use PurePath.
        pathobj = PurePosixPath(path)
        for f in filter(_is_yml, project.listdir_at(path, rev=rev)):
            ret.append(project.read_at(str(pathobj / f),
                                       rev=rev).decode('utf-8'))
        return ret
    else:
        raise MalformedManifest(f"can't decipher project {project.name} "
                                f'path {path} revision {rev} '
                                f'(git type: {ptype})')

def _is_yml(path):
    return os.path.splitext(str(path))[1][1:] in _YML_EXTS

def _default_importer(project, file):
    raise ManifestImportFailed(project, file)

def _load(data):
    try:
        return yaml.safe_load(data)
    except yaml.scanner.ScannerError as e:
        raise MalformedManifest(data) from e

def _new_ctx(ctx, _new_filter):
    return _import_ctx(ctx.projects, _and_filters(ctx.filter_fn, _new_filter))

def _is_imap_list(value):
    # Return True if the value is a valid import map 'blacklist' or
    # 'whitelist'. Empty strings and lists are OK, and list nothing.

    return (isinstance(value, str) or
            (isinstance(value, list) and
             all(isinstance(item, str) for item in value)))

def _filter_ok(filter_fn, project):
    # Returns True if the given filter_fn allows the project to be added
    # to the current working list in a None-safe way.

    return (filter_fn is None) or filter_fn(project)

def _and_filters(filter_fn1, filter_fn2):
    # Return a filter function which is the logical AND of the two
    # arguments. Any filter_fn which is None is treated as an
    # always-true predicate.
    #
    # The return value therefore needs to be used with _filter_ok().

    if filter_fn1 and filter_fn2:
        return lambda project: (filter_fn1(project) and filter_fn2(project))
    else:
        return filter_fn1 or filter_fn2

def _imap_filter(imap):
    # Returns either None (if no filter is necessary) or a predicate
    # function for the given import map.

    if any([imap.name_whitelist, imap.path_whitelist,
            imap.name_blacklist, imap.path_blacklist]):
        return lambda project: _is_imap_ok(imap, project)
    else:
        return None

def _is_imap_ok(imap, project):
    # Return True if a project passes an import map's filters,
    # and False otherwise.

    nwl, pwl, nbl, pbl = [_ensure_list(lst) for lst in
                          (imap.name_whitelist, imap.path_whitelist,
                           imap.name_blacklist, imap.path_blacklist)]
    name = project.name
    path = PurePath(project.path)
    blacklisted = (name in nbl) or any(path.match(p) for p in pbl)
    whitelisted = (name in nwl) or any(path.match(p) for p in pwl)
    no_whitelists = not (nwl or pwl)

    if blacklisted:
        return whitelisted
    else:
        return whitelisted or no_whitelists

def _ensure_list(item):
    # Converts item to a list containing it if item is a string, or
    # returns item.

    if isinstance(item, str):
        return [item]
    return item
