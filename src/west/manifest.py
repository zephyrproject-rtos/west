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
import errno
import os
import shutil
import shlex
import subprocess

import configparser
import pykwalify.core
import yaml
from pathlib import PurePath

from west import util, log
from west.backports import CompletedProcess
from west.configuration import config


# Todo: take from _bootstrap?
# Default west repository URL.
WEST_URL_DEFAULT = 'https://github.com/zephyrproject-rtos/west'
# Default revision to check out of the west repository.
WEST_REV_DEFAULT = 'master'

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

    Raises: WestNotFound if called from outside of a west working directory,
    MalformedConfig if the configuration file is missing a manifest.path key,
    and FileNotFoundError if the manifest.path file doesn't exist.'''
    try:
        ret = os.path.join(util.west_topdir(),
                           config.get('manifest', 'path'),
                           'west.yml')
    except (configparser.NoOptionError, configparser.NoSectionError) as e:
        raise MalformedConfig('no "manifest.path" config option is set') from e
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
    def from_file(source_file=None):
        '''Create and return a new Manifest object given a source YAML file.

        :param source_file: Path to a YAML file containing the manifest.

        If source_file is None, the value returned by `manifest_path()`
        is used.

        Raises `MalformedManifest` in case of validation errors.
        Raises `MalformedConfig` in case of missing configuration settings.'''
        if source_file is None:
            source_file = manifest_path()
        return Manifest(source_file=source_file)

    @staticmethod
    def from_data(source_data):
        '''Create and return a new Manifest object given parsed YAML data.

        :param source_data: Parsed YAML data as a Python object.

        Raises MalformedManifest in case of validation errors.
        Raises MalformedConfig in case of missing configuration settings.'''
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

        Raises MalformedManifest in case of validation errors.
        Raises MalformedConfig in case of missing configuration settings.'''
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

        if 'manifest' not in self._data:
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
        self._load(self._data)

    def get_remote(self, name):
        '''Get a manifest Remote, given its name.'''
        return self._remotes_dict[name]

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
            sha = project.sha(QUAL_MANIFEST_REV_BRANCH)
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

    def _load(self, data):
        # Initialize this instance's fields from values given in the
        # manifest data, which must be validated according to the schema.
        projects = []
        project_names = set()
        project_abspaths = set()

        manifest = data.get('manifest')

        path = config.get('manifest', 'path', fallback=None)

        self_tag = manifest.get('self')
        if path is None:
            path = self_tag.get('path') if self_tag else ''
        west_commands = self_tag.get('west-commands') if self_tag else None

        project = ManifestProject(path=path, west_commands=west_commands)
        projects.insert(MANIFEST_PROJECT_INDEX, project)

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

        # mp = manifest project (dictionary with values parsed from
        # the manifest)
        for mp in manifest['projects']:
            # Validate the project name.
            name = mp['name']

            # Validate the project remote or URL.
            remote_name = mp.get('remote')
            url = mp.get('url')
            repo_path = mp.get('repo-path')
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
                                  path=mp.get('path'),
                                  clone_depth=mp.get('clone-depth'),
                                  revision=mp.get('revision'),
                                  west_commands=mp.get('west-commands'),
                                  remote=remote,
                                  repo_path=repo_path,
                                  url=url)
            except ValueError as ve:
                self._malformed(ve.args[0])

            # Project names must be unique.
            if project.name in project_names:
                self._malformed('project name {} is already used'.
                                format(project.name))
            # Two projects cannot have the same path. We use absolute
            # paths to check for collisions to ensure paths are
            # normalized (e.g. for case-insensitive file systems or
            # in cases like on Windows where / or \ may serve as a
            # path component separator).
            if project.abspath in project_abspaths:
                self._malformed('project {} path {} is already in use'.
                                format(project.name, project.path))

            project_names.add(project.name)
            project_abspaths.add(project.abspath)
            projects.append(project)

        self.defaults = defaults
        self.remotes = remotes
        self._remotes_dict = remotes_dict
        self.projects = tuple(projects)


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

    __slots__ = ('name remote url path abspath posixpath clone_depth '
                 'revision west_commands').split()

    def __init__(self, name, defaults=None, path=None, clone_depth=None,
                 revision=None, west_commands=None, remote=None,
                 repo_path=None, url=None):
        '''Specify a Project by name, Remote, and optional information.

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
        self.url = url or (remote.url_base + '/' + repo_path)
        '''Complete fetch URL for the project, either as given by the url kwarg
        or computed from the remote URL base and the project name.'''
        self.path = os.path.normpath(path or name)
        '''Relative path to the project in the installation.'''
        self.abspath = os.path.realpath(os.path.join(util.west_topdir(),
                                                     self.path))
        '''Absolute path to the project.'''
        self.posixpath = PurePath(self.abspath).as_posix()
        '''Absolute path to the project, POSIX style (with forward slashes).'''
        self.clone_depth = clone_depth
        '''Project's clone depth, or None'''
        self.revision = revision or defaults.revision
        '''Revision to check out for this project, as given in the manifest,
        from manifest defaults, or from the default supplied by west.'''
        self.west_commands = west_commands
        '''Path to project's "west-commands", or None.'''
        self.remote = remote
        '''`Remote` instance corresponding to the project's remote, or None.'''

    def __eq__(self, other):
        return NotImplemented

    def __repr__(self):
        reprs = [repr(x) for x in
                 (self.name, self.remote, self.url, self.path,
                  self.abspath, self.clone_depth, self.revision)]
        return ('Project(name={}, remote={}, url={}, path={}, abspath={}, '
                'clone_depth={}, revision={})').format(*reprs)

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

    def git(self, cmd, extra_args=(), capture_stdout=False, check=True,
            cwd=None):
        '''Helper for running a git command using metadata from a Project
        instance.

        :param cmd: git command as a string (or list of strings); all strings
                    are formatted using self.format() before use.
        :param extra_args: sequence of additional arguments to pass to the git
                           command (useful mostly if cmd is a string).
        :param capture_stdout: True if stdout should be captured into the
                               returned object instead of being printed.
                               The stderr output is never captured,
                               to prevent error messages from being eaten.
        :param check: True if a subprocess.CalledProcessError should be raised
                      if the git command finishes with a non-zero return code.
        :param cwd: directory to run command in (default: self.abspath)

        Returns a CompletedProcess (which is back-ported for Python 3.4).'''
        # TODO: Run once somewhere?
        if shutil.which('git') is None:
            log.wrn('Git is not installed or cannot be found')

        if isinstance(cmd, str):
            cmd_list = shlex.split(cmd)
        else:
            cmd_list = list(cmd)

        extra_args = list(extra_args)

        if cwd is None:
            cwd = self.abspath

        args = ['git'] + [self.format(arg) for arg in cmd_list] + extra_args
        cmd_str = util.quote_sh_list(args)

        log.dbg("running '{}'".format(cmd_str), 'in', cwd,
                level=log.VERBOSE_VERY)
        popen = subprocess.Popen(
            args, stdout=subprocess.PIPE if capture_stdout else None, cwd=cwd)

        stdout, _ = popen.communicate()

        dbg_msg = "'{}' in {} finished with exit status {}".format(
            cmd_str, cwd, popen.returncode)
        if capture_stdout:
            dbg_msg += " and wrote {} to stdout".format(stdout)
        log.dbg(dbg_msg, level=log.VERBOSE_VERY)

        # stderr is None because this method never captures it.
        if check and popen.returncode:
            raise subprocess.CalledProcessError(popen.returncode, cmd_list,
                                                output=stdout, stderr=None)
        else:
            return CompletedProcess(popen.args, popen.returncode, stdout, None)

    def sha(self, rev):
        '''Returns the SHA of the given revision in the current project.

        :param rev: git revision (HEAD, v2.0.0, etc.) as a string
        '''
        cp = self.git('rev-parse ' + rev, capture_stdout=True)
        # Assumption: SHAs are hex values and thus safe to decode in ASCII.
        # It'll be fun when we find out that was wrong and how...
        return cp.stdout.decode('ascii').strip()

    def is_ancestor_of(self, rev1, rev2):
        '''Check if 'rev1' is an ancestor of 'rev2' in this project.

        :param rev1: commit that could be the ancestor
        :param rev2: commit that could be a descendant

        Returns True if rev1 is an ancestor commit of rev2 in the
        given project; rev1 and rev2 can be anything that resolves to
        a commit. (If rev1 and rev2 refer to the same commit, the
        return value is True, i.e. a commit is considered an ancestor
        of itself.) Returns False otherwise.'''
        returncode = self.git('merge-base --is-ancestor {} {}'.
                              format(rev1, rev2),
                              check=False).returncode

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

    def is_up_to_date_with(self, rev):
        '''Check if a project is up to date with revision 'rev'.

        :param rev: base revision to check if project is up to date with.

        Returns True if all commits in 'rev' are also in HEAD. This
        can be used to check if this project needs updates, rebasing,
        etc.; 'rev' can be anything that resolves to a commit.

        This is a special case of is_ancestor_of() provided for convenience.
        '''
        return self.is_ancestor_of(rev, 'HEAD')

    def is_up_to_date(self):
        '''Returns is_up_to_date_with(self.revision).'''
        return self.is_up_to_date_with(self.revision)

class ManifestProject(Project):
    '''Represents the manifest as a project.'''

    def __init__(self, path=None, revision=None, url=None,
                 west_commands=None):
        '''Specify a Special Project by name, and url, and optional information.

        :param path: Relative path to the project in the west
                     installation, if present in the manifest. If None,
                     the project's ``name`` is used.
        :param revision: manifest project revision, or None
        :param url: Complete URL for the manifest project, or None
        :param west_commands: path to a YAML file in the project containing
                              a description of west extension commands provided
                              by the project, if given. This obviously only
                              makes sense for the manifest project, not west.
        '''
        self.name = path or 'manifest'
        '''Project's name (path or default "manifest").'''

        self.url = url
        '''Complete fetch URL for the project.'''

        self.path = path or self.name
        '''Relative path to the project in the installation.'''

        self.abspath = os.path.realpath(os.path.join(util.west_topdir(),
                                                     self.path))
        '''Absolute path to the project.'''

        self.posixpath = PurePath(self.abspath).as_posix()
        '''Absolute path to the project, POSIX style (with forward slashes).'''

        self.revision = revision
        '''Revision to check out for the west project, as given in the manifest,
        from manifest defaults, or from the default supplied by west. Undefined
        for the manifest project.'''

        self.remote = None
        '''None, provided for analogy with Project.'''

        self.clone_depth = None
        '''None, provided for analogy with Project.'''

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
