# Copyright 2018 Foundries.io Ltd
#
# SPDX-License-Identifier: Apache-2.0

from glob import glob
import os
import platform
from unittest.mock import patch

import pytest
import yaml

from west import configuration as config
from west.manifest import Manifest, Defaults, Remote, Project, \
    ManifestProject, MalformedManifest

THIS_DIRECTORY = os.path.dirname(__file__)

@pytest.fixture
def config_file_project_setup(tmpdir):
    tmpdir.join('.west/.west_topdir').ensure()
    tmpdir.join('.west/config').write('''
[manifest]
path = manifests
''')

    # Switch to the top-level West installation directory
    tmpdir.chdir()

    config.read_config()

    return tmpdir

@pytest.fixture
def project_setup(tmpdir):
    tmpdir.join('.west/.west_topdir').ensure()

    # Switch to the top-level West installation directory
    tmpdir.chdir()

    config.config.remove_section('manifest')

    return tmpdir

def deep_eq_check(actual, expected):
    # Check equality of all project fields (projects themselves are
    # not comparable).
    assert actual.name == expected.name
    assert actual.remote == expected.remote
    assert actual.url == expected.url
    assert actual.path == expected.path
    assert actual.abspath == expected.abspath
    assert actual.clone_depth == expected.clone_depth
    assert actual.revision == expected.revision
    assert actual.west_commands == expected.west_commands

@patch('west.util.west_topdir', return_value='/west_top')
def test_init_with_url(west_topdir):
    # Test the project constructor works as expected with a URL.

    p = Project('p', url='some-url')
    assert p.name == 'p'
    assert p.remote is None
    assert p.url == 'some-url'
    assert p.path == 'p'
    assert p.abspath == os.path.realpath(os.path.join('/west_top', 'p'))
    posixpath = p.posixpath
    if platform.system() == 'Windows':
        posixpath = os.path.splitdrive(posixpath)[1]
    assert posixpath == '/west_top/p'
    assert p.clone_depth is None
    assert p.revision == 'master'
    assert p.west_commands is None

@patch('west.util.west_topdir', return_value='/west_top')
def test_remote_url_init(west_topdir):
    # Projects must be initialized with a remote or a URL, but not both.
    # The resulting URLs must behave as documented.

    r1 = Remote('testremote1', 'https://example.com')
    p1 = Project('project1', None, remote=r1)
    assert p1.remote is r1
    assert p1.url == 'https://example.com/project1'

    p2 = Project('project2', None, url='https://example.com/project2')
    assert p2.remote is None
    assert p2.url == 'https://example.com/project2'

    with pytest.raises(ValueError):
        Project('project3', None, remote=r1, url='not-empty')

    with pytest.raises(ValueError):
        Project('project4', None, remote=None, url=None)

@patch('west.util.west_topdir', return_value='/west_top')
def test_no_remote_ok(west_topdir):
    # remotes isn't required if projects are specified by URL.

    content = '''\
    manifest:
      projects:
        - name: testproject
          url: https://example.com/my-project
    '''
    manifest = Manifest.from_data(yaml.safe_load(content))
    assert manifest.projects[1].url == 'https://example.com/my-project'

def test_no_defaults(config_file_project_setup):
    # Manifests with no defaults should work.
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
    '''
    r1 = Remote('testremote1', 'https://example1.com')
    r2 = Remote('testremote2', 'https://example2.com')

    with patch('west.util.west_topdir', return_value='/west_top'):
        manifest = Manifest.from_data(yaml.safe_load(content))

        expected = [ManifestProject(path='manifests'),
                    Project('testproject1', None, path='testproject1',
                            clone_depth=None, revision='rev1', remote=r1),
                    Project('testproject2', None, path='testproject2',
                            clone_depth=None, revision='master', remote=r2)]

    # Check the remotes are as expected.
    assert list(manifest.remotes) == [r1, r2]

    # Check the projects are as expected.
    for p, e in zip(manifest.projects, expected):
        deep_eq_check(p, e)
    assert all(p.abspath == os.path.realpath(os.path.join('/west_top', p.path))
               for p in manifest.projects)

def test_self_tag(project_setup):
    # Manifests with self tag reference.
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
        path: manifests
    '''
    r1 = Remote('testremote1', 'https://example1.com')
    r2 = Remote('testremote2', 'https://example2.com')

    with patch('west.util.west_topdir', return_value='/west_top'):
        manifest = Manifest.from_data(yaml.safe_load(content))

        expected = [ManifestProject(path='manifests'),
                    Project('testproject1', None, path='testproject1',
                            clone_depth=None, revision='rev1', remote=r1),
                    Project('testproject2', None, path='testproject2',
                            clone_depth=None, revision='master', remote=r2)]

    # Check the remotes are as expected.
    assert list(manifest.remotes) == [r1, r2]

    # Check the projects are as expected.
    for p, e in zip(manifest.projects, expected):
        deep_eq_check(p, e)
    assert all(p.abspath == os.path.realpath(os.path.join('/west_top', p.path))
               for p in manifest.projects)

def test_default_clone_depth(config_file_project_setup):
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

    with patch('west.util.west_topdir', return_value='/west_top'):
        manifest = Manifest.from_data(yaml.safe_load(content))

        expected = [ManifestProject(path='manifests'),
                    Project('testproject1', d, path='testproject1',
                            clone_depth=None, revision=d.revision, remote=r1),
                    Project('testproject2', d, path='testproject2',
                            clone_depth=1, revision='rev', remote=r2)]

    # Check that default attributes match.
    assert manifest.defaults.remote == d.remote
    assert manifest.defaults.revision == d.revision

    # Check the remotes are as expected.
    assert list(manifest.remotes) == [r1, r2]

    # Check that the projects are as expected.
    for p, e in zip(manifest.projects, expected):
        deep_eq_check(p, e)
    assert all(p.abspath == os.path.realpath(os.path.join('/west_top', p.path))
               for p in manifest.projects)


def test_path():
    # Projects must be able to override their default paths.
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
    with patch('west.util.west_topdir',
               return_value=os.path.realpath('/west_top')):
        manifest = Manifest.from_data(yaml.safe_load(content))
    assert manifest.projects[1].path == 'sub' + os.path.sep + 'directory'
    assert manifest.projects[1].abspath == \
        os.path.realpath('/west_top/sub/directory')


def test_sections():
    # We no longer validate the west section, so things that would
    # once have been schema errors shouldn't matter.

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
    with patch('west.util.west_topdir',
               return_value=os.path.realpath('/west_top')):
        # Parsing manifest only, no exception raised
        manifest = Manifest.from_data(yaml.safe_load(content_wrong_west))
    assert manifest.projects[1].path == 'sub' + os.path.sep + 'directory'
    assert manifest.projects[1].abspath == \
        os.path.realpath('/west_top/sub/directory')

def test_west_commands():
    # Projects may specify subdirectories containing west commands.
    content = '''\
    manifest:
      remotes:
        - name: testremote
          url-base: https://example.com

      projects:
        - name: zephyr
          remote: testremote
          west-commands: some-path/west-commands.yml
    '''
    with patch('west.util.west_topdir',
               return_value=os.path.realpath('/west_top')):
        manifest = Manifest.from_data(yaml.safe_load(content))
    assert len(manifest.projects) == 2
    assert manifest.projects[-1].west_commands == 'some-path/west-commands.yml'


def test_west_is_ok():
    # Projects named west are allowed now.
    content = '''\
    manifest:
      remotes:
        - name: testremote
          url-base: https://example.com

      projects:
        - name: west
          remote: testremote
    '''
    with patch('west.util.west_topdir',
               return_value=os.path.realpath('/west_top')):
        manifest = Manifest.from_data(yaml.safe_load(content))
    assert manifest.projects[1].name == 'west'


# Invalid manifests should raise MalformedManifest.
@pytest.mark.parametrize('invalid',
                         glob(os.path.join(THIS_DIRECTORY, 'manifests',
                                           'invalid_*.yml')))
@patch('west.util.west_topdir', return_value='/west_top')
def test_invalid(topdir, invalid):
    with pytest.raises(MalformedManifest):
        Manifest.from_file(invalid)
