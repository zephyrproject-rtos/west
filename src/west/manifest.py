# Copyright (c) 2018, Nordic Semiconductor ASA
# Copyright 2018, Foundries.io Ltd
#
# SPDX-License-Identifier: Apache-2.0

'''Parser and abstract data types for west manifests.

The main class is Manifest. The recommended method for creating a
Manifest instance is via its from_file() or from_data() helper
methods.

There are additionally Defaults, Remote, and Project types defined,
which represent the values by the same names in a west
manifest. (I.e. "Remote" represents one of the elements in the
"remote" sequence in the manifest, and so on.) Some Default values,
such as the default project revision, may be supplied by this module
if they are not present in the manifest data.'''

import os

import pykwalify.core
import yaml

from west import util, log


META_NAMES = ['west', 'manifest']
'''Names of the special "meta-projects", which are reserved and cannot
be used to name a project in the manifest file.'''


def default_path():
    '''Return the path to the default manifest in the west directory.

    Raises WestNotFound if called from outside of a west working directory.'''
    return os.path.join(util.west_dir(), 'manifest', 'default.yml')


class Manifest:
    '''Represents the contents of a West manifest file.

    The most convenient way to construct an instance is using the
    from_file and from_data helper methods.'''

    @staticmethod
    def from_file(source_file=None):
        '''Create and return a new Manifest object given a source YAML file.

        :param source_file: Path to a YAML file containing the manifest.

        If source_file is None, the value returned by default_path()
        is used.

        Raises MalformedManifest in case of validation errors.'''
        if source_file is None:
            source_file = default_path()
        return Manifest(source_file=source_file)

    @staticmethod
    def from_data(source_data):
        '''Create and return a new Manifest object given parsed YAML data.

        :param source_data: Parsed YAML data as a Python object.

        Raises MalformedManifest in case of validation errors.'''
        return Manifest(source_data=source_data)

    def __init__(self, source_file=None, source_data=None):
        '''Create a new Manifest object.

        :param source_file: Path to a YAML file containing the manifest.
        :param source_data: Parsed YAML data as a Python object.

        Normally, it is more convenient to use the `from_file` and
        `from_data` convenience factories than calling the constructor
        directly.

        Exactly one of the source_file and source_data parameters must
        be given.

        Raises MalformedManifest in case of validation errors.'''
        if source_file and source_data:
            raise ValueError('both source_file and source_data were given')

        if source_file:
            with open(source_file, 'r') as f:
                self._data = yaml.safe_load(f.read())
            path = source_file
        else:
            self._data = source_data
            path = None

        self.path = path
        '''Path to the file containing the manifest, or None if created
        from data rather than the file system.'''

        if not self._data:
            self._malformed('manifest contains no data')

        try:
            pykwalify.core.Core(
                source_data=self._data,
                schema_files=[_SCHEMA_PATH]
            ).validate()
        except pykwalify.errors.SchemaError as e:
            self._malformed(e)

        self.defaults = None
        '''west.manifest.Defaults object representing default values
        in the manifest, either as specified by the user or west itself.'''

        self.remotes = None
        '''Sequence of west.manifest.Remote objects representing manifest
        remotes.'''

        self.projects = None
        '''Sequence of west.manifest.Project objects representing manifest
        projects.

        Each element's values are fully initialized; there is no need
        to consult the defaults field to supply missing values.'''

        # Set up the public attributes documented above, as well as
        # any internal attributes needed to implement the public API.
        self._load(self._data['manifest'])

    def get_remote(self, name):
        '''Get a manifest Remote, given its name.'''
        return self._remotes_dict[name]

    def _malformed(self, complaint):
        context = (' file {} '.format(self.path) if self.path
                   else ' data:\n{}\n'.format(self._data))
        raise MalformedManifest('Malformed manifest{}(schema: {}):\n{}'
                                .format(context, _SCHEMA_PATH, complaint))

    def _load(self, manifest):
        # Initialize this instance's fields from values given in the
        # manifest data, which must be validated according to the schema.

        projects = []
        project_abspaths = set()

        # Map from each remote's name onto that remote's data in the manifest.
        remotes = tuple(Remote(r['name'], r['url']) for r in
                        manifest['remotes'])
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

        # mp = manifest project (dictionary with values parsed from
        # the manifest)
        for mp in manifest['projects']:
            # Validate the project name.
            name = mp['name']
            if name in META_NAMES:
                self._malformed('the name "{}" is reserved and cannot '.
                                format(name) +
                                'be used to name a manifest project')

            # Validate the project remote.
            remote_name = mp.get('remote', default_remote_name)
            if remote_name is None:
                self._malformed('project {} does not specify a remote'.
                                format(name))
            if remote_name not in remotes_dict:
                self._malformed('project {} remote {} is not defined'.
                                format(name, remote_name))
            project = Project(name,
                              remotes_dict[remote_name],
                              defaults,
                              path=mp.get('path'),
                              clone_depth=mp.get('clone-depth'),
                              revision=mp.get('revision'))

            # Two projects cannot have the same path. We use absolute
            # paths to check for collisions to ensure paths are
            # normalized (e.g. for case-insensitive file systems or
            # in cases like on Windows where / or \ may serve as a
            # path component separator).
            if project.abspath in project_abspaths:
                self._malformed('project {} path {} is already in use'.
                                format(project.name, project.path))

            project_abspaths.add(project.abspath)
            projects.append(project)

        self.defaults = defaults
        self.remotes = remotes
        self._remotes_dict = remotes_dict
        self.projects = tuple(projects)


class MalformedManifest(Exception):
    '''Exception indicating that west manifest parsing failed due to a
    malformed value.'''


# Definitions for Manifest attribute types.

class Defaults:
    '''Represents default values in a manifest, either specified by the
    user or by west itself.

    Defaults are neither comparable nor hashable.'''

    __slots__ = 'remote revision'.split()

    def __init__(self, remote=None, revision=None):
        if remote is not None:
            _wrn_if_not_remote(remote)
        if revision is None:
            revision = 'master'

        self.remote = remote
        self.revision = revision

    def __eq__(self, other):
        raise NotImplemented

    def __repr__(self):
        return 'Defaults(remote={}, revision={})'.format(repr(self.remote),
                                                         repr(self.revision))


class Remote:
    '''Represents a remote defined in a west manifest.

    Remotes may be compared for equality, but are not hashable.'''

    __slots__ = 'name url'.split()

    def __init__(self, name, url):
        if url.endswith('/'):
            log.wrn('Remote', name, 'URL', url, 'ends with a slash ("/");',
                    'these are automatically appended by West')

        self.name = name
        self.url = url

    def __eq__(self, other):
        return self.name == other.name and self.url == other.url

    def __repr__(self):
        return 'Remote(name={}, url={})'.format(repr(self.name),
                                                repr(self.url))


class Project:
    '''Represents a project defined in a west manifest.

    Projects are neither comparable nor hashable.'''

    __slots__ = 'name remote url path abspath clone_depth revision'.split()

    def __init__(self, name, remote, defaults, path=None, clone_depth=None,
                 revision=None):
        '''Specify a Project by name, Remote, and optional information.

        :param name: Project's user-defined name in the manifest
        :param remote: Remote instance corresponding to this Project's remote.
                       This may not be None.
        :param path: Relative path to the project in the west
                     installation, if present in the manifest. If None,
                     the project's ``name`` is used.
        :param revision: Project revision as given in the manifest, if present.
        '''
        if remote is None:
            raise ValueError('remote may not be None')
        _wrn_if_not_remote(remote)

        self.name = name
        self.remote = remote
        self.url = remote.url + '/' + name
        self.path = path or name
        self.abspath = os.path.normpath(os.path.join(util.west_topdir(), self.path))
        self.clone_depth = clone_depth
        self.revision = revision or defaults.revision

    def __eq__(self, other):
        raise NotImplemented

    def __repr__(self):
        reprs = [repr(x) for x in
                 (self.name, self.remote, self.url, self.path,
                  self.abspath, self.clone_depth, self.revision)]
        return ('Project(name={}, remote={}, url={}, path={}, abspath={}, '
                'clone_depth={}, revision={})').format(*reprs)


def _wrn_if_not_remote(remote):
    if not isinstance(remote, Remote):
        log.wrn('Remote', remote, 'is not a Remote instance')


_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "manifest-schema.yml")
