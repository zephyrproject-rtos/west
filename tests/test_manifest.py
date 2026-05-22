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

import logging
import os
import platform
import subprocess
from copy import deepcopy
from pathlib import Path, PurePath
from unittest.mock import patch

import pytest
import yaml
from conftest import (
    add_commit,
    add_tag,
    check_proj_consistency,
    create_repo,
    create_workspace,
    rev_parse,
)

from west.configuration import ConfigFile, Configuration
from west.manifest import (
    MANIFEST_PROJECT_INDEX,
    ImportFlag,
    MalformedConfig,
    MalformedManifest,
    Manifest,
    ManifestImportFailed,
    ManifestProject,
    Project,
    manifest_path,
    validate,
)

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


#########################################
# ImportFlag tests
#
# These cover the four documented flag values exposed in
# `west.manifest.ImportFlag`. They live in the new tests/ tree; the
# pre-rust counterparts in `tests-legacy/` exercise the same flows
# against the legacy resolver and remain as a reference until the
# migration finishes.


def test_import_flag_ignore_skips_project_and_self_imports():
    # IGNORE must drop every import silently — the resulting manifest
    # contains only the directly-defined projects, and no importer is
    # required.

    m = M(
        '''\
    projects:
    - name: foo
      url: https://example.com
      import: true
    ''',
        import_flags=ImportFlag.IGNORE,
    )
    assert [p.name for p in m.projects[1:]] == ['foo']

    m = M(
        '''\
    projects:
    - name: foo
      url: https://example.com
    self:
      import: a-file
    ''',
        import_flags=ImportFlag.IGNORE,
    )
    assert [p.name for p in m.projects[1:]] == ['foo']


def test_import_flag_default_rejects_unresolvable_imports():
    # With DEFAULT (the default), an import in from_data() has no way to
    # be resolved and the binding surfaces ManifestImportFailed.
    with pytest.raises(ManifestImportFailed):
        M(
            '''\
        projects:
        - name: foo
          url: https://example.com
          import: true
        '''
        )


def test_import_flag_force_projects_uses_importer():
    # FORCE_PROJECTS with from_data() lets the caller resolve per-project
    # imports through an in-memory callback. Any top-level / self imports
    # in the imported body are silently skipped (no filesystem anchor).

    upstream_body = '''\
manifest:
  projects:
    - name: nested
      url: https://example.com/nested
'''

    def importer(project, _file):
        if project.name == 'upstream':
            return upstream_body
        return None

    m = Manifest.from_data(
        '''\
manifest:
  projects:
    - name: upstream
      url: https://example.com/upstream
      import: true
    - name: downstream
      url: https://example.com/downstream
''',
        importer=importer,
        import_flags=ImportFlag.FORCE_PROJECTS,
    )
    names = [p.name for p in m.projects[1:]]
    assert names == ['upstream', 'downstream', 'nested']


def test_import_flag_force_projects_requires_importer():
    with pytest.raises(ValueError, match='FORCE_PROJECTS requires'):
        M(
            '''\
        projects:
        - name: foo
          url: https://example.com
        ''',
            import_flags=ImportFlag.FORCE_PROJECTS,
        )


def test_import_flag_rejects_invalid_combinations():
    # Only constraint between bits (mirrors legacy `_flags_ok`):
    # FORCE_PROJECTS is incompatible with IGNORE / IGNORE_PROJECTS.
    # IGNORE | IGNORE_PROJECTS is allowed (IGNORE subsumes IGNORE_PROJECTS).
    rejected = (
        ImportFlag.IGNORE | ImportFlag.FORCE_PROJECTS,
        ImportFlag.FORCE_PROJECTS | ImportFlag.IGNORE_PROJECTS,
        ImportFlag.IGNORE | ImportFlag.FORCE_PROJECTS | ImportFlag.IGNORE_PROJECTS,
    )
    for combo in rejected:
        with pytest.raises(ValueError, match='invalid import_flags'):
            M(
                '''\
            projects:
            - name: foo
              url: https://example.com
            ''',
                import_flags=combo,
            )

    # IGNORE | IGNORE_PROJECTS is redundant but legal. It should behave
    # like IGNORE on its own — directly-defined projects, no imports.
    m = M(
        '''\
    projects:
    - name: foo
      url: https://example.com
      import: true
    ''',
        import_flags=ImportFlag.IGNORE | ImportFlag.IGNORE_PROJECTS,
    )
    assert [p.name for p in m.projects[1:]] == ['foo']


def test_import_flag_ignore_projects_workspace(tmp_path):
    # IGNORE_PROJECTS lets a workspace caller load the manifest even
    # though a project declares `import:` and is unreachable. This is
    # the real-world flow used by `west update` when only some
    # projects have been cloned.

    ws = tmp_path / 'ws'
    create_workspace(ws)
    manifest_repo = ws / 'mp'
    add_commit(
        manifest_repo,
        'manifest',
        files={
            'west.yml': '''\
manifest:
  projects:
    - name: zephyr
      url: https://example.com/zephyr
      import: true
    - name: net-tools
      url: https://example.com/net-tools
''',
        },
    )

    m = Manifest.from_topdir(topdir=ws, import_flags=ImportFlag.IGNORE_PROJECTS)
    names = [p.name for p in m.projects[1:]]
    assert names == ['zephyr', 'net-tools']


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
    assert m.abspath is not None
    assert Path(m.abspath) == mf
    assert m.posixpath == mf.as_posix()
    assert m.relative_path is not None
    assert Path(m.relative_path) == relpath
    assert m.path_raw is None
    assert m.repo_abspath is not None
    assert Path(m.repo_abspath) == repo_abspath
    assert m.repo_posixpath == repo_abspath.as_posix()
    assert m.topdir is not None
    assert Path(m.topdir) == topdir
    # Legacy ManifestProject tests.
    mproj = m.projects[MANIFEST_PROJECT_INDEX]
    assert mproj.topdir is not None
    assert Path(mproj.topdir) == topdir
    assert mproj.path is not None
    assert Path(mproj.path) == Path('mp')
    # Project tests.
    p1 = m.projects[1]
    assert p1.topdir is not None
    assert Path(p1.topdir) == Path(topdir)
    assert p1.abspath is not None
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
    assert m.abspath is not None
    assert Path(m.abspath) == abspath
    assert m.posixpath == abspath.as_posix()
    assert m.relative_path is not None
    assert Path(m.relative_path) == relpath
    assert m.path_raw == 'something/else'
    assert m.repo_abspath is not None
    assert Path(m.repo_abspath) == repo_abspath
    assert m.repo_posixpath == repo_abspath.as_posix()
    assert m.topdir is not None
    assert Path(m.topdir) == topdir
    # Legacy ManifestProject tests.
    mproj = m.projects[MANIFEST_PROJECT_INDEX]
    assert mproj.topdir is not None
    assert Path(mproj.topdir).is_absolute()
    assert Path(mproj.topdir) == topdir
    assert mproj.path is not None
    assert Path(mproj.path) == Path('mp')
    assert mproj.abspath is not None
    assert Path(mproj.abspath).is_absolute()
    assert Path(mproj.abspath) == repo_abspath
    # Project tests.
    p1 = m.projects[1]
    assert p1.topdir is not None
    assert Path(p1.topdir) == Path(topdir)
    assert p1.abspath is not None
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
    assert p1.abspath is not None
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
    assert m.path_raw == 'p'
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
    assert manifest.topdir is not None
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
    assert manifest.topdir is not None
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
    assert manifest.abspath is not None
    assert Path(manifest.abspath) == another_yml
    assert manifest.repo_abspath is not None
    assert Path(manifest.repo_abspath) == another_repo
    mproj = manifest.projects[0]
    assert mproj.path is not None
    assert Path(mproj.path) == Path('another-repo')
    assert mproj.abspath is not None
    assert Path(mproj.abspath) == another_repo
    assert mproj.posixpath == another_repo.as_posix()
    p1 = manifest.projects[1]
    assert p1.name == 'another-1'
    assert p1.url == 'another-url-1'
    assert p1.topdir is not None
    assert Path(p1.topdir) == topdir
    assert p1.abspath is not None
    assert PurePath(p1.abspath) == topdir / 'another-1'
    p2 = manifest.projects[2]
    assert p2.name == 'another-2'
    assert p2.url == 'another-url-2'
    assert p2.topdir is not None
    assert Path(p2.topdir) == topdir
    assert p2.abspath is not None
    assert Path(p2.abspath) == topdir / 'another' / 'path'

    # If the manifest yaml file does specify its path, the path_raw
    # attribute should reflect that, but we should still reflect what
    # we actually loaded.
    manifest = Manifest.from_file(source_file=another_yml_with_path)
    assert manifest.path_raw == 'yaml-path'
    assert manifest.abspath is not None
    assert Path(manifest.abspath) == another_yml_with_path
    mproj = manifest.projects[0]
    assert mproj.abspath is not None
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

    # keep content_str, content_dict and expected_yaml in sync.

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
      group-filter:
        - -Ddisabled
        - +Cenabled
        - -Bdisabled
        - +Aenabled
    '''
    content_dict = {
        'manifest': {
            'projects': [
                {'name': 'p1', 'url': 'https://example.com/p1', 'revision': 'master'},
                {
                    'name': 'p2',
                    'url': 'https://example.com/p2',
                    'revision': 'deadbeef',
                    'path': 'project-two',
                    'clone-depth': 1,
                    'west-commands': 'commands.yml',
                },
            ],
            'self': {'path': os.path.basename(manifest_repo)},
            'group-filter': [
                '-Bdisabled',
                '-Ddisabled',
            ],
        }
    }

    expected_yaml = '''\
manifest:
  group-filter:
  - -Bdisabled
  - -Ddisabled
  projects:
  - name: p1
    revision: master
    url: https://example.com/p1
  - clone-depth: 1
    name: p2
    path: project-two
    revision: deadbeef
    url: https://example.com/p2
    west-commands: commands.yml
  self:
    path: mp
'''

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

    manifest_topdir = MT(topdir=os.path.dirname(manifest_repo))

    # We can always call as_dict() and as_yaml(), regardless of what's
    # cloned.

    as_dict = manifest.as_dict()

    as_dict_topdir = manifest_topdir.as_dict()
    assert as_dict == as_dict_topdir

    yaml_roundtrip = yaml.safe_load(manifest.as_yaml())
    assert as_dict == content_dict
    assert yaml_roundtrip == content_dict

    # Deterministic output
    assert manifest.as_yaml().replace(" ", "") == expected_yaml.replace(" ", "")
    # More demanding: compare whitespace too
    assert expected_yaml == manifest.as_yaml()

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

    with patch('west.manifest.Project.is_cloned', side_effect=lambda: True):
        manifest = MF()
        with patch('west.manifest.Project.sha', side_effect=sha_patch_1):
            frozen = manifest.as_frozen_dict()
        assert frozen == frozen_expected

        with patch('west.manifest.Project.sha', side_effect=sha_patch_2):
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
        for configfile in [ConfigFile.SYSTEM, ConfigFile.GLOBAL, ConfigFile.LOCAL]:
            try:
                config.delete('manifest.project-filter', configfile=configfile)
            except KeyError:
                pass

    def check_error(project_filter, expected_err_contains):
        for configfile in [ConfigFile.SYSTEM, ConfigFile.GLOBAL, ConfigFile.LOCAL]:
            clean_up_config_files()
            config.set('manifest.project-filter', project_filter, configfile=configfile)

            with pytest.raises(MalformedConfig) as e:
                MT(topdir=topdir)

            err = str(e.value)
            assert (f'invalid "manifest.project-filter" option value "{project_filter}":') in err
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
    config.set('manifest.project-filter', '-foo,-bar,-baz', configfile=ConfigFile.SYSTEM)
    config.set('manifest.project-filter', '-foo', configfile=ConfigFile.GLOBAL)
    manifest = Manifest.from_topdir(topdir=topdir, config=config)
    foo, bar, baz = manifest.get_projects(['foo', 'bar', 'baz'])
    assert not manifest.is_active(foo)
    assert manifest.is_active(bar)
    assert manifest.is_active(baz)

    # Local has higher precedence than either.
    config.set('manifest.project-filter', '-bar,-f.*', configfile=ConfigFile.LOCAL)
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
    ''',
]


@pytest.mark.parametrize(
    'content',
    _DOWNSTREAM_WYMLS,
    ids=['url-true', 'url-west', 'remote-true', 'remote-west', 'default-remote'],
)
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
    actual = Manifest.from_data(content, importer=importer, import_flags=FPI).projects

    expected = [
        ManifestProject(),
        Project('upstream', 'upstream.com/upstream', revision='refs/tags/v1.0', path='upstream'),
        Project(
            'hal_nordic',
            'upstream.com/hal_nordic',
            revision='hal_nordic-upstream-rev',
            path='modules/hal/nordic',
        ),
        Project(
            'segger',
            'upstream.com/segger',
            revision='segger-upstream-rev',
            path='modules/debug/segger',
        ),
    ]

    for a, e in zip(actual, expected, strict=True):
        check_proj_consistency(a, e)


def test_import_with_fork_and_proj():
    # Downstream of fixed release, one forked project, and one
    # additional non-forked project.
    #
    # This verifies that common projects are merged into the previous
    # list, and downstream-only projects are appended onto it.

    importer = make_importer({('upstream', 'west.yml'): _UPSTREAM_WYML})
    actual = Manifest.from_data(
        '''\
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
        import_flags=FPI,
    ).projects

    expected = [
        ManifestProject(),
        Project(
            'hal_nordic',
            'downstream.com/hal_nordic',
            revision='my-branch',
            path='modules/hal/nordic',
        ),
        Project('my-proj', 'downstream.com/my-proj', revision='master', path='my-proj'),
        Project('upstream', 'upstream.com/upstream', revision='refs/tags/v1.0', path='upstream'),
        Project(
            'segger',
            'upstream.com/segger',
            revision='segger-upstream-rev',
            path='modules/debug/segger',
        ),
    ]

    for a, e in zip(actual, expected, strict=True):
        check_proj_consistency(a, e)
