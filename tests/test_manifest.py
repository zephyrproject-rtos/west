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
import os
from pathlib import PurePath
import platform
import subprocess
from unittest.mock import patch

import pytest
import yaml

from west.manifest import Manifest, Project, ManifestProject, \
    MalformedManifest, ManifestVersionError, ManifestImportFailed, \
    manifest_path, ImportFlag, validate, MANIFEST_PROJECT_INDEX

from conftest import create_repo, checkout_branch, \
    create_branch, add_commit, GIT

FPI = ImportFlag.FORCE_PROJECTS  # to force project imports to use the callback

if platform.system() == 'Windows':
    TOPDIR = 'C:\\topdir'
    TOPDIR_POSIX = 'C:/topdir'
else:
    TOPDIR = '/topdir'
    TOPDIR_POSIX = TOPDIR

# Keep these two in sync.
FS_TOPDIR_CONTENT_STR = '''\
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
FS_TOPDIR_CONTENT_DICT = {'manifest':
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
                           'self': {}}}

THIS_DIRECTORY = os.path.dirname(__file__)

@pytest.fixture
def fs_topdir(tmpdir):
    # This fixture creates a skeletal west workspace in a temporary
    # directory on the file system, and changes directory there.
    #
    # If you use this fixture, you can create
    # './mp/west.yml', then run tests using its contents using
    # Manifest.from_file(), etc.

    # Create the topdir
    topdir = tmpdir.join('topdir')
    topdir.mkdir()

    # Create the manifest repository directory and skeleton config.
    topdir.join('mp').mkdir()
    topdir.join('.west').mkdir()
    topdir.join('.west', 'config').write('[manifest]\n'
                                         'path = mp\n')

    # Switch to the top-level west workspace directory,
    # and give it to the test case.
    topdir.chdir()
    return topdir

@pytest.fixture
def fs_topdir_content(fs_topdir):
    with open(fs_topdir / 'mp' / 'west.yml', 'w') as f:
        f.write(FS_TOPDIR_CONTENT_STR)

def check_proj_consistency(actual, expected):
    # Check equality of all project fields (projects themselves are
    # not comparable), with extra semantic consistency checking
    # for paths.
    assert actual.name == expected.name

    assert actual.path == expected.path
    if actual.topdir is None or expected.topdir is None:
        assert actual.topdir is None and expected.topdir is None
        assert actual.abspath is None and expected.abspath is None
        assert actual.posixpath is None and expected.posixpath is None
    else:
        assert actual.topdir and actual.abspath and actual.posixpath
        assert expected.topdir and expected.abspath and expected.posixpath
        a_top, e_top = PurePath(actual.topdir), PurePath(expected.topdir)
        a_abs, e_abs = PurePath(actual.abspath), PurePath(expected.abspath)
        a_psx, e_psx = PurePath(actual.posixpath), PurePath(expected.posixpath)
        assert a_top.is_absolute()
        assert e_top.is_absolute()
        assert a_abs.is_absolute()
        assert e_abs.is_absolute()
        assert a_psx.is_absolute()
        assert e_psx.is_absolute()
        assert a_top == e_top
        assert a_abs == e_abs
        assert a_psx == e_psx

    assert actual.url == expected.url
    assert actual.clone_depth == expected.clone_depth
    assert actual.revision == expected.revision
    assert actual.west_commands == expected.west_commands

def nodrive(path):
    return os.path.splitdrive(path)[1]

def M(content, **kwargs):
    # A convenience to save typing
    return Manifest.from_data('manifest:\n' + content, **kwargs)

def MF(**kwargs):
    # A convenience to save typing
    return Manifest.from_file(**kwargs)

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
    assert p.west_commands is None
    assert p.topdir is None

    p = Project('p', 'some-url', clone_depth=4, west_commands='foo',
                topdir=TOPDIR)
    assert p.clone_depth == 4
    assert p.west_commands == 'foo'
    assert p.topdir == TOPDIR
    assert p.abspath == os.path.join(TOPDIR, 'p')
    assert p.posixpath == TOPDIR_POSIX + '/p'

def test_manifest_from_data():
    # We can load manifest data as a dict or a string.

    manifest = Manifest.from_data('''\
    manifest:
      projects:
        - name: foo
          url: https://foo.com
    ''')
    assert manifest.projects[-1].name == 'foo'

    manifest = Manifest.from_data({'manifest':
                                   {'projects':
                                    [{'name': 'foo',
                                      'url': 'https:foo.com'}]}})
    assert manifest.projects[-1].name == 'foo'

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

    assert validate({'manifest':
                     {'projects':
                      [{'name': 'p',
                        'url': 'u'}]}}) is None

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
    ''') is None

def test_not_both_args():
    with pytest.raises(ValueError) as e:
        Manifest(source_file='x', source_data='y')
    assert 'both source_file and source_data were given' in str(e.value)

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
    # Absolute path attributes should work as documented.

    ps = M('''\
    remotes:
    - name: testremote
      url-base: https://example.com
    projects:
    - name: testproject
      remote: testremote
      path: sub/directory
    ''', topdir='/west_top').projects
    assert ps[1].path == 'sub/directory'
    assert nodrive(ps[1].posixpath) == '/west_top/sub/directory'

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

    # Same again, but with topdir.
    if platform.system() == 'Windows':
        topdir = 'C:/'
    else:
        topdir = '/'
    m = M(content, topdir=topdir)
    expected1 = Project('testproject_v1', 'https://url1.com/testproject',
                        revision='v1.0', topdir=topdir)
    expected2 = Project('testproject_v2', 'https://url1.com/testproject',
                        revision='v2.0', topdir=topdir)
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
    assert m.projects[1].west_commands == 'some-path/west-commands.yml'

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

#########################################
# Tests for the manifest repository

def test_manifest_project():
    # Basic test that the manifest repository, when represented as a project,
    # has attributes which make sense.

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
    assert mp.url is None
    assert mp.revision == 'HEAD'
    assert mp.clone_depth is None

    # Case 2: path etc. are specified, but not topdir
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
    assert mp.west_commands == 'cmds.yml'
    assert mp.topdir is None
    assert mp.abspath is None
    assert mp.posixpath is None
    assert mp.url is None
    assert mp.revision == 'HEAD'
    assert mp.clone_depth is None

    # Case 3: path etc. and topdir are all specified
    m = M('''\
    projects:
    - name: name
      url: url
    self:
      path: my-path
      west-commands: cmds.yml
    ''',
          manifest_path='should-be-ignored',
          topdir='/west_top')
    mp = m.projects[0]
    assert mp.name == 'manifest'
    assert mp.path == 'my-path'
    assert mp.topdir is not None
    assert PurePath(nodrive(mp.abspath)) == PurePath('/west_top/my-path')
    assert mp.posixpath is not None
    assert mp.west_commands == 'cmds.yml'
    assert mp.url is None
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

    # Absent a path and a path hint, the path attribute is None.
    assert M('''\
    projects:
    - name: p
      url: u
    ''').projects[0].path is None

    # With a path hint, the path attribute is not None.
    assert M('''\
    projects:
    - name: p
      url: u
    ''', manifest_path='mpath').projects[0].path == 'mpath'

#########################################
# File system tests
#
# Parsing manifests from data is the base case that everything else
# reduces to, but parsing may also be done from files on the file
# system, or "as if" it were done from files on the file system.

def test_from_data_with_topdir(tmpdir):
    # If you specify the topdir along with some source data, you will
    # get absolute paths, even if it doesn't exist.
    #
    # This is true of both projects and the manifest itself.

    topdir = str(tmpdir)

    # Case 1: manifest has no path (projects always have paths)
    m = M('''\
    projects:
    - name: my-cool-project
      url: from-manifest-dir
    ''', topdir=topdir)
    assert m.topdir == topdir
    mproj = m.projects[0]
    assert mproj.topdir == topdir
    assert mproj.path is None
    p1 = m.projects[1]
    assert PurePath(p1.topdir) == PurePath(topdir)
    assert PurePath(p1.abspath) == PurePath(str(tmpdir / 'my-cool-project'))

    # Case 2: manifest path is provided programmatically
    m = M('''\
    projects:
    - name: my-cool-project
      url: from-manifest-dir
    ''', manifest_path='from-api', topdir=topdir)
    assert m.topdir == topdir
    mproj = m.projects[0]
    assert PurePath(mproj.topdir).is_absolute()
    assert PurePath(mproj.topdir) == PurePath(topdir)
    assert mproj.path == 'from-api'
    assert PurePath(mproj.abspath).is_absolute()
    assert PurePath(mproj.abspath) == PurePath(str(tmpdir / 'from-api'))
    p1 = m.projects[1]
    assert PurePath(p1.topdir) == PurePath(topdir)
    assert PurePath(p1.abspath) == PurePath(str(tmpdir / 'my-cool-project'))

    # Case 3: manifest has a self path. This must override the
    # manifest_path kwarg.
    m = M('''\
    projects:
    - name: my-cool-project
      url: from-manifest-dir
    self:
        path: from-content
    ''', manifest_path='should-be-ignored', topdir=topdir)
    assert m.topdir == topdir
    mproj = m.projects[0]
    assert mproj.path == 'from-content'
    assert PurePath(mproj.abspath) == PurePath(str(tmpdir / 'from-content'))
    p1 = m.projects[1]
    assert p1.path == 'my-cool-project'
    assert PurePath(p1.abspath) == PurePath(str(tmpdir / 'my-cool-project'))

    # Case 4: project has a path.
    m = M('''\
    projects:
    - name: my-cool-project
      url: from-manifest-dir
      path: project-path
    self:
        path: manifest-path
    ''', manifest_path='should-be-ignored', topdir=topdir)
    assert m.topdir == topdir
    mproj = m.projects[0]
    assert mproj.path == 'manifest-path'
    assert PurePath(mproj.abspath) == PurePath(str(tmpdir / 'manifest-path'))
    p1 = m.projects[1]
    assert p1.path == 'project-path'
    assert PurePath(p1.abspath) == PurePath(str(tmpdir / 'project-path'))

def test_manifest_path_not_found(fs_topdir):
    # Make sure manifest_path() raises FileNotFoundError if the
    # manifest file specified in .west/config doesn't exist.
    # Here, we rely on fs_topdir not actually creating the file.

    with pytest.raises(FileNotFoundError) as e:
        manifest_path()
    assert e.value.filename == fs_topdir / 'mp' / 'west.yml'

def test_manifest_path_conflicts():
    # Project path conflicts with the manifest path are errors.
    # This is true for both implicit and explicit paths.

    with pytest.raises(MalformedManifest) as e:
        M('''\
        projects:
        - name: p
          url: u
        self:
          path: p
        ''')
    assert 'p path "p" is taken by the manifest' in str(e.value)

    with pytest.raises(MalformedManifest) as e:
        M('''\
        projects:
        - name: n
          url: u
          path: p
        self:
          path: p
        ''')
    assert 'n path "p" is taken by the manifest' in str(e.value)

def test_fs_topdir(fs_topdir):
    # The API should be able to find a manifest file based on the file
    # system and west configuration. The resulting topdir and abspath
    # attributes should work as specified.

    content = '''\
    manifest:
      projects:
      - name: project-from-manifest-dir
        url: from-manifest-dir
    '''
    west_yml = str(fs_topdir / 'mp' / 'west.yml')
    with open(west_yml, 'w') as f:
        f.write(content)

    # manifest_path() should discover west_yml.
    assert manifest_path() == west_yml

    # Manifest.from_file() should as well.
    # The project hierarchy should be rooted in the topdir.
    manifest = Manifest.from_file()
    assert manifest.topdir is not None
    assert manifest.topdir == str(fs_topdir)
    assert len(manifest.projects) == 2
    p = manifest.projects[1]
    assert p.name == 'project-from-manifest-dir'
    assert p.url == 'from-manifest-dir'
    assert p.topdir is not None
    assert PurePath(p.topdir) == PurePath(str(fs_topdir))

    manifest = MF(topdir=fs_topdir)
    assert len(manifest.get_projects(['project-from-manifest-dir'],
                                     allow_paths=False)) == 1

def test_fs_topdir_different_source(fs_topdir):
    # The API should be able to parse multiple manifest files inside a
    # single topdir. The project hierarchies should always be rooted
    # in that same topdir. The results of parsing the two separate
    # files are independent of one another.

    topdir = str(fs_topdir)
    west_yml_content = '''\
    manifest:
      projects:
      - name: project-1
        url: url-1
      - name: project-2
        url: url-2
    '''
    west_yml = str(fs_topdir / 'mp' / 'west.yml')
    with open(west_yml, 'w') as f:
        f.write(west_yml_content)

    another_yml_content = '''\
    manifest:
      projects:
      - name: another-1
        url: another-url-1
      - name: another-2
        url: another-url-2
        path:  another/path
    '''
    another_yml = str(fs_topdir / 'another.yml')
    with open(another_yml, 'w') as f:
        f.write(another_yml_content)

    another_yml_with_path_content = '''\
    manifest:
      projects:
      - name: foo
        url: bar
      self:
        path: with-path
    '''
    another_yml_with_path = str(fs_topdir / 'another-with-path.yml')
    with open(another_yml_with_path, 'w') as f:
        f.write(another_yml_with_path_content)

    # manifest_path() should discover west_yml.
    assert manifest_path() == west_yml

    # Manifest.from_file() should discover west.yml, and
    # the project hierarchy should be rooted at topdir.
    manifest = Manifest.from_file()
    assert PurePath(manifest.topdir) == PurePath(topdir)
    assert len(manifest.projects) == 3
    assert manifest.projects[1].name == 'project-1'
    assert manifest.projects[2].name == 'project-2'

    # Manifest.from_file() should be also usable with another_yml.
    # The project hierarchy in its return value should still be rooted
    # in the topdir, but the resulting ManifestProject does not have a
    # path, because it's not set in the file, and we're explicitly not
    # comparing its path to manifest.path.
    #
    # However, the project hierarchy *must* be rooted at topdir.
    manifest = Manifest.from_file(source_file=another_yml)
    assert len(manifest.projects) == 3
    assert manifest.topdir is not None
    assert PurePath(manifest.topdir) == PurePath(topdir)
    mproj = manifest.projects[0]
    assert mproj.path is None
    assert mproj.abspath is None
    assert mproj.posixpath is None
    p1 = manifest.projects[1]
    assert p1.name == 'another-1'
    assert p1.url == 'another-url-1'
    assert p1.topdir == topdir
    assert PurePath(p1.abspath) == PurePath(str(fs_topdir / 'another-1'))
    p2 = manifest.projects[2]
    assert p2.name == 'another-2'
    assert p2.url == 'another-url-2'
    assert p2.topdir == topdir
    assert PurePath(p2.abspath) == PurePath(str(fs_topdir / 'another' /
                                                'path'))

    # On the other hand, if the manifest yaml file does specify its
    # path, the ManifestProject must also be rooted at topdir.
    manifest = Manifest.from_file(source_file=another_yml_with_path)
    mproj = manifest.projects[0]
    assert mproj.path == 'with-path'
    assert PurePath(mproj.topdir) == PurePath(topdir)
    assert PurePath(mproj.abspath) == PurePath(str(fs_topdir / 'with-path'))

def test_fs_topdir_freestanding_manifest(tmpdir, fs_topdir):
    # The API should be able to parse a random manifest file
    # in a location that has nothing to do with the current topdir.
    #
    # The resulting Manifest will have projects rooted at topdir.
    #
    # If it has a self path, that's its path, and the ManifestProject
    # is rooted at topdir. Otherwise, its path and abspath are None.
    topdir = str(fs_topdir)

    # Case 1: self path is present. ManifestProject is rooted
    # within the same topdir as the projects.
    content = '''\
    manifest:
      projects:
      - name: name
        url: url
      self:
        path: my-path
    '''
    yml = str(tmpdir / 'random.yml')
    with open(yml, 'w') as f:
        f.write(content)
    manifest = Manifest.from_file(source_file=yml, topdir=topdir)
    assert PurePath(manifest.topdir) == PurePath(topdir)
    mproj = manifest.projects[0]
    assert mproj.path == 'my-path'
    assert PurePath(mproj.abspath) == PurePath(str(fs_topdir / 'my-path'))

    # Case 1: self path is missing
    content = '''\
    manifest:
      projects:
      - name: name
        url: url
    '''
    yml = str(tmpdir / 'random.yml')
    with open(yml, 'w') as f:
        f.write(content)
    manifest = Manifest.from_file(source_file=yml, topdir=topdir)
    assert PurePath(manifest.topdir) == PurePath(topdir)
    mproj = manifest.projects[0]
    assert mproj.path is None
    assert mproj.abspath is None

def test_fs_topdir_no_manifest(fs_topdir):
    # Make sure we get expected failure using Manifest.from_file()
    # with the topdir kwarg when no west.yml exists.

    with pytest.raises(ValueError) as e:
        MF(topdir=fs_topdir / 'mp')
    assert 'is not a west workspace root' in str(e.value)
    assert f'but {fs_topdir} is' in str(e.value)

def test_from_bad_topdir(tmpdir):
    # If we give a bad temporary directory that isn't a workspace
    # root, that should also fail.

    with pytest.raises(ValueError) as e:
        MF(topdir=tmpdir)
    assert 'is not a west workspace root' in str(e.value)

#########################################
# Miscellaneous tests

def test_ignore_west_section():
    # We no longer validate the west section, so things that would
    # once have been schema errors shouldn't be anymore. Projects
    # should still work as expected regardless of what's in there.

    content_wrong_west = '''\
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
    '''
    # Parsing manifest only, no exception raised
    manifest = Manifest.from_data(yaml.safe_load(content_wrong_west),
                                  topdir='/west_top')
    p1 = manifest.projects[1]
    assert PurePath(p1.path) == PurePath('sub', 'directory')
    assert PurePath(nodrive(p1.abspath)) == PurePath('/west_top/sub/directory')

def test_get_projects(fs_topdir):
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
    with open(fs_topdir / 'mp' / 'west.yml', 'w') as f:
        f.write(content)

    # Asking for an uncloned project should fail if only_cloned=False.
    # The ValueError args are (unknown, uncloned).
    manifest = MF(topdir=fs_topdir)
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
    assert projects[0].abspath == fs_topdir / 'mp'

    # No project_ids means "all projects".
    projects = manifest.get_projects([])
    assert len(projects) == 2
    assert projects[0].name == 'manifest'
    assert projects[1].name == 'foo'
    with pytest.raises(ValueError) as e:
        projects = manifest.get_projects([], only_cloned=True)
    unknown, uncloned = e.value.args
    assert len(uncloned) == 2   # subtle: the manifest repository isn't cloned!
    assert uncloned[0].name == 'manifest'
    assert uncloned[1].name == 'foo'

def test_as_dict_and_yaml(fs_topdir_content):
    # coverage for as_dict, as_frozen_dict, as_yaml, as_frozen_yaml.

    fake_sha = 'the-sha'
    frozen_expected = deepcopy(FS_TOPDIR_CONTENT_DICT)
    for p in frozen_expected['manifest']['projects']:
        p['revision'] = fake_sha

    manifest = MF()

    # We can always call as_dict() and as_yaml(), regardless of what's
    # cloned.

    as_dict = manifest.as_dict()
    yaml_roundtrip = yaml.safe_load(manifest.as_yaml())
    assert as_dict == FS_TOPDIR_CONTENT_DICT
    assert yaml_roundtrip == FS_TOPDIR_CONTENT_DICT

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

@pytest.mark.parametrize('ver', ['0.6.99'])
def test_version_check_success(ver):
    # Test that version checking succeeds when it should.

    fmt = '''\
    manifest:
      version: {}
      projects:
      - name: foo
        url: https://foo.com
    '''
    manifest = Manifest.from_data(fmt.format(ver))
    assert manifest.projects[-1].name == 'foo'
    manifest = Manifest.from_data(fmt.format('"' + ver + '"'))
    assert manifest.projects[-1].name == 'foo'

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
    actual = Manifest.from_data(content, manifest_path='my-repo',
                                topdir=TOPDIR, importer=importer,
                                import_flags=FPI).projects

    expected = [
        ManifestProject(path='my-repo', topdir=TOPDIR),
        Project('upstream', 'upstream.com/upstream', revision='refs/tags/v1.0',
                path='upstream', topdir=TOPDIR),
        Project('hal_nordic', 'upstream.com/hal_nordic',
                revision='hal_nordic-upstream-rev',
                path='modules/hal/nordic', topdir=TOPDIR),
        Project('segger', 'upstream.com/segger',
                revision='segger-upstream-rev',
                path='modules/debug/segger', topdir=TOPDIR)]

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
                                manifest_path='my-repo',
                                topdir=TOPDIR, importer=importer,
                                import_flags=FPI).projects

    expected = [
        ManifestProject(path='my-repo', topdir=TOPDIR),
        Project('hal_nordic', 'downstream.com/hal_nordic',
                revision='my-branch', path='modules/hal/nordic',
                topdir=TOPDIR),
        Project('my-proj', 'downstream.com/my-proj', revision='master',
                path='my-proj', topdir=TOPDIR),
        Project('upstream', 'upstream.com/upstream', revision='refs/tags/v1.0',
                path='upstream', topdir=TOPDIR),
        Project('segger', 'upstream.com/segger',
                revision='segger-upstream-rev',
                path='modules/debug/segger', topdir=TOPDIR)]

    for a, e in zip(actual, expected):
        check_proj_consistency(a, e)

def test_import_project_list(fs_topdir):
    # We should be able to import a list of files from a project at a
    # revision. The files should come from git, not the file system.

    manifest_repo = fs_topdir / 'mp'
    create_repo(manifest_repo)
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

    p1 = fs_topdir / 'p1'
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
    assert (p1 / 'm1.yml').check(file=1)
    assert (p1 / 'm2.yml').check(file=1)
    checkout_branch(p1, 'master')
    assert (p1 / 'm1.yml').check(file=0, dir=0)
    assert (p1 / 'm2.yml').check(file=0, dir=0)

    actual = MF().projects
    expected = [ManifestProject(path='mp', topdir=fs_topdir),
                Project('p1', 'p1-url', topdir=fs_topdir),
                Project('p2', 'p2-url', topdir=fs_topdir),
                Project('p3', 'p3-url', topdir=fs_topdir)]

    for a, e in zip(actual, expected):
        check_proj_consistency(a, e)

def test_import_project_directory(fs_topdir):
    # We should be able to import manifest files in a directory from a
    # revision. The files should come from git, not the file system.

    manifest_repo = fs_topdir / 'mp'
    create_repo(manifest_repo)
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

    p1 = fs_topdir / 'p1'
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
    assert (p1 / 'd').check(dir=1)
    assert (p1 / 'd' / 'ignore-me.txt').check(file=1)
    assert (p1 / 'd' / 'm1.yml').check(file=1)
    assert (p1 / 'd' / 'm2.yml').check(file=1)
    checkout_branch(p1, 'master')
    assert (p1 / 'd').check(file=0, dir=0)

    actual = MF().projects
    expected = [ManifestProject(path='mp', topdir=fs_topdir),
                Project('p1', 'p1-url', topdir=fs_topdir),
                Project('p2', 'p2-url', topdir=fs_topdir),
                Project('p3', 'p3-url', topdir=fs_topdir)]

    for a, e in zip(actual, expected):
        check_proj_consistency(a, e)

def test_import_project_err_malformed(fs_topdir):
    # Checks for erroneous or malformed imports from projects.

    manifest_repo = fs_topdir / 'mp'
    create_repo(manifest_repo)
    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write('''\
        manifest:
          projects:
          - name: p
            url: p-url
            import: true
        ''')

    p = fs_topdir / 'p'
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

def test_import_project_submanifest_commands(fs_topdir):
    # If a project has no west-commands, but an imported manifest
    # inside it defines some, they should be inherited in the parent.

    manifest_repo = fs_topdir / 'mp'
    create_repo(manifest_repo)
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

    p1 = fs_topdir / 'p1'
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

def test_import_project_submanifest_commands_both(fs_topdir):
    # Like test_import_project_submanifest_commands, but making sure
    # that if multiple west-commands appear throughout the imported
    # manifests, then west_commands is a list of all of them, resolved
    # in import order.

    manifest_repo = fs_topdir / 'mp'
    create_repo(manifest_repo)
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

    p1 = fs_topdir / 'p1'
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

# A manifest repository with a subdirectory containing multiple
# additional files:
#
# split-manifest/
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


def _setup_import_self(tmpdir, manifests):
    tmpdir.chdir()
    (tmpdir / '.west').mkdir()
    manifest_repo = tmpdir / 'split-manifest'
    manifest_repo.mkdir()
    (manifest_repo / 'west.d').mkdir()
    for path, content in manifests.items():
        with open(str(manifest_repo / path), 'w') as f:
            f.write(content)

@pytest.mark.parametrize('content', _IMPORT_SELF_MANIFESTS,
                         ids=['dir', 'files'])
def test_import_self_directory(content, tmpdir):
    # Test a couple of different equivalent ways to import content
    # from the manifest repository.

    call_map = {('upstream', 'west.yml'): _UPSTREAM_WYML}

    # Create tmpdir/.west and the manifest files.
    manifests = {'west.yml': content}
    manifests.update(_IMPORT_SELF_SUBMANIFESTS)
    _setup_import_self(tmpdir, manifests)

    # Resolve the manifest. The split-manifest/west.d content comes
    # from the file system in this case.
    topdir = str(tmpdir)
    actual = Manifest.from_data(manifests['west.yml'],
                                manifest_path='split-manifest', topdir=topdir,
                                importer=make_importer(call_map),
                                import_flags=FPI).projects

    expected = [
        ManifestProject(path='split-manifest', topdir=topdir),
        # Projects from 01-libraries.yml come first.
        Project('my-1', 'downstream.com/my-lib-1', revision='my-1-rev',
                path='lib/my-1', topdir=topdir),
        Project('my-2', 'downstream.com/my-lib-2', revision='my-2-rev',
                path='lib/my-2', topdir=topdir),
        # Next, projects from 02-vendor-hals.yml.
        Project('hal_nordic', 'downstream.com/hal_nordic',
                revision='my-hal-rev', path='modules/hal/nordic',
                topdir=topdir),
        Project('hal_downstream_sauce', 'downstream.com/hal_downstream_only',
                revision='my-down-hal-rev', path='modules/hal/downstream_only',
                topdir=topdir),
        # After that, 03-applications.yml.
        Project('my-app', 'downstream.com/my-app', revision='my-app-rev',
                path='applications/my-app', topdir=topdir),
        # upstream is the only element of our projects list, so it's
        # after all the self-imports.
        Project('upstream', 'upstream.com/upstream', revision='refs/tags/v1.0',
                path='upstream', topdir=topdir),
        # Projects we imported from upstream are last. Projects
        # present upstream which we have already defined should be
        # ignored and not appear here.
        Project('segger', 'upstream.com/segger',
                revision='segger-upstream-rev',
                path='modules/debug/segger', topdir=topdir),
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

def test_import_self_err_malformed(fs_topdir):
    # Checks for erroneous or malformed imports from self.

    manifest_repo = fs_topdir / 'mp'
    create_repo(manifest_repo)

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

def test_import_self_submanifest_commands(fs_topdir):
    # If we import a sub-manifest from 'self' that has west commands
    # in its own self section, those should be treated as if they were
    # declared in the top-level self section.

    manifest_repo = fs_topdir / 'mp'
    create_repo(manifest_repo)

    top = '''\
    manifest:
      projects:
      - name: p1
        url: u1
      self:
        import: sub-manifest.yml
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
    assert mp.west_commands == 'sub-commands.yml'

def test_import_self_submanifest_commands_both(fs_topdir):
    # Like test_import_self_submanifest_commands, but making sure that
    # if multiple west-commands appear throughout the imported manifests,
    # then west_commands is a list of all of them, resolved in import order.

    manifest_repo = fs_topdir / 'mp'
    create_repo(manifest_repo)

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

    mp = MF(topdir=fs_topdir).projects[MANIFEST_PROJECT_INDEX]
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

def test_import_map_name_whitelist(fs_topdir):
    # This tests an example from the documentation which uses
    # name-whitelist.

    manifest_repo = fs_topdir / 'mp'
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

    mainline = fs_topdir / 'mainline'
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

    actual = MF().projects

    expected = M('''\
    projects:
      - name: mainline
        url: https://git.example.com/mainline/manifest
      - name: downstream-app
        url: https://git.example.com/downstream/app
      - name: lib3
        path: libraries/lib3
        url: https://git.example.com/downstream/lib3
      - name: mainline-app
        path: examples/app
        url: https://git.example.com/mainline/app
      - name: lib2
        path: libraries/lib2
        url: https://git.example.com/mainline/lib2
    ''',
                 manifest_path='mp',
                 topdir=fs_topdir).projects

    for a, e in zip(actual, expected):
        check_proj_consistency(a, e)

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
