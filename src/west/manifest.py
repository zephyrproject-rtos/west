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

import collections
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
from west.config import config


# Todo: take from _bootstrap?
# Default west repository URL.
WEST_URL_DEFAULT = 'https://github.com/zephyrproject-rtos/west'
# Default revision to check out of the west repository.
WEST_REV_DEFAULT = 'master'

MANIFEST_SECTIONS = ['manifest', 'west']
'''Sections in the manifest file'''

MANIFEST_PROJECT_INDEX = 0
'''Index in projects where the project with contains project manifest file is
located'''

MANIFEST_REV_BRANCH = 'manifest-rev'
'''The name of the branch that points to the revision specified in the
manifest'''

QUAL_MANIFEST_REV_BRANCH = 'refs/heads/' + MANIFEST_REV_BRANCH
'''A qualified reference to MANIFEST_REV_BRANCH, i.e.
refs/heads/{}'''.format(MANIFEST_REV_BRANCH)


def manifest_path():
    '''Return the path to the manifest file.

    Raises WestNotFound if called from outside of a west working directory.'''
    try:
        return os.path.join(util.west_topdir(),
                            config.get('manifest', 'path'),
                            'west.yml')
    except (configparser.NoOptionError, configparser.NoSectionError) as e:
        raise MalformedConfig('missing key: \'{}\' in west config file'
                              .format(e.args[0])) from e


class Manifest:
    '''Represents the contents of a West manifest file.

    The most convenient way to construct an instance is using the
    from_file and from_data helper methods.'''

    @staticmethod
    def from_file(source_file=None, sections=MANIFEST_SECTIONS):
        '''Create and return a new Manifest object given a source YAML file.

        :param source_file: Path to a YAML file containing the manifest.
        :param sections: Only parse specified sections from YAML file,
                         default: all sections are parsed.

        If source_file is None, the value returned by manifest_path()
        is used.

        Raises MalformedManifest in case of validation errors.
        Raises MalformedConfig in case of missing configuration settings.'''
        if source_file is None:
            source_file = manifest_path()
        return Manifest(source_file=source_file, sections=sections)

    @staticmethod
    def from_data(source_data, sections=MANIFEST_SECTIONS):
        '''Create and return a new Manifest object given parsed YAML data.

        :param source_data: Parsed YAML data as a Python object.
        :param sections: Only parse specified sections from YAML data,
                         default: all sections are parsed.

        Raises MalformedManifest in case of validation errors.
        Raises MalformedConfig in case of missing configuration settings.'''
        return Manifest(source_data=source_data, sections=sections)

    def __init__(self, source_file=None, source_data=None,
                 sections=MANIFEST_SECTIONS):
        '''Create a new Manifest object.

        :param source_file: Path to a YAML file containing the manifest.
        :param source_data: Parsed YAML data as a Python object.
        :param sections: Only parse specified sections from YAML file,
                         default: all sections are parsed.

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

        for section in sections:
            if section not in MANIFEST_SECTIONS:
                raise ValueError('invalid section {}'.format(section))

        self.path = path
        '''Path to the file containing the manifest, or None if created
        from data rather than the file system.'''

        if not self._data:
            self._malformed('manifest contains no data')

        if 'manifest' not in self._data:
            self._malformed('manifest contains no manifest element')

        for key in self._data:
            if key in sections:
                try:
                    pykwalify.core.Core(
                        source_data=self._data[key],
                        schema_files=[_SCHEMA_PATH[key]]
                    ).validate()
                except pykwalify.errors.SchemaError as e:
                    self._malformed(e, key)

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
        to consult the defaults field to supply missing values.

        Note: The index MANIFEST_PROJECT_INDEX in sequence will hold the
        project which contains the project manifest file.'''

        self.west_project = None
        '''west.manifest.SpecialProject object representing the west meta
        project.'''

        # Set up the public attributes documented above, as well as
        # any internal attributes needed to implement the public API.
        self._load(self._data, sections)

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
        r['west'] = self.west_project.as_dict()
        r['west']['revision'] = self.west_project.sha(QUAL_MANIFEST_REV_BRANCH)
        r['manifest'] = collections.OrderedDict()
        r['manifest']['defaults'] = self.defaults.as_dict()
        r['manifest']['remotes'] = [r.as_dict() for r in self.remotes]
        r['manifest']['projects'] = frozen_projects
        r['manifest']['self'] = self.projects[MANIFEST_PROJECT_INDEX].as_dict()

        return r

    def _malformed(self, complaint, section='manifest'):
        context = (' file {} '.format(self.path) if self.path
                   else ' data:\n{}\n'.format(self._data))
        raise MalformedManifest('Malformed manifest{}(schema: {}):\n{}'
                                .format(context, _SCHEMA_PATH[section],
                                        complaint))

    def _load(self, data, sections):
        # Initialize this instance's fields from values given in the
        # manifest data, which must be validated according to the schema.
        if 'west' in sections:
            west = data.get('west', {})

            url = west.get('url') or WEST_URL_DEFAULT
            revision = west.get('revision') or WEST_REV_DEFAULT

            self.west_project = SpecialProject('west',
                                               url=url,
                                               revision=revision,
                                               path=os.path.join('.west',
                                                                 'west'))

        # Next is the manifest section
        if 'manifest' not in sections:
            return

        projects = []
        project_abspaths = set()

        manifest = data.get('manifest')

        path = config.get('manifest', 'path', fallback=None)

        self_tag = manifest.get('self')
        if path is None:
            path = self_tag.get('path') if self_tag else ''
        west_commands = self_tag.get('west-commands') if self_tag else None

        project = SpecialProject(path, path=path, west_commands=west_commands)
        projects.insert(MANIFEST_PROJECT_INDEX, project)

        # Map from each remote's name onto that remote's data in the manifest.
        remotes = tuple(Remote(r['name'], r['url-base']) for r in
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
            if name == 'west':
                self._malformed('the name "west" is reserved and cannot '
                                'be used to name a manifest project')

            # Validate the project remote.
            remote_name = mp.get('remote', default_remote_name)
            if remote_name is None:
                self._malformed('project {} does not specify a remote'.
                                format(name))
            if remote_name not in remotes_dict:
                self._malformed('project {} remote {} is not defined'.
                                format(name, remote_name))

            # Create the project instance for final checking.
            project = Project(name,
                              remotes_dict[remote_name],
                              defaults,
                              path=mp.get('path'),
                              clone_depth=mp.get('clone-depth'),
                              revision=mp.get('revision'),
                              west_commands=mp.get('west-commands'))

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
        if remote is not None:
            _wrn_if_not_remote(remote)
        if revision is None:
            revision = 'master'

        self.remote = remote
        self.revision = revision

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
        self.url_base = url_base

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

    def __init__(self, name, remote, defaults, path=None, clone_depth=None,
                 revision=None, west_commands=None):
        '''Specify a Project by name, Remote, and optional information.

        :param name: Project's user-defined name in the manifest.
        :param remote: Remote instance corresponding to this Project as
                       specified in the manifest. This is used to build
                       the project's URL, and is also stored as an attribute.
        :param defaults: If the revision parameter is not given, the project's
                         revision is set to defaults.revision.
        :param path: Relative path to the project in the west
                     installation, if present in the manifest. If not given,
                     the project's ``name`` is used.
        :param clone_depth: Nonnegative integer clone depth if present in
                            the manifest.
        :param revision: Project revision as given in the manifest, if present.
                         If not given, defaults.revision is used instead.
        :param west_commands: path to a YAML file in the project containing
                              a description of external west commands provided
                              by the project, if given.
        '''
        _wrn_if_not_remote(remote)

        self.name = name
        self.remote = remote
        self.url = remote.url_base + '/' + name
        self.path = os.path.normpath(path or name)
        self.abspath = os.path.realpath(os.path.join(util.west_topdir(),
                                                     self.path))
        self.posixpath = PurePath(self.abspath).as_posix()
        self.clone_depth = clone_depth
        self.revision = revision or defaults.revision
        self.west_commands = west_commands

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

        The format method is called with *args and the following kwargs:

        - this object's __slots__ / values (name, url, etc.)
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

class SpecialProject(Project):
    '''Represents a special project, e.g. the west or manifest project.

    Projects are neither comparable nor hashable.'''

    def __init__(self, name, path=None, revision='(not set)', url='(not set)',
                 west_commands=None):
        '''Specify a Special Project by name, and url, and optional information.

        :param name: Special Project's user-defined name in the manifest
        :param path: Relative path to the project in the west
                     installation, if present in the manifest. If None,
                     the project's ``name`` is used.
        :param revision: Project revision as given in the manifest, if present.
        :param url: Complete URL for special project.
        :param west_commands: path to a YAML file in the project containing
                              a description of external west commands provided
                              by the project, if given. This obviously only
                              makes sense for the manifest project, not west.
        '''
        if name == 'west' and west_commands:
            raise ValueError('setting west_commands on west is forbidden')

        self.name = name
        self.url = url
        self.path = path or name
        self.abspath = os.path.realpath(os.path.join(util.west_topdir(),
                                                     self.path))
        self.posixpath = PurePath(self.abspath).as_posix()
        self.revision = revision
        self.remote = None
        self.clone_depth = None
        self.west_commands = west_commands

    def as_dict(self):
        '''Return a representation of this object as a dict, as it would be
        parsed from an equivalent YAML manifest.'''
        if self.name == 'west':
            return collections.OrderedDict((('url', self.url),
                                            ('revision', self.revision)))
        else:
            # Manifest project is assumed.
            ret = collections.OrderedDict({'path': self.path})
            if self.west_commands:
                ret['west-commands'] = self.west_commands
            return ret

def _wrn_if_not_remote(remote):
    if not isinstance(remote, Remote):
        log.wrn('Remote', remote, 'is not a Remote instance')


_SCHEMA_PATH = {'manifest': os.path.join(os.path.dirname(__file__),
                                         "manifest-schema.yml"),
                'west': os.path.join(os.path.dirname(__file__),
                                     "_bootstrap",
                                     "west-schema.yml")}
