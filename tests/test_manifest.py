# Copyright 2018 Foundries.io Ltd
# Copyright (c) 2020, Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

# Tests for the west.manifest API.
#
# Generally try to avoid shelling out to git in this test file, but if
# it's particularly inconvenient to test something without a real git
# repository, go ahead and make one in a temporary directory.

#########################################
# The very basics
#
# We need to be able to instantiate Projects and parse manifest data
# from strings or dicts, as well as from the file system.

import os
import platform
import subprocess
from pathlib import Path

import pytest
from conftest import add_commit, add_tag, check_proj_consistency, create_repo, rev_parse

from west.manifest import MalformedManifest, Manifest, ManifestProject, Project, validate

if platform.system() == 'Windows':
    TOPDIR = 'C:\\topdir'
    TOPDIR_POSIX = 'C:/topdir'
else:
    TOPDIR = '/topdir'
    TOPDIR_POSIX = TOPDIR

THIS_DIRECTORY = os.path.dirname(__file__)


def M(content, **kwargs):
    # A convenience to save typing
    return Manifest.from_data('manifest:\n' + content, **kwargs)


def MF(**kwargs):
    # A convenience to save typing
    return Manifest.from_file(**kwargs)


def MT(**kwargs):
    # A convenience to save typing
    return Manifest.from_topdir(**kwargs)


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

    p = Project('p', 'some-url', clone_depth=4, west_commands='foo', topdir=TOPDIR)
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

    manifest = Manifest.from_data({
        'manifest': {'projects': [{'name': 'foo', 'url': 'https:foo.com'}]}
    })
    assert manifest.projects[-1].name == 'foo'
    assert manifest.projects[-1].abspath is None


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
    ''') == {'manifest': {'projects': [{'name': 'p', 'url': 'u'}]}}


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
    expected = [Project('p1', 'u1', revision='defaultrev'), Project('p2', 'u2', revision='rev')]
    for p, e in zip(m.projects[1:], expected, strict=True):
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
    expected1 = Project('testproject_v1', 'https://url1.com/testproject', revision='v1.0')
    expected2 = Project('testproject_v2', 'https://url1.com/testproject', revision='v2.0')
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
        return p.git(f'show {rev}:a.txt', capture_stderr=True, capture_stdout=True).stdout.decode(
            'ascii'
        )

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
    assert (
        repr(m.projects[1])
        == 'Project("zephyr", "https://foo.com", revision="r", path=\'zephyr\', '
        'clone_depth=None, west_commands=[\'some-path/west-commands.yml\'], '
        'topdir=None, groups=[], userdata=None)'
    )  # noqa: E501


def test_project_sha(tmpdir):
    tmpdir = Path(os.fspath(tmpdir))
    create_repo(tmpdir)
    add_tag(tmpdir, 'test-tag')
    expected_sha = rev_parse(tmpdir, 'HEAD^{commit}')
    project = Project(
        'name', 'url-do-not-fetch', revision='test-tag', path=tmpdir.name, topdir=tmpdir.parent
    )
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
    desc = 'This is a long multi-line description\nfor project baz.\n'

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

    m = Manifest.from_data('manifest:')
    assert len(m.projects) == 1

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
    assert m.path_raw == 'my-path'
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

    expected = [
        ManifestProject(path='the-manifest-path', west_commands='scripts/west_commands'),
        Project('testproject1', 'https://example1.com/testproject1', revision='rev1'),
        Project('testproject2', 'https://example2.com/testproject2'),
    ]

    # Check the projects are as expected.
    for p, e in zip(m.projects, expected, strict=True):
        check_proj_consistency(p, e)

    # With a "self: path:" value, that will be available in the
    # path_raw attribute, but all other absolute and relative
    # attributes are None since we aren't reading from a workspace.
    assert m.abspath is None
    assert m.relative_path is None
    assert m.path_raw == 'the-manifest-path'
    assert m.repo_abspath is None

    # If "self: path:" is missing, we won't have a path_raw attribute.
    m = M('''\
    projects:
    - name: p
      url: u
    ''')
    assert m.path_raw is None

    # Empty paths are an error.
    with pytest.raises(MalformedManifest) as e:
        M('''\
        projects: []
        self:
          path:''')
    assert 'must be nonempty if present' in str(e.value)
