# Copyright 2018 Foundries.io Ltd
# Copyright (c) 2020, Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

# Tests for the west.manifest API.
#
# Generally try to avoid shelling out to git in this test file, but if
# it's particularly inconvenient to test something without a real git
# repository, go ahead and make one in a temporary directory.

from copy import deepcopy
from glob import glob
import logging
import os
from pathlib import PurePath, Path
import platform
import subprocess
from unittest.mock import patch
import textwrap

import pytest
import yaml

from west.manifest import Manifest, Project, ManifestProject, \
    MalformedManifest, ManifestVersionError, ManifestImportFailed, \
    manifest_path, ImportFlag, validate, MANIFEST_PROJECT_INDEX, \
    _ManifestImportDepth, is_group, SCHEMA_VERSION
from west.configuration import Configuration, ConfigFile, MalformedConfig

# White box checks for the schema version.
from west.manifest import _VALID_SCHEMA_VERS

from conftest import create_workspace, create_repo, checkout_branch, \
    create_branch, add_commit, add_tag, rev_parse, GIT, check_proj_consistency

assert 'TOXTEMPDIR' in os.environ, "you must run these tests using tox"

FPI = ImportFlag.FORCE_PROJECTS  # to force project imports to use the callback

if platform.system() == 'Windows':
    TOPDIR = 'C:\\topdir'
    TOPDIR_POSIX = 'C:/topdir'
else:
    TOPDIR = '/topdir'
    TOPDIR_POSIX = TOPDIR

THIS_DIRECTORY = os.path.dirname(__file__)

@pytest.fixture
def tmp_workspace(tmpdir):
    # This fixture creates a skeletal west workspace in a temporary
    # directory on the file system, and changes directory there.
    #
    # If you use this fixture, you can create
    # './mp/west.yml', then run tests using its contents using
    # Manifest.from_file(), etc. Or just use manifest_repo().

    # Create the manifest repository directory and skeleton config.
    topdir = tmpdir / 'topdir'
    create_workspace(topdir)

    # Switch to the top-level west workspace directory,
    # and give it to the test case.
    topdir.chdir()
    return topdir

@pytest.fixture
def manifest_repo(tmp_workspace):
    # This creates a temporary manifest repository, changes directory
    # to it, and returns a pathlike for it.

    manifest_repo = tmp_workspace / 'mp'
    create_repo(manifest_repo)
    manifest_repo.topdir = Path(tmp_workspace)
    return manifest_repo

def nodrive(path):
    return os.path.splitdrive(path)[1]

def M(content, **kwargs):
    # A convenience to save typing
    return Manifest.from_data('manifest:\n' + content, **kwargs)

def MF(**kwargs):
    # A convenience to save typing
    return Manifest.from_file(**kwargs)

def MT(**kwargs):
    # A convenience to save typing
    return Manifest.from_topdir(**kwargs)

#########################################
# The very basics
#
# We need to be able to instantiate Projects and parse manifest data
# from strings or dicts, as well as from the file system.

def test_project_init():
    # Basic tests of the Project constructor and public attributes.

    p = Project('p', 'some-url', revision='v1.2')
    assert p.name == 'p'
    assert p.url == 'some-url'
    assert p.revision == 'v1.2'
    assert p.path == 'p'
    assert p.abspath is None
    assert p.posixpath is None
    assert p.clone_depth is None
    assert p.west_commands == []
    assert p.topdir is None

    p = Project('p', 'some-url', clone_depth=4, west_commands='foo',
                topdir=TOPDIR)
    assert p.clone_depth == 4
    assert p.west_commands == ['foo']
    assert p.topdir == TOPDIR
    assert p.abspath == os.path.join(TOPDIR, 'p')
    assert p.posixpath == TOPDIR_POSIX + '/p'

def test_manifest_from_data_without_topdir():
    # We can load manifest data as a dict or a string.
    # If no *topdir* argument is given, as is done here,
    # absolute path attributes should be None.

    manifest = Manifest.from_data('''\
    manifest:
      projects:
        - name: foo
          url: https://foo.com
    ''')
    assert manifest.projects[-1].name == 'foo'
    assert manifest.projects[-1].abspath is None

    manifest = Manifest.from_data({'manifest':
                                   {'projects':
                                    [{'name': 'foo',
                                      'url': 'https:foo.com'}]}})
    assert manifest.projects[-1].name == 'foo'
    assert manifest.projects[-1].abspath is None

def test_manifest_from_file_with_fall_back(manifest_repo):
    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write('''
        manifest:
          projects: []
        ''')
    repo_abspath = Path(str(manifest_repo))
    os.chdir(repo_abspath.parent.parent)  # this is the tmp_workspace dir
    try:
        os.environ['ZEPHYR_BASE'] = os.fspath(manifest_repo)
        manifest = MF()
        assert Path(manifest.repo_abspath) == repo_abspath
    finally:
        del os.environ['ZEPHYR_BASE']

def test_validate():
    # Get some coverage for west.manifest.validate.

    # White box
    with pytest.raises(TypeError):
        validate(None)

    with pytest.raises(MalformedManifest):
        validate('invalid')

    with pytest.raises(MalformedManifest):
        validate('not-a-manifest')

    with pytest.raises(MalformedManifest):
        validate({'not-manifest': 'foo'})

    manifest_data = {'manifest': {'projects': [{'name': 'p', 'url': 'u'}]}}
    assert validate(manifest_data) == manifest_data

    with pytest.raises(MalformedManifest):
        # White box:
        #
        # The 're' string in there is crafted specifically to force a
        # yaml.scanner.ScannerError, which needs to be converted to
        # MalformedManifest.
        validate('''\
        manifest:
          projects:
          - name: p
            url: p-url
        re
            import: not-a-file
        ''')

    assert validate('''\
    manifest:
      projects:
      - name: p
        url: u
    ''') == {
        'manifest': {
            'projects': [{'name': 'p', 'url': 'u'}]
        }
    }

def test_constructor_arg_validation():
    with pytest.raises(ValueError) as e:
        Manifest(source_data='x', topdir='y')
    assert 'both topdir and source_data were given' in str(e.value)
    with pytest.raises(ValueError) as e:
        Manifest(source_data='x', config='y')
    assert 'both source_data and config were given' in str(e.value)

#########################################
# Project parsing tests
#
# Tests for validating and parsing project data, including:
#
# - names
# - URLs
# - revisions
# - paths
# - clone depths
# - west commands
# - repr()

def test_projects_must_have_name():
    # A project must have a name. Names must be unique.

    with pytest.raises(MalformedManifest):
        M('''\
        projects:
        - url: foo
        ''')

    with pytest.raises(MalformedManifest):
        M('''\
        projects:
        - name: foo
          url: u1
        - name: foo
          url: u2
        ''')

    m = M('''\
    projects:
    - name: foo
      url: u1
    - name: bar
      url: u2
    ''')
    assert m.projects[1].name == 'foo'
    assert m.projects[2].name == 'bar'

def test_no_project_named_manifest():
    # The name 'manifest' is reserved.

    with pytest.raises(MalformedManifest):
        M('''\
        projects:
        - name: manifest
          url: u
        ''')

def test_project_named_west():
    # A project named west is allowed now, even though it was once an error.

    m = M('''\
    projects:
      - name: west
        url: https://foo.com
    ''')
    assert m.projects[1].name == 'west'

def test_project_urls():
    # Projects must be initialized with a remote or a URL, but not both.
    # The resulting URLs must behave as documented.

    # The following cases are valid:
    # - explicit url
    # - explicit remote, no repo-path
    # - explicit remote + repo-path
    # - default remote, no repo-path
    # - default remote + repo-path
    ps = M('''\
    defaults:
      remote: r2
    remotes:
    - name: r1
      url-base: https://foo.com
    - name: r2
      url-base: https://baz.com
    projects:
    - name: project1
      url: https://bar.com/project1
    - name: project2
      remote: r1
    - name: project3
      remote: r1
      repo-path: project3-path
    - name: project4
    - name: project5
      repo-path: subdir/project-five
    ''').projects
    assert ps[1].url == 'https://bar.com/project1'
    assert ps[2].url == 'https://foo.com/project2'
    assert ps[3].url == 'https://foo.com/project3-path'
    assert ps[4].url == 'https://baz.com/project4'
    assert ps[5].url == 'https://baz.com/subdir/project-five'

    # A remotes section isn't required in a manifest if all projects
    # are specified by URL.
    ps = M('''\
    projects:
    - name: testproject
      url: https://example.com/my-project
    ''').projects
    assert ps[1].url == 'https://example.com/my-project'

    # Projects can't have both url and remote attributes.
    with pytest.raises(MalformedManifest):
        M('''\
        remotes:
        - name: r1
          url-base: https://example.com
        projects:
        - name: project1
          remote: r1
          url: https://example.com/project2
        ''')

    # Projects can't combine url and repo-path.
    with pytest.raises(MalformedManifest):
        M('''\
        projects:
        - name: project1
          repo-path: x
          url: https://example.com/project2
        ''')

    # A remote or URL must be given if no default remote is set.
    with pytest.raises(MalformedManifest):
        M('''\
        remotes:
        - name: r1
          url-base: https://example.com
        projects:
        - name: project1
        ''')

    # All remotes must be defined, even if there is a default.
    with pytest.raises(MalformedManifest):
        M('''\
        defaults:
          remote: r1
        remotes:
        - name: r1
          url-base: https://example.com
        projects:
        - name: project1
          remote: deadbeef
        ''')

def test_project_revisions():
    # All projects have revisions.

    # The default revision, if set, should take effect
    # when not explicitly specified in a project.
    m = M('''\
    defaults:
      revision: defaultrev
    projects:
    - name: p1
      url: u1
    - name: p2
      url: u2
      revision: rev
    ''')
    expected = [Project('p1', 'u1', revision='defaultrev'),
                Project('p2', 'u2', revision='rev')]
    for p, e in zip(m.projects[1:], expected):
        check_proj_consistency(p, e)

    # The default revision, if not given in a defaults section, is
    # master.
    m = M('''\
    projects:
    - name: p1
      url: u1
    ''')
    assert m.projects[1].revision == 'master'

def test_project_paths_explicit_implicit():
    # Test project path parsing.

    # Project paths may be explicitly given, or implicit.
    ps = M('''\
    projects:
    - name: p
      url: u
      path: foo
    - name: q
      url: u
    ''').projects
    assert ps[1].path == 'foo'
    assert ps[2].path == 'q'

def test_project_paths_absolute():
    # Absolute path attributes should be None when loading from data.

    ps = M('''\
    remotes:
    - name: testremote
      url-base: https://example.com
    projects:
    - name: testproject
      remote: testremote
      path: sub/directory
    ''').projects
    assert ps[1].path == 'sub/directory'
    assert ps[1].abspath is None
    assert ps[1].posixpath is None

def test_project_paths_unique():
    # No two projects may have the same path.

    with pytest.raises(MalformedManifest):
        M('''\
        projects:
        - name: a
          path: p
        - name: p
        ''')
    with pytest.raises(MalformedManifest):
        M('''\
        projects:
        - name: a
          path: p
        - name: b
          path: p
        ''')

def test_project_paths_with_repo_path():
    # The same fetch URL may be checked out under two different
    # names, as long as they end up in different places.
    content = '''\
    defaults:
      remote: remote1
    remotes:
    - name: remote1
      url-base: https://url1.com
    projects:
    - name: testproject_v1
      revision: v1.0
      repo-path: testproject
    - name: testproject_v2
      revision: v2.0
      repo-path: testproject
    '''

    # Try this first without providing topdir.
    m = M(content)
    expected1 = Project('testproject_v1', 'https://url1.com/testproject',
                        revision='v1.0')
    expected2 = Project('testproject_v2', 'https://url1.com/testproject',
                        revision='v2.0')
    check_proj_consistency(m.projects[1], expected1)
    check_proj_consistency(m.projects[2], expected2)

def test_project_clone_depth():
    ps = M('''\
    projects:
    - name: foo
      url: u1
    - name: bar
      url: u2
      clone-depth: 4
    ''').projects
    assert ps[1].clone_depth is None
    assert ps[2].clone_depth == 4

def test_project_west_commands():
    # Projects may also specify subdirectories with west commands.

    m = M('''\
    projects:
    - name: zephyr
      url: https://foo.com
      west-commands: some-path/west-commands.yml
    ''')
    assert m.projects[1].west_commands == ['some-path/west-commands.yml']

def test_project_git_methods(tmpdir):
    # Test the internal consistency of the various methods that call
    # out to git.

    # Just manually create a Project instance. We don't need a full
    # Manifest.
    path = tmpdir / 'project'
    p = Project('project', 'ignore-this-url', topdir=tmpdir)

    # Helper for getting the contents of a.txt at a revision.
    def a_content_at(rev):
        return p.git(f'show {rev}:a.txt', capture_stderr=True,
                     capture_stdout=True).stdout.decode('ascii')

    # The project isn't cloned yet.
    assert not p.is_cloned()

    # Create it, then verify the API knows it's cloned.
    # Cache the current SHA.
    create_repo(path)
    assert p.is_cloned()
    start_sha = p.sha('HEAD')

    # If a.txt doesn't exist at a revision, we can't read it. If it
    # does, we can.
    with pytest.raises(subprocess.CalledProcessError):
        a_content_at('HEAD')
    add_commit(path, 'add a.txt', files={'a.txt': 'a'})
    a_sha = p.sha('HEAD')
    with pytest.raises(subprocess.CalledProcessError):
        a_content_at(start_sha)
    assert a_content_at(a_sha) == 'a'

    # Checks for read_at() and listdir_at().
    add_commit(path, 'add b.txt', files={'b.txt': 'b'})
    b_sha = p.sha('HEAD')
    assert p.read_at('a.txt', rev=a_sha) == b'a'
    with pytest.raises(subprocess.CalledProcessError):
        p.read_at('a.txt', rev=start_sha)
    assert p.listdir_at('', rev=start_sha) == []
    assert p.listdir_at('', rev=a_sha) == ['a.txt']
    assert sorted(p.listdir_at('', rev=b_sha)) == ['a.txt', 'b.txt']

    # p.git() should be able to take a cwd kwarg which is a PathLike
    # or a str.
    p.git('log -1', cwd=path)
    p.git('log -1', cwd=str(path))

    # Basic checks for functions which operate on commits.
    assert a_content_at(a_sha) == 'a'
    assert p.is_ancestor_of(start_sha, a_sha)
    assert not p.is_ancestor_of(a_sha, start_sha)
    assert p.is_up_to_date_with(start_sha)
    assert p.is_up_to_date_with(a_sha)
    assert p.is_up_to_date_with(b_sha)
    p.revision = b_sha
    assert p.is_up_to_date()
    p.git(f'reset --hard {a_sha}')
    assert not p.is_up_to_date()

def test_project_repr():
    m = M('''\
    projects:
    - name: zephyr
      url: https://foo.com
      revision: r
      west-commands: some-path/west-commands.yml
    ''')
    assert repr(m.projects[1]) == \
        'Project("zephyr", "https://foo.com", revision="r", path=\'zephyr\', clone_depth=None, west_commands=[\'some-path/west-commands.yml\'], topdir=None, groups=[], userdata=None)'  # noqa: E501

def test_project_sha(tmpdir):
    tmpdir = Path(os.fspath(tmpdir))
    create_repo(tmpdir)
    add_tag(tmpdir, 'test-tag')
    expected_sha = rev_parse(tmpdir, 'HEAD^{commit}')
    project = Project('name',
                      'url-do-not-fetch',
                      revision='test-tag',
                      path=tmpdir.name,
                      topdir=tmpdir.parent)
    assert project.sha(project.revision) == expected_sha

def test_project_description(tmpdir):
    m = M('''\
    defaults:
      remote: r
    remotes:
      - name: r
        url-base: base
    projects:
    - name: foo
    - name: bar
      description: bar-description
    - name: baz
      description: |
        This is a long multi-line description
        for project baz.
    ''')
    foo, bar, baz = m.get_projects(['foo', 'bar', 'baz'])

    assert foo.description is None
    assert bar.description == 'bar-description'
    desc = 'This is a long multi-line description\n' \
           'for project baz.\n'

    assert baz.description == desc
    assert 'description' not in foo.as_dict()
    assert 'description' in bar.as_dict()
    assert 'bar-description' == bar.as_dict()['description']


def test_project_userdata(tmpdir):
    m = M('''\
    defaults:
      remote: r
    remotes:
      - name: r
        url-base: base
    projects:
    - name: foo
    - name: bar
      userdata: a-string
    - name: baz
      userdata:
        key: value
    ''')
    foo, bar, baz = m.get_projects(['foo', 'bar', 'baz'])

    assert foo.userdata is None
    assert bar.userdata == 'a-string'
    assert baz.userdata == {'key': 'value'}

    assert 'userdata' not in foo.as_dict()
    assert 'a-string' == bar.as_dict()['userdata']


def test_self_userdata(tmpdir):
    m = M('''
    defaults:
      remote: r
    remotes:
      - name: r
        url-base: base
    projects:
    - name: bar
    self:
      path: foo
      userdata:
        key: value
    ''')
    foo, bar = m.get_projects(['manifest', 'bar'])

    assert m.userdata == {'key': 'value'}
    assert foo.userdata == {'key': 'value'}
    assert bar.userdata is None
    assert 'userdata' in foo.as_dict()
    assert 'userdata' not in bar.as_dict()


def test_self_missing_userdata(tmpdir):
    m = M('''
    defaults:
      remote: r
    remotes:
      - name: r
        url-base: base
    projects:
    - name: bar
    self:
      path: foo
    ''')
    foo, bar = m.get_projects(['manifest', 'bar'])

    assert m.userdata is None
    assert foo.userdata is None
    assert bar.userdata is None
    assert 'userdata' not in foo.as_dict()
    assert 'userdata' not in bar.as_dict()


def test_no_projects():
    # An empty projects list is allowed.

    m = Manifest.from_data('manifest: {}')
    assert len(m.projects) == 1  # just ManifestProject

    m = M('''
    self:
      path: foo
    ''')
    assert len(m.projects) == 1  # just ManifestProject

#########################################
# Tests for the manifest repository

def test_manifest_project():
    # Basic test that the manifest repository, when represented as a project,
    # has attributes which make sense when loaded from data.

    # Case 1: everything at defaults
    m = M('''\
    projects:
    - name: name
      url: url
    ''')
    mp = m.projects[0]
    assert mp.name == 'manifest'
    assert mp.path is None
    assert mp.topdir is None
    assert mp.abspath is None
    assert mp.posixpath is None
    assert mp.url == ''
    assert mp.revision == 'HEAD'
    assert mp.clone_depth is None

    # Case 2: path and west-commands are specified
    m = M('''\
    projects:
    - name: name
      url: url
    self:
      path: my-path
      west-commands: cmds.yml
    ''')
    mp = m.projects[0]
    assert mp.name == 'manifest'
    assert mp.path == 'my-path'
    assert m.yaml_path == 'my-path'
    assert mp.west_commands == ['cmds.yml']
    assert mp.topdir is None
    assert mp.abspath is None
    assert mp.posixpath is None
    assert mp.url == ''
    assert mp.revision == 'HEAD'
    assert mp.clone_depth is None

def test_self_tag():
    # Manifests may contain a self section describing the manifest
    # repository. It should work with multiple projects and remotes as
    # expected.

    m = M('''\
    remotes:
    - name: testremote1
      url-base: https://example1.com
    - name: testremote2
      url-base: https://example2.com

    projects:
    - name: testproject1
      remote: testremote1
      revision: rev1
    - name: testproject2
      remote: testremote2

    self:
      path: the-manifest-path
      west-commands: scripts/west_commands
    ''')

    expected = [ManifestProject(path='the-manifest-path',
                                west_commands='scripts/west_commands'),
                Project('testproject1', 'https://example1.com/testproject1',
                        revision='rev1'),
                Project('testproject2', 'https://example2.com/testproject2')]

    # Check the projects are as expected.
    for p, e in zip(m.projects, expected):
        check_proj_consistency(p, e)

    # With a "self: path:" value, that will be available in the
    # yaml_path attribute, but all other absolute and relative
    # attributes are None since we aren't reading from a workspace.
    assert m.abspath is None
    assert m.relative_path is None
    assert m.yaml_path == 'the-manifest-path'
    assert m.repo_abspath is None

    # If "self: path:" is missing, we won't have a yaml_path attribute.
    m = M('''\
    projects:
    - name: p
      url: u
    ''')
    assert m.yaml_path is None

    # Empty paths are an error.
    with pytest.raises(MalformedManifest) as e:
        M('''\
        projects: []
        self:
          path:''')
    assert 'must be nonempty if present' in str(e.value)

#########################################
# File system tests
#
# Parsing manifests from data is the base case that everything else
# reduces to, but parsing may also be done from files on the file
# system, or "as if" it were done from files on the file system.

def test_from_topdir(tmp_workspace):
    # If you load from topdir along with some source data, you will
    # get absolute paths.
    #
    # This is true of both projects and the manifest itself.

    topdir = Path(str(tmp_workspace))
    repo_abspath = topdir / 'mp'
    relpath = Path('mp') / 'west.yml'
    abspath = topdir / relpath
    mf = topdir / relpath

    # Case 1: manifest has no "self: path:".
    with open(mf, 'w', encoding='utf-8') as f:
        f.write('''
        manifest:
          projects:
          - name: my-cool-project
            url: from-manifest-dir
        ''')
    m = MT(topdir=topdir)
    # Path-related Manifest attribute tests.
    assert Path(m.abspath) == mf
    assert m.posixpath == mf.as_posix()
    assert Path(m.relative_path) == relpath
    assert m.yaml_path is None
    assert Path(m.repo_abspath) == repo_abspath
    assert m.repo_posixpath == repo_abspath.as_posix()
    assert Path(m.topdir) == topdir
    # Legacy ManifestProject tests.
    mproj = m.projects[MANIFEST_PROJECT_INDEX]
    assert Path(mproj.topdir) == topdir
    assert Path(mproj.path) == Path('mp')
    # Project tests.
    p1 = m.projects[1]
    assert Path(p1.topdir) == Path(topdir)
    assert Path(p1.abspath) == Path(topdir / 'my-cool-project')

    # Case 2: manifest has a "self: path:", which disagrees with the
    # actual file system path.
    with open(mf, 'w', encoding='utf-8') as f:
        f.write('''
        manifest:
          projects:
          - name: my-cool-project
            url: from-manifest-dir
          self:
            path: something/else
        ''')
    m = MT(topdir=topdir)
    # Path-related Manifest attribute tests.
    assert Path(m.abspath) == abspath
    assert m.posixpath == abspath.as_posix()
    assert Path(m.relative_path) == relpath
    assert m.yaml_path == 'something/else'
    assert Path(m.repo_abspath) == repo_abspath
    assert m.repo_posixpath == repo_abspath.as_posix()
    assert Path(m.topdir) == topdir
    # Legacy ManifestProject tests.
    mproj = m.projects[MANIFEST_PROJECT_INDEX]
    assert Path(mproj.topdir).is_absolute()
    assert Path(mproj.topdir) == topdir
    assert Path(mproj.path) == Path('mp')
    assert Path(mproj.abspath).is_absolute()
    assert Path(mproj.abspath) == repo_abspath
    # Project tests.
    p1 = m.projects[1]
    assert Path(p1.topdir) == Path(topdir)
    assert Path(p1.abspath) == topdir / 'my-cool-project'

    # Case 3: project has a path. This always takes effect.
    with open(mf, 'w', encoding='utf-8') as f:
        f.write('''
        manifest:
          projects:
          - name: my-cool-project
            url: from-manifest-dir
            path: project-path
          self:
            path: something/else
        ''')
    m = MT(topdir=topdir)
    p1 = m.projects[1]
    assert p1.path == 'project-path'
    assert Path(p1.abspath) == topdir / 'project-path'
    assert p1.posixpath == (topdir / 'project-path').as_posix()

def test_manifest_path_not_found(tmp_workspace):
    # Make sure manifest_path() raises FileNotFoundError if the
    # manifest file specified in .west/config doesn't exist.
    # Here, we rely on tmp_workspace not actually creating the file.

    with pytest.raises(FileNotFoundError) as e:
        manifest_path()
    assert e.value.filename == tmp_workspace / 'mp' / 'west.yml'

def test_manifest_path_conflicts(tmp_workspace):
    # Project path conflicts with the manifest path are errors. This
    # is true when we have an explicit file system path, but it is not
    # true when loading from data, where absolute paths are not known
    # and the actual location of the manifest may be overridden from
    # "self: path:", e.g. with "west init -l".

    with open(tmp_workspace / 'mp' / 'west.yml', 'w', encoding='utf-8') as f:
        f.write('''
        manifest:
           projects:
           - name: p
             path: mp
             url: u
        ''')

    with pytest.raises(MalformedManifest) as e:
        MT(topdir=tmp_workspace)
    assert 'p path "mp" is taken by the manifest repository' in str(e.value)

    m = M('''\
        projects:
        - name: n
          url: u
          path: p
        self:
          path: p
        ''')
    assert m.yaml_path == 'p'
    assert m.abspath is None
    assert m.projects[1].path == 'p'
    assert m.projects[1].abspath is None

def test_manifest_repo_discovery(manifest_repo):
    # The API should be able to find a manifest file based on the file
    # system and west configuration. The resulting topdir and abspath
    # attributes should work as specified.

    topdir = manifest_repo.topdir

    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write('''\
        manifest:
          projects:
          - name: project-from-manifest-dir
            url: from-manifest-dir
        ''')

    # manifest_path() should discover west_yml.
    assert manifest_path() == manifest_repo / 'west.yml'

    # Manifest.from_file() should as well.
    # The project hierarchy should be rooted in the topdir.
    manifest = Manifest.from_file()
    assert manifest.topdir is not None
    assert Path(manifest.topdir) == topdir
    assert len(manifest.projects) == 2
    p = manifest.projects[1]
    assert p.name == 'project-from-manifest-dir'
    assert p.url == 'from-manifest-dir'
    assert p.topdir is not None
    assert PurePath(p.topdir) == topdir

    # Manifest.from_topdir() should work similarly.
    manifest = MT()
    assert Path(manifest.topdir) == topdir

def test_parse_multiple_manifest_files(manifest_repo):
    # The API should be able to parse multiple manifest files inside a
    # single topdir. The project hierarchies should always be rooted
    # in that same topdir. The results of parsing the two separate
    # files are independent of one another.

    topdir = Path(manifest_repo.topdir)
    manifest_repo = Path(manifest_repo)
    west_yml = manifest_repo / 'west.yml'

    with open(west_yml, 'w') as f:
        f.write('''\
        manifest:
          projects:
          - name: project-1
            url: url-1
          - name: project-2
            url: url-2
        ''')

    another_repo = topdir / 'another-repo'
    create_repo(another_repo)
    another_yml = another_repo / 'another.yml'
    with open(another_yml, 'w') as f:
        f.write('''\
        manifest:
          projects:
          - name: another-1
            url: another-url-1
          - name: another-2
            url: another-url-2
            path:  another/path
        ''')

    another_yml_with_path = another_repo / 'another-with-path.yml'
    with open(another_yml_with_path, 'w') as f:
        f.write('''\
        manifest:
          projects:
          - name: foo
            url: bar
          self:
            path: yaml-path
        ''')

    # manifest_path() should discover west_yml.
    assert Path(manifest_path()) == west_yml

    # Manifest.from_file() should discover west.yml, and
    # the project hierarchy should be rooted at topdir.
    manifest = Manifest.from_file()
    assert Path(manifest.topdir) == topdir
    assert len(manifest.projects) == 3
    assert manifest.projects[1].name == 'project-1'
    assert manifest.projects[2].name == 'project-2'

    # Manifest.from_file() should be also usable with another_yml.
    # The project hierarchy in its return value should still be rooted
    # in the topdir, but the resulting manifest will be initialized
    # as if from "another_repo".
    manifest = Manifest.from_file(source_file=another_yml)
    assert len(manifest.projects) == 3
    assert manifest.topdir is not None
    assert Path(manifest.topdir) == topdir
    assert Path(manifest.abspath) == another_yml
    assert Path(manifest.repo_abspath) == another_repo
    mproj = manifest.projects[0]
    assert Path(mproj.path) == Path('another-repo')
    assert Path(mproj.abspath) == another_repo
    assert mproj.posixpath == another_repo.as_posix()
    p1 = manifest.projects[1]
    assert p1.name == 'another-1'
    assert p1.url == 'another-url-1'
    assert Path(p1.topdir) == topdir
    assert PurePath(p1.abspath) == topdir / 'another-1'
    p2 = manifest.projects[2]
    assert p2.name == 'another-2'
    assert p2.url == 'another-url-2'
    assert Path(p2.topdir) == topdir
    assert Path(p2.abspath) == topdir / 'another' / 'path'

    # If the manifest yaml file does specify its path, the yaml_path
    # attribute should reflect that, but we should still reflect what
    # we actually loaded.
    manifest = Manifest.from_file(source_file=another_yml_with_path)
    assert manifest.yaml_path == 'yaml-path'
    assert Path(manifest.abspath) == another_yml_with_path
    mproj = manifest.projects[0]
    assert Path(mproj.abspath) == another_repo

def test_bad_topdir_fails(tmp_workspace):
    # Make sure we get expected failure using Manifest.from_topdir()
    # with the topdir kwarg when no west.yml exists.

    with pytest.raises(MalformedConfig):
        MT(topdir=tmp_workspace)

def test_from_bad_topdir(tmpdir):
    # If we give a bad temporary directory that isn't a workspace
    # root, that should also fail.

    with pytest.raises(MalformedConfig) as e:
        MT(topdir=tmpdir)
    assert 'local configuration file not found' in str(e.value)

#########################################
# Miscellaneous tests

def test_ignore_west_section():
    # We no longer validate the west section, so things that would
    # once have been schema errors shouldn't be anymore. Projects
    # should still work as expected regardless of what's in there.

    # Parsing manifest only, no exception raised
    manifest = Manifest.from_data(yaml.safe_load('''\
    west:
      url: https://example.com
      revision: abranch
      wrongfield: avalue
    manifest:
      remotes:
      - name: testremote
        url-base: https://example.com
      projects:
      - name: testproject
        remote: testremote
        path: sub/directory
    '''))

    p1 = manifest.projects[1]
    assert PurePath(p1.path) == PurePath('sub', 'directory')

def test_get_projects(tmp_workspace):
    # Coverage for get_projects.

    content = '''\
    manifest:
      projects:
      - name: foo
        url: https://foo.com
    '''

    # Attempting to get an unknown project is an error.
    manifest = Manifest.from_data(yaml.safe_load(content))
    with pytest.raises(ValueError) as e:
        manifest.get_projects(['unknown'])
    # The ValueError args are (unknown, uncloned).
    assert e.value.args[0] == ['unknown']
    assert e.value.args[1] == []

    # For the remainder of the tests, make a manifest file.
    with open(tmp_workspace / 'mp' / 'west.yml', 'w') as f:
        f.write(content)

    # Asking for an uncloned project should fail if only_cloned=False.
    # The ValueError args are (unknown, uncloned).
    manifest = MT(topdir=tmp_workspace)
    with pytest.raises(ValueError) as e:
        manifest.get_projects(['foo'], only_cloned=True)
    unknown, uncloned = e.value.args
    assert unknown == []
    assert len(uncloned) == 1
    assert uncloned[0].name == 'foo'

    # Asking for an uncloned project should succeed if
    # only_cloned=False (the default).
    projects = manifest.get_projects(['foo'])
    assert len(projects) == 1
    assert projects[0].name == 'foo'

    # We can get the manifest project, for now.
    projects = manifest.get_projects(['manifest'])
    assert len(projects) == 1
    assert projects[0].name == 'manifest'
    assert projects[0].abspath == tmp_workspace / 'mp'

    # No project_ids means "all projects".
    projects = manifest.get_projects([])
    assert len(projects) == 2
    assert projects[0].name == 'manifest'
    assert projects[0].is_cloned()
    assert projects[1].name == 'foo'
    with pytest.raises(ValueError) as e:
        projects = manifest.get_projects([], only_cloned=True)
    unknown, uncloned = e.value.args
    assert len(uncloned) == 1
    assert uncloned[0].name == 'foo'

def test_as_dict_and_yaml(manifest_repo):
    # coverage for as_dict, as_frozen_dict, as_yaml, as_frozen_yaml.

    # keep content_str and content_dict in sync.
    content_str = '''\
    manifest:
      projects:
      - name: p1
        url: https://example.com/p1
      - name: p2
        url: https://example.com/p2
        revision: deadbeef
        path: project-two
        clone-depth: 1
        west-commands: commands.yml
    '''
    content_dict = {'manifest':
                    {'projects':
                     [{'name': 'p1',
                       'url': 'https://example.com/p1',
                       'revision': 'master'},
                      {'name': 'p2',
                       'url': 'https://example.com/p2',
                       'revision': 'deadbeef',
                       'path': 'project-two',
                       'clone-depth': 1,
                       'west-commands': 'commands.yml'}],
                     'self': {'path': os.path.basename(manifest_repo)}}}

    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write(content_str)

    fake_sha = 'the-sha'
    frozen_expected = deepcopy(content_dict)
    for p in frozen_expected['manifest']['projects']:
        p['revision'] = fake_sha

    # Manifest.from_file() and Manifest.from_file(topdir=<topdir>) shall
    # produce result when given topdir is identical to what util.west_topdir()
    # produces.
    manifest = MF()

    manifest_topdir = MT(
        topdir=os.path.dirname(manifest_repo))

    # We can always call as_dict() and as_yaml(), regardless of what's
    # cloned.

    as_dict = manifest.as_dict()

    as_dict_topdir = manifest_topdir.as_dict()
    assert as_dict == as_dict_topdir

    yaml_roundtrip = yaml.safe_load(manifest.as_yaml())
    assert as_dict == content_dict
    assert yaml_roundtrip == content_dict

    # With no cloned projects, however, we should not be able to freeze.

    with pytest.raises(RuntimeError) as e:
        manifest.as_frozen_dict()
    assert 'is uncloned' in str(e.value)
    with pytest.raises(RuntimeError) as e:
        manifest.as_frozen_dict()
    assert 'is uncloned' in str(e.value)

    # Test as_frozen_dict() again, with the relevant git methods
    # patched out, for checking expected results.

    def sha_patch_1(*args, **kwargs):
        # Replacement for sha() that succeeds with a fake value.
        return fake_sha

    def sha_patch_2(*args, **kwargs):
        # Replacement that intentionally fails, but without running
        # git.
        raise subprocess.CalledProcessError(1, 'mocked-out')
    with patch('west.manifest.Project.is_cloned',
               side_effect=lambda: True):
        manifest = MF()
        with patch('west.manifest.Project.sha',
                   side_effect=sha_patch_1):
            frozen = manifest.as_frozen_dict()
        assert frozen == frozen_expected

        with patch('west.manifest.Project.sha',
                   side_effect=sha_patch_2):
            with pytest.raises(RuntimeError) as e:
                manifest.as_frozen_dict()
            assert 'cannot be resolved to a SHA' in str(e.value)
            with pytest.raises(RuntimeError) as e:
                manifest.as_frozen_yaml()
            assert 'cannot be resolved to a SHA' in str(e.value)

def test_as_dict_groups():
    # Make sure groups and group-filter round-trip properly.

    actual = Manifest.from_data('''\
    manifest:
      group-filter: [+foo,-bar]
      projects:
        - name: p1
          url: u
        - name: p2
          url: u
          groups:
            - g
    ''').as_dict()['manifest']

    assert actual['group-filter'] == ['-bar']
    assert 'groups' not in actual['projects'][0]
    assert actual['projects'][1]['groups'] == ['g']

def test_version_check_failure():
    # Check that the manifest.version key causes manifest parsing to
    # fail when it should.

    valid_fmt = '''\
    manifest:
      version: {}
      projects:
      - name: foo
        url: https://foo.com
    '''
    invalid_fmt = '''\
    manifest:
      version: {}
      projects:
      - name: foo
        url: https://foo.com
      pytest-invalid-key: a-value
    '''

    # Parsing a well-formed manifest for a version of west greater
    # than our own should raise ManifestVersionError.
    #
    # This should be the case whether the version is a string (as is
    # usual) or, as a special case to work around YAML syntax rules, a
    # float.
    with pytest.raises(ManifestVersionError):
        Manifest.from_data(valid_fmt.format('"99.0"'))
    with pytest.raises(ManifestVersionError):
        Manifest.from_data(valid_fmt.format('99.0'))

    # Parsing Manifests with unsatisfiable version requirements should
    # *not* raise MalformedManifest, even if they have unrecognized keys.
    with pytest.raises(ManifestVersionError):
        Manifest.from_data(invalid_fmt.format('"99.0"'))
    with pytest.raises(ManifestVersionError):
        Manifest.from_data(invalid_fmt.format('99.0'))

    # Manifest versions below 0.6.99 are definitionally invalid,
    # because we added the version feature itself after 0.6.
    with pytest.raises(MalformedManifest):
        Manifest.from_data(invalid_fmt.format('0.0.1'))
    with pytest.raises(MalformedManifest):
        Manifest.from_data(invalid_fmt.format('0.5.0'))
    with pytest.raises(MalformedManifest):
        Manifest.from_data(invalid_fmt.format('0.6'))
    with pytest.raises(MalformedManifest):
        Manifest.from_data(invalid_fmt.format('0.6.9'))
    with pytest.raises(MalformedManifest):
        Manifest.from_data(invalid_fmt.format('0.6.98'))

@pytest.mark.parametrize('ver', sorted(set(['0.6.99', SCHEMA_VERSION] +
                                           _VALID_SCHEMA_VERS)))
def test_version_check_success(ver):
    # Test that version checking succeeds when it should.
    # Always quote the version to avoid issues with floating point,
    # e.g if 'ver' is "0.10", it gets treated like 0.1 in YAML.

    manifest = Manifest.from_data(f'''\
    manifest:
      version: "{ver}"
      projects:
      - name: foo
        url: https://foo.com
    ''')
    assert manifest.projects[-1].name == 'foo'

def test_project_filter_validation(config_tmpdir):
    # Make sure we error out in the expected way when invalid
    # manifest.project-filter options occur anywhere.

    topdir = config_tmpdir / 'test-topdir'
    manifest_repo = topdir / 'mp'
    config = Configuration(topdir=topdir)
    config.set('manifest.path', 'mp')
    create_repo(manifest_repo)
    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write('manifest: {}')

    def clean_up_config_files():
        for configfile in [ConfigFile.SYSTEM,
                           ConfigFile.GLOBAL,
                           ConfigFile.LOCAL]:
            try:
                config.delete('manifest.project-filter',
                              configfile=configfile)
            except KeyError:
                pass

    def check_error(project_filter, expected_err_contains):
        for configfile in [ConfigFile.SYSTEM,
                           ConfigFile.GLOBAL,
                           ConfigFile.LOCAL]:
            clean_up_config_files()
            config.set('manifest.project-filter', project_filter,
                       configfile=configfile)

            with pytest.raises(MalformedConfig) as e:
                MT(topdir=topdir)

            err = str(e.value)
            assert (f'invalid "manifest.project-filter" option value '
                    f'"{project_filter}":') in err
            assert expected_err_contains in err

    check_error('foo', 'element "foo" does not start with "+" or "-"')
    check_error('foo,+bar', 'element "foo" does not start with "+" or "-"')
    check_error('foo , +bar', 'element "foo" does not start with "+" or "-"')
    check_error('+', 'a bare "+" or "-" contains no regular expression')
    check_error('-', 'a bare "+" or "-" contains no regular expression')
    check_error('++', 'invalid regular expression "+":')

def test_project_filter_matching(config_tmpdir):
    # Test manifest.project-filter matching rules by making
    # sure that projects can be made active or inactive. Also
    # test that west ignores empty elements.

    topdir = config_tmpdir / 'test-topdir'
    manifest_repo = topdir / 'mp'
    config = Configuration(topdir=topdir)
    config.set('manifest.path', 'mp')
    create_repo(manifest_repo)
    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write('''
        manifest:
          projects:
            - name: foo
            - name: foobar
            - name: bar

          defaults:
            remote: test
          remotes:
            - name: test
              url-base: ignored
        ''')

    # West currently does not dynamically adjust its conception
    # of what the configuration files said after __init__ time, so
    # we recreate the manifest object every time.

    config.set('manifest.project-filter', '-foo')
    manifest = Manifest.from_topdir(topdir=topdir, config=config)
    foo, foobar, bar = manifest.get_projects(['foo', 'foobar', 'bar'])
    assert not manifest.is_active(foo)
    assert manifest.is_active(foobar)
    assert manifest.is_active(bar)

    config.set('manifest.project-filter', '-foo,-bar')
    manifest = Manifest.from_topdir(topdir=topdir, config=config)
    foo, foobar, bar = manifest.get_projects(['foo', 'foobar', 'bar'])
    assert not manifest.is_active(foo)
    assert manifest.is_active(foobar)
    assert not manifest.is_active(bar)

    config.set('manifest.project-filter', '-foobar,-fo')
    manifest = Manifest.from_topdir(topdir=topdir, config=config)
    foo, foobar, bar = manifest.get_projects(['foo', 'foobar', 'bar'])
    assert manifest.is_active(foo)
    assert not manifest.is_active(foobar)
    assert manifest.is_active(bar)

    # This is equivalent to above: west should ignore the empty element.
    config.set('manifest.project-filter', '-foobar,,-fo')
    manifest = Manifest.from_topdir(topdir=topdir, config=config)
    foo, foobar, bar = manifest.get_projects(['foo', 'foobar', 'bar'])
    assert manifest.is_active(foo)
    assert not manifest.is_active(foobar)
    assert manifest.is_active(bar)

def test_project_filter_precedence(config_tmpdir):
    # Test manifest.project-filter matching rules by making
    # sure that projects can be made active or inactive.

    topdir = config_tmpdir / 'test-topdir'
    manifest_repo = topdir / 'mp'
    config = Configuration(topdir=topdir)
    config.set('manifest.path', 'mp')
    create_repo(manifest_repo)
    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write('''
        manifest:
          projects:
            - name: foo
            - name: bar
            - name: baz

          defaults:
            remote: test
          remotes:
            - name: test
              url-base: ignored
        ''')

    # West currently does not dynamically adjust its conception
    # of what the configuration files said after __init__ time, so
    # we recreate the manifest object every time.

    # Global has higher precedence than system.
    config.set('manifest.project-filter', '-foo,-bar,-baz',
               configfile=ConfigFile.SYSTEM)
    config.set('manifest.project-filter', '-foo',
               configfile=ConfigFile.GLOBAL)
    manifest = Manifest.from_topdir(topdir=topdir, config=config)
    foo, bar, baz = manifest.get_projects(['foo', 'bar', 'baz'])
    assert not manifest.is_active(foo)
    assert manifest.is_active(bar)
    assert manifest.is_active(baz)

    # Local has higher precedence than either.
    config.set('manifest.project-filter', '-bar,-f.*',
               configfile=ConfigFile.LOCAL)
    manifest = Manifest.from_topdir(topdir=topdir, config=config)
    foo, bar, baz = manifest.get_projects(['foo', 'bar', 'baz'])
    assert not manifest.is_active(foo)
    assert not manifest.is_active(bar)
    assert manifest.is_active(baz)

def test_project_filter_inactive_prevents_import(config_tmpdir):
    # West should not try to import from inactive projects.
    # West should import from active projects.

    topdir = config_tmpdir / 'test-topdir'
    manifest_repo = topdir / 'mp'
    config = Configuration(topdir=topdir)
    config.set('manifest.path', 'mp')
    config.set('manifest.project-filter', '-foo')
    create_repo(manifest_repo)
    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write('''
        manifest:
          projects:
            - name: foo
              url: ignored
              import: true
        ''')

    # With foo inactive, we can load the project but its import
    # is ignored.
    manifest = Manifest.from_topdir(topdir=topdir, config=config)
    assert not manifest.is_active(manifest.get_projects(['foo'])[0])

    # Making foo active will try to do the import and thus fail
    # to resolve the manifest.
    config.set('manifest.project-filter', '+foo')
    with pytest.raises(ManifestImportFailed):
        Manifest.from_topdir(topdir=topdir, config=config)

def test_project_filter_warnings_and_errors(config_tmpdir, caplog):
    topdir = config_tmpdir / 'test-topdir'
    manifest_repo = topdir / 'mp'
    config = Configuration(topdir=topdir)
    config.set('manifest.path', 'mp')
    create_repo(manifest_repo)
    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write('''
        manifest:
          projects:
            - name: foo,bar
              url: ignored
        ''')

    Manifest.from_topdir(topdir=topdir, config=config)
    warned = False
    for source, level, message in caplog.record_tuples:
        if source != 'west.manifest':
            continue
        if level != logging.WARNING:
            continue
        if not message.startswith('project "foo,bar"'):
            continue
        if 'contains comma (",") or whitespace' in message:
            warned = True
    assert warned, caplog.record_tuples

    config.set('manifest.project-filter', '+arbitrary')
    with pytest.raises(MalformedConfig) as e:
        Manifest.from_topdir(topdir=topdir, config=config)
    err = str(e.value)
    assert 'project "foo,bar"' in err
    assert 'contains comma (",") or whitespace' in err

#########################################
# Manifest import tests

def make_importer(import_map):
    # Helper function for making a simple importer for test cases.
    #
    # The argument is a map from (project_name, path, revision) tuples
    # to the manifest contents the importer should return.
    #
    # This, makes it easier to set up tests cases where import
    # resolution can be done entirely with data in this file. That's
    # faster (both when writing tests and running them) than setting
    # up a west workspace on the file system.

    def importer(project, file):
        return import_map[(project.name, file)]
    return importer

def test_import_false_ok():
    # When it would have no effect, it's OK to parse manifest data
    # with imports in it, even without an importer. The project data
    # should be parsed as expected.

    manifest = Manifest.from_data('''\
    manifest:
      projects:
        - name: foo
          url: https://foo.com
          import: false
    ''')
    assert manifest.projects[-1].name == 'foo'

# A stand-in for zephyr/west.yml to use when testing manifest imports.
# This feature isn't tied to Zephyr in any way, but we write the tests
# this way to make them easier to read and relate to Zephyr use cases.
_UPSTREAM_WYML = '''\
manifest:
  defaults:
    remote: up-rem
  remotes:
    - name: up-rem
      url-base: upstream.com
  projects:
    - name: hal_nordic
      revision: hal_nordic-upstream-rev
      path: modules/hal/nordic
    - name: segger
      path: modules/debug/segger
      revision: segger-upstream-rev
'''

_DOWNSTREAM_WYMLS = [
    '''\
    manifest:
      projects:
      - name: upstream
        url: upstream.com/upstream
        revision: refs/tags/v1.0
        import: true
    ''',
    '''\
    manifest:
      projects:
      - name: upstream
        url: upstream.com/upstream
        revision: refs/tags/v1.0
        import: west.yml
    ''',
    '''\
    manifest:
      remotes:
      - name: upstream-remote
        url-base: upstream.com
      projects:
      - name: upstream
        remote: upstream-remote
        revision: refs/tags/v1.0
        import: true
    ''',
    '''\
    manifest:
      remotes:
      - name: upstream-remote
        url-base: upstream.com
      projects:
      - name: upstream
        remote: upstream-remote
        revision: refs/tags/v1.0
        import: west.yml
    ''',
    '''\
    manifest:
      defaults:
        remote: upstream-remote
      remotes:
      - name: upstream-remote
        url-base: upstream.com
      projects:
      - name: upstream
        revision: refs/tags/v1.0
        import: west.yml
    '''
]

@pytest.mark.parametrize('content', _DOWNSTREAM_WYMLS,
                         ids=['url-true', 'url-west',
                              'remote-true', 'remote-west',
                              'default-remote'])
def test_import_basics(content):
    # Test a downstream manifest, which simply imports a tag from an
    # upstream manifest.
    #
    # This tests the import semantics for "Downstream of a fixed
    # Zephyr release" in the documentation for this feature, in various ways.
    #
    # It of course doesn't test any file sytem or network related
    # features required to make west update, west manifest --freeze,
    # etc. work.
    #
    # Here, the main west.yml simply imports upstream/west.yml.
    # We expect the projects list to be the same as upstream's,
    # with the addition of one project (upstream itself).

    importer = make_importer({('upstream', 'west.yml'): _UPSTREAM_WYML})
    actual = Manifest.from_data(content, importer=importer,
                                import_flags=FPI).projects

    expected = [
        ManifestProject(),
        Project('upstream', 'upstream.com/upstream', revision='refs/tags/v1.0',
                path='upstream'),
        Project('hal_nordic', 'upstream.com/hal_nordic',
                revision='hal_nordic-upstream-rev',
                path='modules/hal/nordic'),
        Project('segger', 'upstream.com/segger',
                revision='segger-upstream-rev',
                path='modules/debug/segger')]

    for a, e in zip(actual, expected):
        check_proj_consistency(a, e)

def test_import_with_fork_and_proj():
    # Downstream of fixed release, one forked project, and one
    # additional non-forked project.
    #
    # This verifies that common projects are merged into the previous
    # list, and downstream-only projects are appended onto it.

    importer = make_importer({('upstream', 'west.yml'): _UPSTREAM_WYML})
    actual = Manifest.from_data('''\
    manifest:
      projects:
      - name: hal_nordic
        path: modules/hal/nordic
        url: downstream.com/hal_nordic
        revision: my-branch
      - name: my-proj
        url: downstream.com/my-proj
      - name: upstream
        url: upstream.com/upstream
        revision: refs/tags/v1.0
        import: true
     ''',
                                importer=importer,
                                import_flags=FPI).projects

    expected = [
        ManifestProject(),
        Project('hal_nordic', 'downstream.com/hal_nordic',
                revision='my-branch', path='modules/hal/nordic'),
        Project('my-proj', 'downstream.com/my-proj', revision='master',
                path='my-proj'),
        Project('upstream', 'upstream.com/upstream', revision='refs/tags/v1.0',
                path='upstream'),
        Project('segger', 'upstream.com/segger',
                revision='segger-upstream-rev',
                path='modules/debug/segger')]

    for a, e in zip(actual, expected):
        check_proj_consistency(a, e)

def test_import_project_list(manifest_repo):
    # We should be able to import a list of files from a project at a
    # revision. The files should come from git, not the file system.

    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write('''\
        manifest:
          projects:
          - name: p1
            url: p1-url
            import:
            - m1.yml
            - m2.yml
          self:
            path: mp
        ''')

    topdir = manifest_repo.topdir
    p1 = topdir / 'p1'
    create_repo(p1)
    create_branch(p1, 'manifest-rev', checkout=True)
    add_commit(p1, 'add m1.yml and m2.yml',
               files={'m1.yml': '''\
                                manifest:
                                  projects:
                                  - name: p2
                                    url: p2-url
                                ''',
                      'm2.yml': '''\
                                manifest:
                                  projects:
                                  - name: p3
                                    url: p3-url
                                '''})
    assert (p1 / 'm1.yml').is_file()
    assert (p1 / 'm2.yml').is_file()
    checkout_branch(p1, 'master')
    assert not (p1 / 'm1.yml').exists()
    assert not (p1 / 'm2.yml').exists()

    actual = MF().projects
    expected = [ManifestProject(path='mp', topdir=topdir),
                Project('p1', 'p1-url', topdir=topdir),
                Project('p2', 'p2-url', topdir=topdir),
                Project('p3', 'p3-url', topdir=topdir)]

    for a, e in zip(actual, expected):
        check_proj_consistency(a, e)

def test_import_project_directory(manifest_repo):
    # We should be able to import manifest files in a directory from a
    # revision. The files should come from git, not the file system.

    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write('''\
        manifest:
          projects:
          - name: p1
            url: p1-url
            import: d
          self:
            path: mp
        ''')

    topdir = manifest_repo.topdir
    p1 = topdir / 'p1'
    create_repo(p1)
    create_branch(p1, 'manifest-rev', checkout=True)
    add_commit(p1, 'add directory of submanifests',
               files={p1 / 'd' / 'ignore-me.txt':
                      'blah blah blah',
                      p1 / 'd' / 'm1.yml':
                      '''\
                      manifest:
                        projects:
                        - name: p2
                          url: p2-url
                      ''',
                      p1 / 'd' / 'm2.yml':
                      '''\
                      manifest:
                        projects:
                        - name: p3
                          url: p3-url
                      '''})
    assert (p1 / 'd').is_dir()
    assert (p1 / 'd' / 'ignore-me.txt').is_file()
    assert (p1 / 'd' / 'm1.yml').is_file()
    assert (p1 / 'd' / 'm2.yml').is_file()
    checkout_branch(p1, 'master')
    assert not (p1 / 'd').exists()

    actual = MF().projects
    expected = [ManifestProject(path='mp', topdir=topdir),
                Project('p1', 'p1-url', topdir=topdir),
                Project('p2', 'p2-url', topdir=topdir),
                Project('p3', 'p3-url', topdir=topdir)]

    for a, e in zip(actual, expected):
        check_proj_consistency(a, e)

def test_import_project_err_malformed(manifest_repo):
    # Checks for erroneous or malformed imports from projects.

    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write('''\
        manifest:
          projects:
          - name: p
            url: p-url
            import: true
        ''')

    p = manifest_repo / '..' / 'p'
    subm = p / 'west.yml'
    create_repo(p)
    create_branch(p, 'manifest-rev', checkout=True)

    add_commit(p, 'not a dictionary', files={subm: 'not-a-manifest'})
    with pytest.raises(MalformedManifest):
        MF()

    add_commit(p, 'not a valid manifest', files={subm: 'manifest: not'})
    with pytest.raises(MalformedManifest):
        MF()

    subprocess.check_call([GIT, 'checkout', '--detach', 'HEAD'], cwd=p)
    subprocess.check_call([GIT, 'update-ref', '-d', 'refs/heads/manifest-rev'],
                          cwd=p)
    with pytest.raises(ManifestImportFailed):
        MF()
    subprocess.check_call([GIT, 'update-ref', 'refs/heads/manifest-rev',
                           'HEAD'], cwd=p)

    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write('''\
        manifest:
          projects:
          - name: p
            url: p-url
            import: not-a-file
        ''')
    with pytest.raises(ManifestImportFailed) as e:
        MF()
    assert 'not-a-file' in str(e.value)

    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write('''\
        manifest:
          projects:
          - name: p
            url: p-url
            import: not-a-file
        ''')
    with pytest.raises(ManifestImportFailed):
        MF()

def test_import_project_submanifest_commands(manifest_repo):
    # If a project has no west-commands, but an imported manifest
    # inside it defines some, they should be inherited in the parent.

    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write('''\
        manifest:
          projects:
          - name: p1
            url: p1-url
            import:
            - m1.yml
            - m2.yml
        ''')

    p1 = manifest_repo / '..' / 'p1'
    create_repo(p1)
    create_branch(p1, 'manifest-rev', checkout=True)
    add_commit(p1, 'add m1.yml and m2.yml',
               files={'m1.yml': '''\
                                manifest:
                                  projects:
                                  - name: p2
                                    url: p2-url
                                  self:
                                    west-commands: m1-commands.yml
                                ''',
                      'm2.yml': '''\
                                manifest:
                                  projects:
                                  - name: p3
                                    url: p3-url
                                  self:
                                    west-commands: m2-commands.yml
                                '''})
    checkout_branch(p1, 'master')
    assert (p1 / 'm1.yml').check(file=0, dir=0)
    assert (p1 / 'm2.yml').check(file=0, dir=0)

    p1 = MF().get_projects(['p1'])[0]
    expected = ['m1-commands.yml', 'm2-commands.yml']
    assert p1.west_commands == expected

def test_import_project_submanifest_commands_both(manifest_repo):
    # Like test_import_project_submanifest_commands, but making sure
    # that if multiple west-commands appear throughout the imported
    # manifests, then west_commands is a list of all of them, resolved
    # in import order.

    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write('''\
        manifest:
          projects:
          - name: p1
            url: p1-url
            import:
            - m1.yml
            - m2.yml
            west-commands: p1-commands.yml
        ''')

    p1 = manifest_repo / '..' / 'p1'
    create_repo(p1)
    create_branch(p1, 'manifest-rev', checkout=True)
    add_commit(p1, 'add m1.yml and m2.yml',
               files={'m1.yml': '''\
                                manifest:
                                  projects:
                                  - name: p2
                                    url: p2-url
                                  self:
                                    west-commands: m1-commands.yml
                                ''',
                      'm2.yml': '''\
                                manifest:
                                  projects:
                                  - name: p3
                                    url: p3-url
                                  self:
                                    west-commands: m2-commands.yml
                                '''})
    checkout_branch(p1, 'master')
    assert (p1 / 'm1.yml').check(file=0, dir=0)
    assert (p1 / 'm2.yml').check(file=0, dir=0)

    p1 = MF().get_projects(['p1'])[0]
    expected = ['p1-commands.yml', 'm1-commands.yml', 'm2-commands.yml']
    assert p1.west_commands == expected

def test_import_map_error_handling():
    # Make sure we handle expected errors when loading import:
    # values that are maps.

    def importer(*args, **kwargs):
        return None

    def make_manifest(import_map):
        return Manifest.from_data({'manifest':
                                   {'projects':
                                    [{'name': 'foo',
                                      'url': 'ignored',
                                      'import': import_map}]}},
                                  importer=importer)

    def check_error(import_map, expected_err_contains):
        with pytest.raises(MalformedManifest) as e:
            make_manifest(import_map)
        assert expected_err_contains in str(e.value)

    # Unexpected keys are errors.
    check_error({'invalid-key': 1}, 'invalid import contents')
    # Invalid types for map keys are errors.
    check_error({'name-allowlist': {}}, 'bad import name-allowlist')
    check_error({'path-allowlist': {}}, 'bad import path-allowlist')
    check_error({'name-blocklist': {}}, 'bad import name-blocklist')
    check_error({'path-blocklist': {}}, 'bad import path-blocklist')
    check_error({'path-prefix': {}}, 'bad import path-prefix')

# A manifest repository with a subdirectory containing multiple
# additional files:
#
# mp/
# ├── west.d
# │   ├── 01-libraries.yml
# │   ├── 02-vendor-hals.yml
# │   └── 03-applications.yml
# └── west.yml
#
# This tests "Downstream with directory of manifest files" in the
# documentation. We do the testing in a tmpdir with just enough
# files to fake out a workspace.
_IMPORT_SELF_MANIFESTS = [
    # as a directory:
    '''\
    manifest:
      remotes:
        - name: upstream
          url-base: upstream.com
      projects:
        - name: upstream
          remote: upstream
          revision: refs/tags/v1.0
          import: true
      self:
        import: west.d
    ''',
    # as an equivalent sequence of files:
    '''\
    manifest:
      remotes:
        - name: upstream
          url-base: upstream.com
      projects:
        - name: upstream
          remote: upstream
          revision: refs/tags/v1.0
          import: true
      self:
        import:
          - west.d/01-libraries.yml
          - west.d/02-vendor-hals.yml
          - west.d/03-applications.yml
    '''
    # as an equivalent map:
    '''\
    manifest:
      remotes:
        - name: upstream
          url-base: upstream.com
      projects:
        - name: upstream
          remote: upstream
          revision: refs/tags/v1.0
          import: true
      self:
        import:
          file: west.d
    '''
]

_IMPORT_SELF_SUBMANIFESTS = {
    'west.d/01-libraries.yml':
    '''\
    manifest:
      defaults:
        remote: my-downstream
      remotes:
      - name: my-downstream
        url-base: downstream.com
      projects:
      - name: my-1
        repo-path: my-lib-1
        revision: my-1-rev
        path: lib/my-1
      - name: my-2
        repo-path: my-lib-2
        revision: my-2-rev
        path: lib/my-2
    ''',

    'west.d/02-vendor-hals.yml':
    '''\
    manifest:
      projects:
      - name: hal_nordic
        url: downstream.com/hal_nordic
        revision: my-hal-rev
        path: modules/hal/nordic
      - name: hal_downstream_sauce
        url: downstream.com/hal_downstream_only
        revision: my-down-hal-rev
        path: modules/hal/downstream_only
    ''',

    'west.d/03-applications.yml':
    '''\
    manifest:
      projects:
      - name: my-app
        url: downstream.com/my-app
        revision: my-app-rev
        path: applications/my-app
    '''
}


def _setup_import_self(tmp_workspace, manifests):
    manifest_repo = tmp_workspace / 'mp'
    (manifest_repo / 'west.d').mkdir()
    for path, content in manifests.items():
        with open(str(manifest_repo / path), 'w') as f:
            f.write(content)

@pytest.mark.parametrize('content', _IMPORT_SELF_MANIFESTS,
                         ids=['dir', 'files'])
def test_import_self_directory(content, tmp_workspace):
    # Test a couple of different equivalent ways to import content
    # from the manifest repository.

    call_map = {('upstream', 'west.yml'): _UPSTREAM_WYML}
    # Create the manifest files.
    manifests = {'west.yml': content}
    manifests.update(_IMPORT_SELF_SUBMANIFESTS)
    _setup_import_self(tmp_workspace, manifests)

    # Resolve the manifest. The mp/west.d content comes
    # from the file system in this case.
    actual = MT(topdir=tmp_workspace,
                importer=make_importer(call_map),
                import_flags=FPI).projects

    expected = [
        ManifestProject(path='mp', topdir=tmp_workspace),
        # Projects from 01-libraries.yml come first.
        Project('my-1', 'downstream.com/my-lib-1', revision='my-1-rev',
                path='lib/my-1', topdir=tmp_workspace),
        Project('my-2', 'downstream.com/my-lib-2', revision='my-2-rev',
                path='lib/my-2', topdir=tmp_workspace),
        # Next, projects from 02-vendor-hals.yml.
        Project('hal_nordic', 'downstream.com/hal_nordic',
                revision='my-hal-rev', path='modules/hal/nordic',
                topdir=tmp_workspace),
        Project('hal_downstream_sauce', 'downstream.com/hal_downstream_only',
                revision='my-down-hal-rev', path='modules/hal/downstream_only',
                topdir=tmp_workspace),
        # After that, 03-applications.yml.
        Project('my-app', 'downstream.com/my-app', revision='my-app-rev',
                path='applications/my-app', topdir=tmp_workspace),
        # upstream is the only element of our projects list, so it's
        # after all the self-imports.
        Project('upstream', 'upstream.com/upstream', revision='refs/tags/v1.0',
                path='upstream', topdir=tmp_workspace),
        # Projects we imported from upstream are last. Projects
        # present upstream which we have already defined should be
        # ignored and not appear here.
        Project('segger', 'upstream.com/segger',
                revision='segger-upstream-rev',
                path='modules/debug/segger', topdir=tmp_workspace),
    ]

    # Since this test is a bit more complicated than some others,
    # first check that we have all the projects in the right order.
    assert [a.name for a in actual] == [e.name for e in expected]

    # With the basic check done, do a more detailed check.
    for a, e in zip(actual, expected):
        check_proj_consistency(a, e)

def test_import_self_bool():
    # Importing a boolean from self is an error and must fail.

    with pytest.raises(MalformedManifest) as e:
        M('''\
        projects:
        - name: p
          url: u
        self:
          import: true''')
    assert 'of boolean' in str(e.value)
    with pytest.raises(MalformedManifest) as e:
        M('''\
        projects:
        - name: p
          url: u
        self:
          import: false''')
    assert 'of boolean' in str(e.value)

def test_import_self_err_malformed(manifest_repo):
    # Checks for erroneous or malformed imports from self.

    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write('''\
        manifest:
          projects:
          - name: p
            url: u
          self:
            import: not-a-file''')
    with pytest.raises(MalformedManifest) as e:
        MF()
    str_value = str(e.value)
    assert 'not found' in str_value
    assert 'not-a-file' in str_value

def test_import_self_submanifest_commands(manifest_repo):
    # If we import a sub-manifest from 'self' that has west commands
    # in its own self section, those should be treated as if they were
    # declared in the top-level self section.

    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write('''\
        manifest:
          projects:
          - name: p1
            url: u1
          self:
            import: sub-manifest.yml
        ''')

    with open(manifest_repo / 'sub-manifest.yml', 'w') as f:
        f.write('''\
        manifest:
          projects:
          - name: p2
            url: u2
          self:
            west-commands: sub-commands.yml
        ''')

    mp = MF().projects[MANIFEST_PROJECT_INDEX]
    assert mp.west_commands == ['sub-commands.yml']

def test_import_self_submanifest_commands_both(manifest_repo):
    # Like test_import_self_submanifest_commands, but making sure that
    # if multiple west-commands appear throughout the imported manifests,
    # then west_commands is a list of all of them, resolved in import order.

    top = '''\
    manifest:
      projects:
      - name: p1
        url: u1
      self:
        import: sub-manifest.yml
        west-commands: top-commands.yml
    '''
    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write(top)

    sub = '''\
    manifest:
      projects:
      - name: p2
        url: u2
      self:
        west-commands: sub-commands.yml
    '''
    with open(manifest_repo / 'sub-manifest.yml', 'w') as f:
        f.write(sub)

    mp = MF().projects[MANIFEST_PROJECT_INDEX]
    assert mp.west_commands == ['sub-commands.yml', 'top-commands.yml']

def test_import_flags_ignore(tmpdir):
    # Test the IGNORE flag by verifying we can create manifest
    # instances that should error out if the import was not ignored.

    m = M('''\
    projects:
    - name: foo
      url: https://example.com
      import: true
    ''', import_flags=ImportFlag.IGNORE)
    assert m.get_projects(['foo'])

    m = M('''\
    projects:
    - name: foo
      url: https://example.com
    self:
      import: a-file
    ''', import_flags=ImportFlag.IGNORE)
    assert m.get_projects(['foo'])

def test_import_map_name_allowlist(manifest_repo):

    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write('''
        manifest:
          projects:
            - name: mainline
              url: https://git.example.com/mainline/manifest
              import:
                name-allowlist:
                  - mainline-app
                  - lib2
            - name: downstream-app
              url: https://git.example.com/downstream/app
            - name: lib3
              path: libraries/lib3
              url: https://git.example.com/downstream/lib3
          self:
            path: mp
        ''')

    mainline = manifest_repo.topdir / 'mainline'
    create_repo(mainline)
    create_branch(mainline, 'manifest-rev', checkout=True)
    add_commit(mainline, 'mainline/west.yml',
               files={'west.yml':
                      '''
                      manifest:
                        projects:
                          - name: mainline-app
                            path: examples/app
                            url: https://git.example.com/mainline/app
                          - name: lib
                            path: libraries/lib
                            url: https://git.example.com/mainline/lib
                          - name: lib2
                            path: libraries/lib2
                            url: https://git.example.com/mainline/lib2
                      '''})
    checkout_branch(mainline, 'master')

    actual = [project.name for project in MF().projects]

    expected = [
        'manifest',
        'mainline',
        'downstream-app',
        'lib3',
        'mainline-app',
        'lib2',
    ]

    assert actual == expected

def test_import_map_name_allowlist_legacy(manifest_repo):
    # This tests the legacy support for blocklists and allowlists
    # through the blacklist and whitelist keywords which cannot
    # be removed because they are part of project's west.yaml
    # and this would break users ability to use git bisect.

    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write('''
        manifest:
          projects:
            - name: mainline
              url: https://git.example.com/mainline/manifest
              import:
                name-whitelist:
                  - mainline-app
                  - lib2
            - name: downstream-app
              url: https://git.example.com/downstream/app
            - name: lib3
              path: libraries/lib3
              url: https://git.example.com/downstream/lib3
          self:
            path: mp
        ''')

    mainline = manifest_repo.topdir / 'mainline'
    create_repo(mainline)
    create_branch(mainline, 'manifest-rev', checkout=True)
    add_commit(mainline, 'mainline/west.yml',
               files={'west.yml':
                      '''
                      manifest:
                        projects:
                          - name: mainline-app
                            path: examples/app
                            url: https://git.example.com/mainline/app
                          - name: lib
                            path: libraries/lib
                            url: https://git.example.com/mainline/lib
                          - name: lib2
                            path: libraries/lib2
                            url: https://git.example.com/mainline/lib2
                      '''})
    checkout_branch(mainline, 'master')

    actual = [project.name for project in MF().projects]

    expected = [
        'manifest',
        'mainline',
        'downstream-app',
        'lib3',
        'mainline-app',
        'lib2'
    ]

    assert actual == expected

def test_import_map_filter_propagation(manifest_repo):
    # blocklists and allowlists need to propagate down imports.

    # For this test, we'll write a west.yml which imports level2.yml
    # with various allowlist and blocklist settings. The file
    # level2.yml exists only to import level3.yml, adding a layer of
    # imports in between west.yml (which defines the filters)
    # and level3.yml (which defines the projects being filtered).
    #
    # We then make sure the filters are applied on level3.yml's
    # projects in the final resolved manifest.

    with open(manifest_repo / 'level2.yml', 'w') as f:
        f.write('''
        manifest:
          projects: []
          self:
            import: level3.yml
        ''')

    with open(manifest_repo / 'level3.yml', 'w') as f:
        f.write('''
        manifest:
          defaults: {remote: r}
          remotes: [{name: r, url-base: u}]
          projects:
          - name: n1
            path: p1
          - name: n2
            path: p2
        ''')

    # Since we need a few different test cases with the above setup,
    # introduce some helpers. It might be nicer to make this a
    # parametrized test at some point, but this will do.

    import_map = {}
    west_yml = {'manifest':
                {'projects': [],
                 'self': {'import': import_map}}}

    def load_manifest(import_map_vals):
        import_map.clear()
        import_map['file'] = 'level2.yml'
        import_map.update(import_map_vals)
        with open(manifest_repo / 'west.yml', 'w') as f:
            f.write(yaml.dump(west_yml))
        return MF()

    projects = load_manifest({'name-allowlist': 'n2'}).projects
    assert len(projects) == 2
    assert projects[1].name == 'n2'

    projects = load_manifest({'name-blocklist': 'n2'}).projects
    assert len(projects) == 2
    assert projects[1].name == 'n1'

    projects = load_manifest({'name-blocklist': 'n2',
                              'name-allowlist': 'n2'}).projects
    assert len(projects) == 2
    assert projects[1].name == 'n2'

    projects = load_manifest({'path-blocklist': 'p*'}).projects
    assert len(projects) == 1

    projects = load_manifest({'path-blocklist': 'p1'}).projects
    assert len(projects) == 2
    assert projects[1].name == 'n2'

def test_import_map_filter_propagation_legacy(manifest_repo):
    # This tests the legacy support for blocklists and allowlists
    # through the blacklist and whitelist keywords which cannot
    # be removed because they are part of project's west.yaml
    # and this would break users ability to use git bisect.

    # For this test, we'll write a west.yml which imports level2.yml
    # with various whitelist and blacklist settings. The file
    # level2.yml exists only to import level3.yml, adding a layer of
    # imports in between west.yml (which defines the filters)
    # and level3.yml (which defines the projects being filtered).
    #
    # We then make sure the filters are applied on level3.yml's
    # projects in the final resolved manifest.

    with open(manifest_repo / 'level2.yml', 'w') as f:
        f.write('''
        manifest:
          projects: []
          self:
            import: level3.yml
        ''')

    with open(manifest_repo / 'level3.yml', 'w') as f:
        f.write('''
        manifest:
          defaults: {remote: r}
          remotes: [{name: r, url-base: u}]
          projects:
          - name: n1
            path: p1
          - name: n2
            path: p2
        ''')

    # Since we need a few different test cases with the above setup,
    # introduce some helpers. It might be nicer to make this a
    # parametrized test at some point, but this will do.

    import_map = {}
    west_yml = {'manifest':
                {'projects': [],
                 'self': {'import': import_map}}}

    def load_manifest(import_map_vals):
        import_map.clear()
        import_map['file'] = 'level2.yml'
        import_map.update(import_map_vals)
        with open(manifest_repo / 'west.yml', 'w') as f:
            f.write(yaml.dump(west_yml))
        return MF()

    projects = load_manifest({'name-whitelist': 'n2'}).projects
    assert len(projects) == 2
    assert projects[1].name == 'n2'

    projects = load_manifest({'name-blacklist': 'n2'}).projects
    assert len(projects) == 2
    assert projects[1].name == 'n1'

    projects = load_manifest({'name-blacklist': 'n2',
                              'name-whitelist': 'n2'}).projects
    assert len(projects) == 2
    assert projects[1].name == 'n2'

    projects = load_manifest({'path-blacklist': 'p*'}).projects
    assert len(projects) == 1

    projects = load_manifest({'path-blacklist': 'p1'}).projects
    assert len(projects) == 2
    assert projects[1].name == 'n2'

def test_import_path_prefix_basics(manifest_repo):
    # The semantics for "import: {path-prefix: ...}" are that the
    # path-prefix is:
    #
    # - prepended to each project.path, including the imported project
    # - inserted properly into each project.abspath, project.posixpath
    # - allowed to, but not required to, have multiple components

    # Save typing
    topdir = manifest_repo.topdir

    # Create some projects to import from and some manifest data
    # inside each.
    prefixes = {
        1: 'prefix-1',
        2: 'prefix/2',
        3: 'pre/fix/3'
    }
    revs = {}
    for i in [1, 2, 3]:
        p = Path(topdir / prefixes[i] / f'project-{i}')
        create_repo(p)
        create_branch(p, 'manifest-rev', checkout=True)
        add_commit(p, f'project-{i} manifest',
                   files={
                       'west.yml': f'''
                       manifest:
                         projects:
                         - name: not-cloned-{i}
                           url: https://example.com/not-cloned-{i}
                       '''
                   },
                   reconfigure=False)
        revs[i] = rev_parse(p, 'HEAD')

    # Create the main manifest file, which imports these with
    # different prefixes.
    add_commit(manifest_repo, 'add main manifest with import',
               files={
                   'west.yml': f'''
                   manifest:
                     remotes:
                     - name: r
                       url-base: https://example.com
                     defaults:
                       remote: r

                     projects:
                     - name: project-1
                       revision: {revs[1]}
                       import:
                         path-prefix: {prefixes[1]}
                     - name: project-2
                       revision: {revs[2]}
                       import:
                         path-prefix: {prefixes[2]}
                     - name: project-3
                       revision: {revs[3]}
                       import:
                         path-prefix: {prefixes[3]}
                   '''
               },
               reconfigure=False)

    # Check semantics for directly imported projects and nested imports.
    actual = MT(topdir=topdir).projects
    expected = [ManifestProject(path='mp', topdir=topdir),
                # Projects in main west.yml with proper path-prefixing
                # applied.
                Project('project-1', 'https://example.com/project-1',
                        revision=revs[1],
                        path='prefix-1/project-1',
                        topdir=topdir, remote_name='r'),
                Project('project-2', 'https://example.com/project-2',
                        revision=revs[2],
                        path='prefix/2/project-2',
                        topdir=topdir, remote_name='r'),
                Project('project-3', 'https://example.com/project-3',
                        revision=revs[3],
                        path='pre/fix/3/project-3',
                        topdir=topdir, remote_name='r'),
                # Imported projects from submanifests. These aren't
                # actually cloned on the file system, but that doesn't
                # matter for this test.
                Project('not-cloned-1', 'https://example.com/not-cloned-1',
                        path='prefix-1/not-cloned-1', topdir=topdir),
                Project('not-cloned-2', 'https://example.com/not-cloned-2',
                        path='prefix/2/not-cloned-2', topdir=topdir),
                Project('not-cloned-3', 'https://example.com/not-cloned-3',
                        path='pre/fix/3/not-cloned-3', topdir=topdir)]
    for a, e in zip(actual, expected):
        check_proj_consistency(a, e)

def test_import_path_prefix_self(manifest_repo):
    # The semantics for "self: import: {path-prefix: ...}" are similar
    # to when it's used from a project, except the path-prefix is not
    # prepended to the manifest repository's path.

    # Save typing
    topdir = manifest_repo.topdir

    # Create the main manifest file.
    add_commit(manifest_repo, 'add main manifest with import',
               files={
                   'west.yml': '''
                   manifest:
                     projects: []
                     self:
                       path: mp
                       import:
                         file: foo.yml
                         path-prefix: bar
                   ''',

                   'foo.yml': '''
                   manifest:
                     projects: []
                   '''
               },
               reconfigure=False)

    # Check semantics for directly imported projects and nested imports.
    actual = MT(topdir=topdir).projects[0]
    expected = ManifestProject(path='mp', topdir=topdir)
    check_proj_consistency(actual, expected)

def test_import_path_prefix_propagation(manifest_repo):
    # An "import: {path-prefix: foo}" of a manifest which itself
    # contains an "import: {path-prefix: bar}" should have a combined
    # path-prefix foo/bar, etc.

    # Save typing
    topdir = manifest_repo.topdir

    # Create the main manifest file.
    add_commit(manifest_repo, 'add main manifest with import',
               files={
                   'west.yml': '''
                   manifest:
                     projects: []
                     self:
                       path: mp
                       import:
                         file: foo.yml
                         path-prefix: prefix/1
                   ''',

                   'foo.yml': '''
                   manifest:
                     projects: []
                     self:
                       import:
                         file: bar.yml
                         path-prefix: prefix-2
                   ''',

                   'bar.yml': '''
                   manifest:
                     projects:
                     - name: project-1
                       path: project-one-path
                       url: https://example.com/project-1
                     - name: project-2
                       url: https://example.com/project-2
                   '''
               },
               reconfigure=False)

    # Check semantics for directly imported projects and nested imports.
    actual = MT(topdir=topdir).projects[1:]
    expected = [Project('project-1', 'https://example.com/project-1',
                        path='prefix/1/prefix-2/project-one-path',
                        topdir=topdir),
                Project('project-2', 'https://example.com/project-2',
                        path='prefix/1/prefix-2/project-2',
                        topdir=topdir)]
    for a, e in zip(actual, expected):
        check_proj_consistency(a, e)

def test_import_path_prefix_no_escape(manifest_repo):
    # An "import: {path-prefix: ...}" must not escape (or even equal) topdir.

    topdir = manifest_repo.topdir

    manifest_template = '''
    manifest:
      projects:
      - name: project
        url: https://example.com/project
        import:
          path-prefix: THE_PATH_PREFIX
    '''

    def mfst(path_prefix):
        return manifest_template.replace('THE_PATH_PREFIX', path_prefix)

    # As a base case, make sure we can parse this manifest with an
    # OK path-prefix.
    add_commit(manifest_repo, 'OK',
               files={'west.yml': mfst('ext')},
               reconfigure=False)
    m = MT(topdir=topdir, import_flags=ImportFlag.IGNORE)
    assert (Path(m.projects[1].abspath) ==
            Path(topdir) / 'ext' / 'project')

    # An invalid path-prefix, all other things equal, should fail.
    add_commit(manifest_repo, 'NOK 1',
               files={'west.yml': mfst('..')},
               reconfigure=False)
    with pytest.raises(MalformedManifest) as excinfo:
        MT(topdir=topdir, import_flags=ImportFlag.IGNORE)
    assert 'escapes the workspace topdir' in str(excinfo.value)

def test_import_loop_detection_self(manifest_repo):
    # Verify that a self-import which causes an import loop is an error.

    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write('''
        manifest:
          projects: []
          self:
           import: foo.yml
        ''')

    with open(manifest_repo / 'foo.yml', 'w') as f:
        f.write('''
        manifest:
          projects: []
          self:
           import: west.yml
        ''')

    with pytest.raises(_ManifestImportDepth):
        MF()

#########################################
# Manifest project group: basic tests
#
# Additional groups of tests follow in later sections.

def test_no_groups_and_import():
    def importer(*args, **kwargs):
        raise RuntimeError("this shouldn't be called")

    with pytest.raises(MalformedManifest) as e:
        Manifest.from_data('''
        manifest:
          projects:
          - name: p
            url: u
            groups:
            - g
            import: True
        ''',
                           importer=importer)

    assert '"groups" cannot be combined with "import"' in str(e.value)

def test_invalid_groups():
    # Invalid group values must be rejected.

    def check(fmt, arg, err_must_contain):
        with pytest.raises(MalformedManifest) as e:
            M(fmt.format(arg))
        assert err_must_contain in str(e.value)

    fmt = '''
    projects:
    - name: p
      url: u
      groups:
      - {}
    '''

    check(fmt, '""', 'invalid group ""')
    check(fmt, 'white space', 'invalid group "white space"')
    check(fmt, 'no,commas', 'invalid group "no,commas"')
    check(fmt, 'no:colons', 'invalid group "no:colons"')
    check(fmt, '-noleadingdash', 'invalid group "-noleadingdash"')
    check(fmt, '+noleadingplus', 'invalid group "+noleadingplus"')

    assert not is_group('')
    assert not is_group('white space')
    assert not is_group('no,commas')
    assert not is_group('no:colons')
    assert not is_group('-noleadingdash')
    assert not is_group('+noleadingplus')

    fmt_scalar_project = '''
    projects:
    - name: p
      url: u
      groups: {}
    '''

    fmt_scalar_group_filter = '''
    projects: []
    group-filter: {}
    '''

    # These come from pykwalify itself.
    for fmt in [fmt_scalar_project, fmt_scalar_group_filter]:
        check(fmt, 'hello', 'is not a list')
        check(fmt, 3, 'is not a list')
        check(fmt, 3.14, 'is not a list')

def test_groups():
    # Basic test for valid project groups, which makes sure non-string
    # types are coerced to strings, and a missing 'groups' results
    # in an empty list as a Project object.

    fmt = '''
    projects:
    - name: p
      url: u
      {}
    '''

    def p(arg):
        return M(fmt.format(arg)).get_projects(['p'])[0]

    assert (p('groups: [1,"hello-world",3.14]').groups ==
            ['1', 'hello-world', '3.14'])
    assert p('groups: []').groups == []
    assert p('').groups == []

    assert is_group(1)
    assert is_group('hello-world')
    assert is_group('hello+world')
    assert is_group(3.14)

def test_invalid_manifest_group_filters():
    # Test cases for invalid "manifest: group-filter:" lists.

    def check(fmt, arg, err_must_contain):
        with pytest.raises(MalformedManifest) as e:
            M(fmt.format(arg))
        assert err_must_contain in str(e.value)

    fmt = '''
    projects: []
    group-filter:
    - {}
    '''

    check(fmt, 'white space', 'contains invalid item "white space"')
    check(fmt, 'no,commas', 'contains invalid item "no,commas"')
    check(fmt, 'no:colons', 'contains invalid item "no:colons"')
    # leading dashes are okay here!

    def check2(group_filter, err_must_contain):
        data = {'manifest': {'projects': [], 'group-filter': group_filter}}
        with pytest.raises(MalformedManifest) as e:
            Manifest.from_data(data)
        assert err_must_contain in "\n".join(e.value.args)

    check2([], 'may not be empty')
    check2('hello', 'not a list')
    check2(3, 'not a list')
    check2(3.14, 'not a list')

def test_is_active():
    # Checks for the results of the 'groups' and 'group-filter' fields on
    # Manifest.is_active(project).

    def manifest(group_filter):
        data = f"""
        defaults:
          remote: r
        remotes:
          - name: r
            url-base: u
        projects:
          - name: p1
            groups:
              - ga
          - name: p2
            groups:
              - ga
              - gb
          - name: p3
        {group_filter}
        """

        return M(data)

    def check(expected, group_filter, extra_filter=None):
        # Checks that the 'expected' tuple matches the is_active() value
        # for the p1, p2, and p3 projects in the above manifest.
        #
        # 'group_filter' is passed to the above manifest() helper.
        #
        # 'extra_filter' is an optional additional group filter, for
        # testing command line additions or for faking out config file
        # changes.

        m = manifest(group_filter)
        assert tuple(m.is_active(p, extra_filter=extra_filter)
                     for p in m.get_projects(['p1', 'p2', 'p3'])) == expected

    check((True, True, True), '')
    check((True, True, True), 'group-filter: [+ga]')
    check((False, True, True), 'group-filter: [-ga]')
    check((True, True, True), 'group-filter: [-gb]',
          extra_filter=['+ga'])
    check((True, True, True), 'group-filter: [-gb]',
          extra_filter=['+gb'])
    check((True, True, True), 'group-filter: [-ga]',
          extra_filter=['+ga'])
    check((False, True, True), 'group-filter: [-ga]',
          extra_filter=['+ga', '-ga'])
    check((True, True, True), 'group-filter: [-ga]',
          extra_filter=['+ga', '-gb'])
    check((False, False, True), 'group-filter: [-ga]',
          extra_filter=['-gb'])

#########################################
# Manifest group-filter + import tests
#
# In schema version 0.9, "manifest: group-filter:" values -- and
# therefore Manifest.group_filter values -- are *NOT* affected
# by manifest imports. Only the top level manifest group-filter has
# any effect.
#
# Shortly after the release, we ran into use cases that made it clear
# this was a mistake.
#
# This behavior means that people who import a manifest with projects
# that are inactive by default need to copy/paste the group-filter
# value if they want that same default. That kind of leaky filter is
# of no use to people who want to build on top of the default projects
# list without knowing the details, especially across multiple
# versions when the set of defaults may change.
#
# Schema version 0.10 will reverse that behavior: manifests which
# request schema version 0.10 will get Manifest.group_filter
# values that *ARE* affected by imported manifests, by
# prepending these values in import order.
#
# For compatibility, manifests which explicitly request a 0.9 schema
# version will get the old behavior. However, we'll also release a
# west 0.9.1 which will warn about a missing schema-version in the top
# level manifest if any manifest in the import hierarchy has a
# 'group-filter:' set, and encourage an upgrade to 0.10.
#
# Hopefully those combined will allow us to phase out any use of 0.9.x
# as soon as we can.
#
# Importantly, manifests which do not make an explicit version
# declaration will get the 0.10 behavior starting in 0.10.
#
# ***  This does mean that running 'west update'    ***
# ***  can produce different results in west 0.9.x  ***
# ***  and west 0.10.x.                             ***
#
# That is unfortunate, but we're going to release 0.10 as quickly as
# we can after 0.9, so this window will be brief.

def test_group_filter_project_import(manifest_repo):
    # Test cases for "manifest: group-filter:" across a project import.

    project = manifest_repo.topdir / 'project'
    create_repo(project)
    create_branch(project, 'manifest-rev', checkout=True)

    def project_import_helper(manifest_version_line, expected_group_filter):
        add_commit(project, 'project.yml',
                   files={
                       'project.yml':
                       '''
                       manifest:
                          group-filter: [-foo]
                       '''})

        with open(manifest_repo / 'west.yml', 'w') as f:
            f.write(f'''
            manifest:
              {manifest_version_line}
              projects:
                - name: project
                  url: ignore
                  revision: {rev_parse(project, "HEAD")}
                  import: project.yml
            ''')

        manifest = MF()
        assert manifest.group_filter == expected_group_filter

    project_import_helper('version: "0.10"', ['-foo'])
    project_import_helper('', ['-foo'])
    project_import_helper('version: 0.9', [])

def test_group_filter_self_import(manifest_repo):
    # Test cases for "manifest: group-filter:" across a self import.

    def self_import_helper(manifest_version_line, expected_group_filter):
        with open(manifest_repo / 'submanifest.yml', 'w') as f:
            f.write('''
            manifest:
              group-filter: [+foo]
            ''')

        with open(manifest_repo / 'west.yml', 'w') as f:
            f.write(f'''
            manifest:
              {manifest_version_line}
              group-filter: [-foo]
              self:
                import: submanifest.yml
            ''')

        manifest = MF()
        assert manifest.group_filter == expected_group_filter

    self_import_helper('version: "0.10"', [])
    self_import_helper('', [])
    self_import_helper('version: 0.9', ['-foo'])

def test_group_filter_imports(manifest_repo):
    # More complex test that ensures group filters are imported correctly:
    #
    #   - imports from self have highest precedence
    #   - the top level manifest comes next
    #   - imports from projects have lowest precedence
    #   - the resulting Manifest.group_filter is simplified appropriately
    #   - requesting the old 0.9 semantics gives them to you
    #   - requesting 0.9 raises warnings when group-filter is used

    topdir = manifest_repo.topdir
    imported_fmt = textwrap.dedent('''\
    manifest:
      group-filter: {}
    ''')
    main_fmt = textwrap.dedent('''\
    manifest:
      {}

      group-filter: [+ga,-gc]

      projects:
        - name: project1
          revision: {}
          import: true
        - name: project2
          revision: {}
          import: true

      self:
        import: self-import.yml

      defaults:
        remote: foo
      remotes:
        - name: foo
          url-base: url-base
    ''')

    def setup_self(file, group_filter):
        with open(manifest_repo / file, 'w') as f:
            f.write(imported_fmt.format(group_filter))

    def setup_project(name, group_filter):
        project = topdir / name
        create_repo(project)
        create_branch(project, 'manifest-rev', checkout=True)
        add_commit(project, 'setup commit',
                   files={'west.yml': imported_fmt.format(group_filter)})
        return rev_parse(project, 'HEAD')

    setup_self('self-import.yml', '[-ga,-gb]')

    sha1 = setup_project('project1', '[-gw,-gw,+gx,-gy]')
    sha2 = setup_project('project2', '[+gy,+gy,-gz]')

    v0_9_expected = ['+ga', '-gc']
    v0_10_expected = ['-ga', '-gb', '-gc', '-gw', '-gy', '-gz']

    #
    # Basic tests of the above setup.
    #

    # No explicitly requested schema version -> v0.10 semantics.
    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write(main_fmt.format('', sha1, sha2))
    m = Manifest.from_file()
    assert sorted(m.group_filter) == v0_10_expected
    assert not hasattr(m, '_legacy_group_filter_warned')

    # Schema version 0.10 -> v0.10 semantics.
    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write(main_fmt.format('version: "0.10"', sha1, sha2))
    m = Manifest.from_file()
    assert sorted(m.group_filter) == v0_10_expected
    assert not hasattr(m, '_legacy_group_filter_warned')

    # Schema version 0.9 -> v0.9 semantics
    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write(main_fmt.format('version: 0.9', sha1, sha2))
    m = Manifest.from_file()
    assert m.group_filter == v0_9_expected
    assert hasattr(m, '_legacy_group_filter_warned')

    #
    # Additional tests for v0.9 related warnings.
    #

    # Schema version 0.9 and no group-filter is used: no warning.
    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write(textwrap.dedent(
            '''\
            manifest:
              version: 0.9
            '''))
    m = Manifest.from_file()
    assert m.group_filter == []
    assert not hasattr(m, '_legacy_group_filter_warned')

    # Schema version 0.9, group-filter is used, no imports: still a warning.
    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write(textwrap.dedent(
            '''\
            manifest:
              version: 0.9
              group-filter: [-ga]
            '''))
    m = Manifest.from_file()
    assert m.group_filter == ['-ga']
    assert hasattr(m, '_legacy_group_filter_warned')

    # Schema version 0.9, group-filter is used by an import: warning.
    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write(textwrap.dedent(
            '''\
            manifest:
              version: 0.9
              projects:
                - name: project1
                  revision: {}
                  url: ignored
                  import: true
            '''.format(sha1)))
    m = Manifest.from_file()
    assert m.group_filter == []
    assert hasattr(m, '_legacy_group_filter_warned')


def test_submodule_manifest():
    m = M('''\
    projects:
    - name: project1
      url: url
    - name: project2
      url: url
      submodules: true
    - name: project3
      url: url
      submodules:
      - path: path
    - name: project4
      url: url
      submodules:
      - path: path
        name: subproject1
    - name: project5
      url: url
      submodules:
      - path: path
        name: subproject1
      - path: path
        name: subproject2
    - name: project6
      url: url
      submodules: false
    ''').as_dict()['manifest']

    mp = m['projects'][0]
    assert 'submodules' not in mp

    mp = m['projects'][1]
    assert 'submodules' in mp
    assert isinstance(mp['submodules'], bool)
    assert mp['submodules']

    mp = m['projects'][2]
    assert isinstance(mp['submodules'], list)
    assert len(mp['submodules']) == 1
    assert 'path' in mp['submodules'][0]
    assert mp['submodules'][0]['path'] == 'path'
    assert 'name' not in mp['submodules'][0]

    mp = m['projects'][3]
    assert isinstance(mp['submodules'], list)
    assert len(mp['submodules']) == 1
    assert 'path' in mp['submodules'][0]
    assert mp['submodules'][0]['path'] == 'path'
    assert 'name' in mp['submodules'][0]
    assert mp['submodules'][0]['name'] == 'subproject1'

    mp = m['projects'][4]
    assert isinstance(mp['submodules'], list)
    assert len(mp['submodules']) == 2
    assert 'path' in mp['submodules'][0]
    assert mp['submodules'][0]['path'] == 'path'
    assert 'name' in mp['submodules'][0]
    assert mp['submodules'][0]['name'] == 'subproject1'
    assert 'path' in mp['submodules'][1]
    assert mp['submodules'][1]['path'] == 'path'
    assert 'name' in mp['submodules'][1]
    assert mp['submodules'][1]['name'] == 'subproject2'

    mp = m['projects'][5]
    assert 'submodules' not in mp


#########################################
# Various invalid manifests

# Invalid manifests should raise MalformedManifest.
@pytest.mark.parametrize('invalid',
                         glob(os.path.join(THIS_DIRECTORY, 'manifests',
                                           'invalid_*.yml')))
def test_invalid(invalid):
    with open(invalid, 'r') as f:
        data = yaml.safe_load(f.read())

    with pytest.raises(MalformedManifest):
        Manifest.from_data(source_data=data)
