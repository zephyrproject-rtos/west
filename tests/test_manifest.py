# Copyright 2018 Foundries.io Ltd
#
# SPDX-License-Identifier: Apache-2.0

from glob import glob
import os
from pathlib import PurePath
import platform

import pytest
import yaml

from west.manifest import Manifest, Defaults, Remote, Project, \
    ManifestProject, MalformedManifest, ManifestVersionError, \
    manifest_path

THIS_DIRECTORY = os.path.dirname(__file__)

@pytest.fixture
def fs_topdir(tmpdir):
    # This fixture creates a skeletal west installation in a temporary
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

    # Switch to the top-level West installation directory,
    # and give it to the test case.
    topdir.chdir()
    return topdir

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

def test_init_with_url():
    # Test the project constructor works as expected with a URL.

    p = Project('p', url='some-url')
    assert p.name == 'p'
    assert p.url == 'some-url'
    assert p.path == 'p'
    assert p.topdir is None
    assert p.abspath is None
    if platform.system() == 'Windows':
        posixpath = os.path.splitdrive(p.posixpath)[1]
        assert posixpath == '/west_top/p'
    assert p.clone_depth is None
    assert p.revision == 'master'
    assert p.west_commands is None

def test_init_with_url_and_topdir():
    # Test the project constructor works as expected with a URL
    # and explicit top level directory.

    p = Project('p', url='some-url', topdir='/west_top')
    assert p.name == 'p'
    assert p.url == 'some-url'
    assert p.path == 'p'
    assert p.topdir == '/west_top'
    if platform.system() == 'Windows':
        posixpath = os.path.splitdrive(p.posixpath)[1]
        assert posixpath == '/west_top/p'
    assert p.clone_depth is None
    assert p.revision == 'master'
    assert p.west_commands is None

def test_remote_url_init():
    # Projects must be initialized with a remote or a URL, but not both.
    # The resulting URLs must behave as documented.

    r1 = Remote('testremote1', 'https://example.com')
    p1 = Project('project1', remote=r1)
    assert p1.url == 'https://example.com/project1'

    p2 = Project('project2', url='https://example.com/project2')
    assert p2.url == 'https://example.com/project2'

    with pytest.raises(ValueError):
        Project('project3', remote=r1, url='not-empty')

    with pytest.raises(ValueError):
        Project('project4', remote=None, url=None)

def test_no_remote_ok():
    # remotes isn't required in a manifest if all projects are
    # specified by URL.

    content = '''\
    manifest:
      projects:
        - name: testproject
          url: https://example.com/my-project
    '''
    manifest = Manifest.from_data(yaml.safe_load(content))
    assert manifest.projects[1].url == 'https://example.com/my-project'

def test_manifest_attrs():
    # test that the manifest repository, when represented as a project,
    # has attributes which make sense.

    # Case 1: everything at defaults
    content = '''\
    manifest:
      projects:
        - name: name
          url: url
    '''
    manifest = Manifest.from_data(yaml.safe_load(content))
    mp = manifest.projects[0]
    assert mp.name == 'manifest'
    assert mp.path is None
    assert mp.topdir is None
    assert mp.abspath is None
    assert mp.posixpath is None
    assert mp.url is None
    assert mp.revision == 'HEAD'
    assert mp.clone_depth is None

    # Case 2: path etc. are specified, but not topdir
    content = '''\
    manifest:
      projects:
        - name: name
          url: url
      self:
        path: my-path
        west-commands: cmds.yml
    '''
    manifest = Manifest.from_data(yaml.safe_load(content))
    mp = manifest.projects[0]
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
    content = '''\
    manifest:
      projects:
        - name: name
          url: url
      self:
        path: my-path
        west-commands: cmds.yml
    '''
    manifest = Manifest.from_data(yaml.safe_load(content),
                                  manifest_path='should-be-ignored',
                                  topdir='/west_top')
    mp = manifest.projects[0]
    assert mp.name == 'manifest'
    assert mp.path == 'my-path'
    assert mp.topdir is not None
    assert PurePath(mp.abspath) == PurePath('/west_top/my-path')
    assert mp.posixpath is not None
    assert mp.west_commands == 'cmds.yml'
    assert mp.url is None
    assert mp.revision == 'HEAD'
    assert mp.clone_depth is None

def test_defaults_and_url():
    # an explicit URL overrides the defaults attribute.

    content = '''\
    manifest:
      defaults:
        remote: remote1
      remotes:
        - name: remote1
          url-base: https://url1.com/
      projects:
        - name: testproject
          url: https://url2.com/testproject
    '''
    manifest = Manifest.from_data(yaml.safe_load(content))
    assert manifest.projects[1].url == 'https://url2.com/testproject'

def test_repo_path():
    # a project's fetch URL may be specified by combining a remote and
    # repo-path. this overrides the default use of the project's name
    # as the repo-path.

    # default remote + repo-path
    content = '''\
    manifest:
      defaults:
        remote: remote1
      remotes:
        - name: remote1
          url-base: https://example.com
      projects:
        - name: testproject
          repo-path: some/path
    '''
    manifest = Manifest.from_data(yaml.safe_load(content))
    assert manifest.projects[1].url == 'https://example.com/some/path'

    # non-default remote + repo-path
    content = '''\
    manifest:
      defaults:
        remote: remote1
      remotes:
        - name: remote1
          url-base: https://url1.com
        - name: remote2
          url-base: https://url2.com
      projects:
        - name: testproject
          remote: remote2
          repo-path: path
    '''
    manifest = Manifest.from_data(yaml.safe_load(content))
    assert manifest.projects[1].url == 'https://url2.com/path'

    # same project checked out under two different names
    content = '''\
    manifest:
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
    manifest = Manifest.from_data(yaml.safe_load(content))
    p1, p2 = manifest.projects[1:]
    r = Remote('remote1', 'https://url1.com')
    d = Defaults(remote=r)
    assert p1.url == 'https://url1.com/testproject'
    assert p1.url == p2.url
    expected1 = Project('testproject_v1', defaults=d, path='testproject_v1',
                        revision='v1.0', remote=r, repo_path='testproject')
    expected2 = Project('testproject_v2', defaults=d, path='testproject_v2',
                        revision='v2.0', remote=r, repo_path='testproject')
    check_proj_consistency(p1, expected1)
    check_proj_consistency(p2, expected2)

def test_data_and_topdir(tmpdir):
    # If you specify the topdir along with some source data, you will
    # get absolute paths, even if it doesn't exist.

    topdir = str(tmpdir)

    # Case 1: manifest has no path (projects always have paths)
    content = '''\
    manifest:
        projects:
          - name: my-cool-project
            url: from-manifest-dir
    '''
    manifest = Manifest.from_data(source_data=yaml.safe_load(content),
                                  topdir=topdir)
    assert manifest.topdir == topdir
    mproj = manifest.projects[0]
    assert mproj.topdir == topdir
    assert mproj.path is None
    p1 = manifest.projects[1]
    assert PurePath(p1.topdir) == PurePath(topdir)
    assert PurePath(p1.abspath) == PurePath(str(tmpdir / 'my-cool-project'))

    # Case 2: manifest path is provided programmatically
    content = '''\
    manifest:
        projects:
          - name: my-cool-project
            url: from-manifest-dir
    '''
    manifest = Manifest.from_data(source_data=yaml.safe_load(content),
                                  manifest_path='from-api',
                                  topdir=topdir)
    assert manifest.topdir == topdir
    mproj = manifest.projects[0]
    assert PurePath(mproj.topdir).is_absolute()
    assert PurePath(mproj.topdir) == PurePath(topdir)
    assert mproj.path == 'from-api'
    assert PurePath(mproj.abspath).is_absolute()
    assert PurePath(mproj.abspath) == PurePath(str(tmpdir / 'from-api'))
    p1 = manifest.projects[1]
    assert PurePath(p1.topdir) == PurePath(topdir)
    assert PurePath(p1.abspath) == PurePath(str(tmpdir / 'my-cool-project'))

    # Case 3: manifest has a self path. This must override the
    # manifest_path kwarg.
    content = '''\
    manifest:
        projects:
          - name: my-cool-project
            url: from-manifest-dir
        self:
            path: from-content
    '''
    manifest = Manifest.from_data(source_data=yaml.safe_load(content),
                                  manifest_path='should-be-ignored',
                                  topdir=topdir)
    assert manifest.topdir == topdir
    mproj = manifest.projects[0]
    assert mproj.path == 'from-content'
    assert PurePath(mproj.abspath) == PurePath(str(tmpdir / 'from-content'))
    p1 = manifest.projects[1]
    assert p1.path == 'my-cool-project'
    assert PurePath(p1.abspath) == PurePath(str(tmpdir / 'my-cool-project'))

    # Case 4: project has a path.
    content = '''\
    manifest:
        projects:
          - name: my-cool-project
            url: from-manifest-dir
            path: project-path
        self:
            path: manifest-path
    '''
    manifest = Manifest.from_data(source_data=yaml.safe_load(content),
                                  manifest_path='should-be-ignored',
                                  topdir=topdir)
    assert manifest.topdir == topdir
    mproj = manifest.projects[0]
    assert mproj.path == 'manifest-path'
    assert PurePath(mproj.abspath) == PurePath(str(tmpdir / 'manifest-path'))
    p1 = manifest.projects[1]
    assert p1.path == 'project-path'
    assert PurePath(p1.abspath) == PurePath(str(tmpdir / 'project-path'))

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

def test_multiple_remotes():
    # More than one remote may be used, and one of them may be used as
    # the default.

    content = '''\
    manifest:
      defaults:
        remote: testremote2

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
        - name: testproject3
    '''
    r1 = Remote('testremote1', 'https://example1.com')
    r2 = Remote('testremote2', 'https://example2.com')

    manifest = Manifest.from_data(yaml.safe_load(content))

    expected = [Project('testproject1', revision='rev1', remote=r1),
                Project('testproject2', remote=r2),
                Project('testproject3', remote=r2)]

    # Check the projects are as expected.
    for p, e in zip(manifest.projects[1:], expected):
        check_proj_consistency(p, e)

    # Throw in an extra check that absolute paths are not available,
    # just for fun.
    assert all(p.abspath is None for p in manifest.projects)

def test_self_tag():
    # Manifests may contain a self section describing their behavior.
    # It should work with multiple projects and remotes as expected.

    content = '''\
    manifest:
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
    '''
    r1 = Remote('testremote1', 'https://example1.com')
    r2 = Remote('testremote2', 'https://example2.com')

    manifest = Manifest.from_data(yaml.safe_load(content))

    expected = [ManifestProject(path='the-manifest-path'),
                Project('testproject1', None, path='testproject1',
                        clone_depth=None, revision='rev1', remote=r1),
                Project('testproject2', None, path='testproject2',
                        clone_depth=None, revision='master', remote=r2)]

    # Check the projects are as expected.
    for p, e in zip(manifest.projects, expected):
        check_proj_consistency(p, e)

def test_default_clone_depth():
    # Defaults and clone depth should work as in this example.
    content = '''\
    manifest:
      defaults:
        remote: testremote1
        revision: defaultrev

      remotes:
        - name: testremote1
          url-base: https://example1.com
        - name: testremote2
          url-base: https://example2.com

      projects:
        - name: testproject1
        - name: testproject2
          remote: testremote2
          revision: rev
          clone-depth: 1
    '''
    r1 = Remote('testremote1', 'https://example1.com')
    r2 = Remote('testremote2', 'https://example2.com')
    d = Defaults(remote=r1, revision='defaultrev')

    manifest = Manifest.from_data(yaml.safe_load(content))

    expected = [Project('testproject1', d, path='testproject1',
                        clone_depth=None, revision=d.revision, remote=r1),
                Project('testproject2', d, path='testproject2',
                        clone_depth=1, revision='rev', remote=r2)]

    # Check that the projects are as expected.
    for p, e in zip(manifest.projects[1:], expected):
        check_proj_consistency(p, e)

def test_path():
    # Projects must be able to override their default paths.
    # Absolute paths should reflect this setting.

    content = '''\
    manifest:
      remotes:
        - name: testremote
          url-base: https://example.com
      projects:
        - name: testproject
          remote: testremote
          path: sub/directory
    '''
    manifest = Manifest.from_data(yaml.safe_load(content), topdir='/west_top')
    assert manifest.projects[1].path == 'sub' + os.path.sep + 'directory'
    assert manifest.projects[1].posixpath == '/west_top/sub/directory'


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
    assert PurePath(p1.abspath) == PurePath('/west_top/sub/directory')

def test_project_west_commands():
    # Projects may specify subdirectories containing west commands.

    content = '''\
    manifest:
      projects:
        - name: zephyr
          url: https://foo.com
          west-commands: some-path/west-commands.yml
    '''
    manifest = Manifest.from_data(yaml.safe_load(content))
    assert len(manifest.projects) == 2
    assert manifest.projects[1].west_commands == 'some-path/west-commands.yml'


def test_project_named_west():
    # A project named west is allowed now, even though it was once an error.

    content = '''\
    manifest:
      projects:
        - name: west
          url: https://foo.com
    '''
    manifest = Manifest.from_data(yaml.safe_load(content))
    assert manifest.projects[1].name == 'west'


def test_get_projects_unknown():
    # Attempting to get an unknown project is an error.
    # TODO: add more testing for get_projects().

    content = '''\
    manifest:
      projects:
        - name: foo
          url: https://foo.com
    '''
    manifest = Manifest.from_data(yaml.safe_load(content))
    with pytest.raises(ValueError):
        manifest.get_projects(['unknown'])

def test_load_str():
    # We can load manifest data as a string.

    manifest = Manifest.from_data('''\
    manifest:
      projects:
        - name: foo
          url: https://foo.com
    ''')
    assert manifest.projects[-1].name == 'foo'

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

# Invalid manifests should raise MalformedManifest.
@pytest.mark.parametrize('invalid',
                         glob(os.path.join(THIS_DIRECTORY, 'manifests',
                                           'invalid_*.yml')))
def test_invalid(invalid):
    with open(invalid, 'r') as f:
        data = yaml.safe_load(f.read())

    with pytest.raises(MalformedManifest):
        Manifest.from_data(source_data=data)
