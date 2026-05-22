# Copyright 2018 Foundries.io Ltd
# Copyright (c) 2020, Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

# Tests for the west.manifest API.
#
# Generally try to avoid shelling out to git in this test file, but if
# it's particularly inconvenient to test something without a real git
# repository, go ahead and make one in a temporary directory.

import logging
import os
import platform
import subprocess
import sys
import textwrap
from copy import deepcopy
from glob import glob
from pathlib import Path, PurePath
from unittest.mock import patch

import pytest
import yaml
from conftest import (
    GIT,
    add_commit,
    add_tag,
    check_proj_consistency,
    checkout_branch,
    cmd,
    create_branch,
    create_repo,
    create_workspace,
    rev_parse,
    update_env,
)

from west.configuration import ConfigFile, Configuration, MalformedConfig

# White box checks for the schema version.
from west.manifest import (
    _VALID_SCHEMA_VERS,
    MANIFEST_PROJECT_INDEX,
    SCHEMA_VERSION,
    ImportFlag,
    MalformedManifest,
    Manifest,
    ManifestImportFailed,
    ManifestProject,
    ManifestVersionError,
    Project,
    _ManifestImportDepth,
    is_group,
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


def nodrive(path):
    return os.path.splitdrive(path)[1]




# Officially _not_ supported! Was actually broken from 1.3 to 1.5
# See #910 and zephyr commit 7d40091fbfedfc
# If this gets in the way of some great new feature then feel free to remove this test.
def test_project_path_is_topdir(repos_tmpdir):
    mnft_dir = Path('zephyr')
    mnft_file = mnft_dir / 'west.yml'

    with open(mnft_file, encoding='utf-8') as f:
        mnft = f.read()
    assert 'path: subdir/Kconfiglib' in mnft
    with open(mnft_file, 'w', encoding='utf-8') as f:
        f.write(mnft.replace('path: subdir/Kconfiglib', 'path: .'))

    cmd(['init', '-l', mnft_dir])
    for c in ['help', 'list', 'update', 'list']:
        outputs = cmd(c)
        assert 'WARNING:' in outputs




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


@pytest.mark.parametrize('ver', sorted(set(['0.6.99', SCHEMA_VERSION] + _VALID_SCHEMA_VERS)))
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
    add_commit(
        p1,
        'add m1.yml and m2.yml',
        files={
            'm1.yml': '''\
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
                                ''',
        },
    )
    assert (p1 / 'm1.yml').is_file()
    assert (p1 / 'm2.yml').is_file()
    checkout_branch(p1, 'master')
    assert not (p1 / 'm1.yml').exists()
    assert not (p1 / 'm2.yml').exists()

    actual = MF().projects
    expected = [
        ManifestProject(path='mp', topdir=topdir),
        Project('p1', 'p1-url', topdir=topdir),
        Project('p2', 'p2-url', topdir=topdir),
        Project('p3', 'p3-url', topdir=topdir),
    ]

    for a, e in zip(actual, expected, strict=True):
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
    add_commit(
        p1,
        'add directory of submanifests',
        files={
            p1 / 'd' / 'ignore-me.txt': 'blah blah blah',
            p1 / 'd' / 'm1.yml': '''\
                      manifest:
                        projects:
                        - name: p2
                          url: p2-url
                      ''',
            p1 / 'd' / 'm2.yml': '''\
                      manifest:
                        projects:
                        - name: p3
                          url: p3-url
                      ''',
        },
    )
    assert (p1 / 'd').is_dir()
    assert (p1 / 'd' / 'ignore-me.txt').is_file()
    assert (p1 / 'd' / 'm1.yml').is_file()
    assert (p1 / 'd' / 'm2.yml').is_file()
    checkout_branch(p1, 'master')
    assert not (p1 / 'd').exists()

    actual = MF().projects
    expected = [
        ManifestProject(path='mp', topdir=topdir),
        Project('p1', 'p1-url', topdir=topdir),
        Project('p2', 'p2-url', topdir=topdir),
        Project('p3', 'p3-url', topdir=topdir),
    ]

    for a, e in zip(actual, expected, strict=True):
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
    subprocess.check_call([GIT, 'update-ref', '-d', 'refs/heads/manifest-rev'], cwd=p)
    with pytest.raises(ManifestImportFailed):
        MF()
    subprocess.check_call([GIT, 'update-ref', 'refs/heads/manifest-rev', 'HEAD'], cwd=p)

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
    add_commit(
        p1,
        'add m1.yml and m2.yml',
        files={
            'm1.yml': '''\
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
                                ''',
        },
    )
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
    add_commit(
        p1,
        'add m1.yml and m2.yml',
        files={
            'm1.yml': '''\
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
                                ''',
        },
    )
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
        return Manifest.from_data(
            {'manifest': {'projects': [{'name': 'foo', 'url': 'ignored', 'import': import_map}]}},
            importer=importer,
        )

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
    ''',
]

_IMPORT_SELF_SUBMANIFESTS = {
    'west.d/01-libraries.yml': '''\
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
    'west.d/02-vendor-hals.yml': '''\
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
    'west.d/03-applications.yml': '''\
    manifest:
      projects:
      - name: my-app
        url: downstream.com/my-app
        revision: my-app-rev
        path: applications/my-app
    ''',
}


def _setup_import_self(tmp_workspace, manifests):
    manifest_repo = tmp_workspace / 'mp'
    (manifest_repo / 'west.d').mkdir()
    for path, content in manifests.items():
        with open(str(manifest_repo / path), 'w') as f:
            f.write(content)


@pytest.mark.parametrize('content', _IMPORT_SELF_MANIFESTS, ids=['dir', 'files'])
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
    actual = MT(topdir=tmp_workspace, importer=make_importer(call_map), import_flags=FPI).projects

    expected = [
        ManifestProject(path='mp', topdir=tmp_workspace),
        # Projects from 01-libraries.yml come first.
        Project(
            'my-1',
            'downstream.com/my-lib-1',
            revision='my-1-rev',
            path='lib/my-1',
            topdir=tmp_workspace,
        ),
        Project(
            'my-2',
            'downstream.com/my-lib-2',
            revision='my-2-rev',
            path='lib/my-2',
            topdir=tmp_workspace,
        ),
        # Next, projects from 02-vendor-hals.yml.
        Project(
            'hal_nordic',
            'downstream.com/hal_nordic',
            revision='my-hal-rev',
            path='modules/hal/nordic',
            topdir=tmp_workspace,
        ),
        Project(
            'hal_downstream_sauce',
            'downstream.com/hal_downstream_only',
            revision='my-down-hal-rev',
            path='modules/hal/downstream_only',
            topdir=tmp_workspace,
        ),
        # After that, 03-applications.yml.
        Project(
            'my-app',
            'downstream.com/my-app',
            revision='my-app-rev',
            path='applications/my-app',
            topdir=tmp_workspace,
        ),
        # upstream is the only element of our projects list, so it's
        # after all the self-imports.
        Project(
            'upstream',
            'upstream.com/upstream',
            revision='refs/tags/v1.0',
            path='upstream',
            topdir=tmp_workspace,
        ),
        # Projects we imported from upstream are last. Projects
        # present upstream which we have already defined should be
        # ignored and not appear here.
        Project(
            'segger',
            'upstream.com/segger',
            revision='segger-upstream-rev',
            path='modules/debug/segger',
            topdir=tmp_workspace,
        ),
    ]

    # Since this test is a bit more complicated than some others,
    # first check that we have all the projects in the right order.
    assert [a.name for a in actual] == [e.name for e in expected]

    # With the basic check done, do a more detailed check.
    for a, e in zip(actual, expected, strict=True):
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

    m = M(
        '''\
    projects:
    - name: foo
      url: https://example.com
      import: true
    ''',
        import_flags=ImportFlag.IGNORE,
    )
    assert m.get_projects(['foo'])

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
    add_commit(
        mainline,
        'mainline/west.yml',
        files={
            'west.yml': '''
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
                      '''
        },
    )
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
    add_commit(
        mainline,
        'mainline/west.yml',
        files={
            'west.yml': '''
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
                      '''
        },
    )
    checkout_branch(mainline, 'master')

    actual = [project.name for project in MF().projects]

    expected = ['manifest', 'mainline', 'downstream-app', 'lib3', 'mainline-app', 'lib2']

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
    west_yml = {'manifest': {'projects': [], 'self': {'import': import_map}}}

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

    projects = load_manifest({'name-blocklist': 'n2', 'name-allowlist': 'n2'}).projects
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
    west_yml = {'manifest': {'projects': [], 'self': {'import': import_map}}}

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

    projects = load_manifest({'name-blacklist': 'n2', 'name-whitelist': 'n2'}).projects
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
    prefixes = {1: 'prefix-1', 2: 'prefix/2', 3: 'pre/fix/3'}
    revs = {}
    for i in [1, 2, 3]:
        p = Path(topdir / prefixes[i] / f'project-{i}')
        create_repo(p)
        create_branch(p, 'manifest-rev', checkout=True)
        add_commit(
            p,
            f'project-{i} manifest',
            files={
                'west.yml': f'''
                       manifest:
                         projects:
                         - name: not-cloned-{i}
                           url: https://example.com/not-cloned-{i}
                       '''
            },
            reconfigure=False,
        )
        revs[i] = rev_parse(p, 'HEAD')

    # Create the main manifest file, which imports these with
    # different prefixes.
    add_commit(
        manifest_repo,
        'add main manifest with import',
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
        reconfigure=False,
    )

    # Check semantics for directly imported projects and nested imports.
    actual = MT(topdir=topdir).projects
    expected = [
        ManifestProject(path='mp', topdir=topdir),
        # Projects in main west.yml with proper path-prefixing
        # applied.
        Project(
            'project-1',
            'https://example.com/project-1',
            revision=revs[1],
            path='prefix-1/project-1',
            topdir=topdir,
            remote_name='r',
        ),
        Project(
            'project-2',
            'https://example.com/project-2',
            revision=revs[2],
            path='prefix/2/project-2',
            topdir=topdir,
            remote_name='r',
        ),
        Project(
            'project-3',
            'https://example.com/project-3',
            revision=revs[3],
            path='pre/fix/3/project-3',
            topdir=topdir,
            remote_name='r',
        ),
        # Imported projects from submanifests. These aren't
        # actually cloned on the file system, but that doesn't
        # matter for this test.
        Project(
            'not-cloned-1',
            'https://example.com/not-cloned-1',
            path='prefix-1/not-cloned-1',
            topdir=topdir,
        ),
        Project(
            'not-cloned-2',
            'https://example.com/not-cloned-2',
            path='prefix/2/not-cloned-2',
            topdir=topdir,
        ),
        Project(
            'not-cloned-3',
            'https://example.com/not-cloned-3',
            path='pre/fix/3/not-cloned-3',
            topdir=topdir,
        ),
    ]
    for a, e in zip(actual, expected, strict=True):
        check_proj_consistency(a, e)


def test_import_path_prefix_self(manifest_repo):
    # The semantics for "self: import: {path-prefix: ...}" are similar
    # to when it's used from a project, except the path-prefix is not
    # prepended to the manifest repository's path.

    # Save typing
    topdir = manifest_repo.topdir

    # Create the main manifest file.
    add_commit(
        manifest_repo,
        'add main manifest with import',
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
                   ''',
        },
        reconfigure=False,
    )

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
    add_commit(
        manifest_repo,
        'add main manifest with import',
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
                   ''',
        },
        reconfigure=False,
    )

    # Check semantics for directly imported projects and nested imports.
    actual = MT(topdir=topdir).projects[1:]
    expected = [
        Project(
            'project-1',
            'https://example.com/project-1',
            path='prefix/1/prefix-2/project-one-path',
            topdir=topdir,
        ),
        Project(
            'project-2',
            'https://example.com/project-2',
            path='prefix/1/prefix-2/project-2',
            topdir=topdir,
        ),
    ]
    for a, e in zip(actual, expected, strict=True):
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
    add_commit(manifest_repo, 'OK', files={'west.yml': mfst('ext')}, reconfigure=False)
    m = MT(topdir=topdir, import_flags=ImportFlag.IGNORE)
    assert Path(m.projects[1].abspath) == Path(topdir) / 'ext' / 'project'

    # An invalid path-prefix, all other things equal, should fail.
    add_commit(manifest_repo, 'NOK 1', files={'west.yml': mfst('..')}, reconfigure=False)
    with pytest.raises(MalformedManifest) as excinfo:
        MT(topdir=topdir, import_flags=ImportFlag.IGNORE)
    assert 'escapes the workspace topdir' in str(excinfo.value)


_def_rec_limit = sys.getrecursionlimit()


# Parametrize to trigger the Python RecursionError in more varied places in the code and provide
# more coverage. We used to test only the default value and to pass this test by chance, missing
# the regression in cpython 3.13.8 caused by this backport to 3.13:
# https://github.com/python/cpython/commit/ebccd1de88d
# Long story in https://github.com/python/cpython/issues/145008 and west issue #908
@pytest.mark.parametrize("py_rec_limit", range(_def_rec_limit, _def_rec_limit + 10))
@pytest.mark.skipif(
    (3, 13, 8) <= sys.version_info[0:3] and sys.version_info[0:3] <= (3, 13, 12),
    reason="cpython issue #145008",
)
def test_import_loop_detection_self(manifest_repo, py_rec_limit):
    # Verify that a self-import which causes an import loop is an error.

    sys.setrecursionlimit(py_rec_limit)

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

    with pytest.raises(_ManifestImportDepth, match=r'\.yml.*too.*deep'):
        MF()


#########################################
# Manifest project group: basic tests
#
# Additional groups of tests follow in later sections.


def test_no_groups_and_import():
    def importer(*args, **kwargs):
        raise RuntimeError("this shouldn't be called")

    with pytest.raises(MalformedManifest) as e:
        Manifest.from_data(
            '''
        manifest:
          projects:
          - name: p
            url: u
            groups:
            - g
            import: True
        ''',
            importer=importer,
        )

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

    assert p('groups: [1,"hello-world",3.14]').groups == ['1', 'hello-world', '3.14']
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
        assert (
            tuple(
                m.is_active(p, extra_filter=extra_filter)
                for p in m.get_projects(['p1', 'p2', 'p3'])
            )
            == expected
        )

    check((True, True, True), '')
    check((True, True, True), 'group-filter: [+ga]')
    check((False, True, True), 'group-filter: [-ga]')
    check((True, True, True), 'group-filter: [-gb]', extra_filter=['+ga'])
    check((True, True, True), 'group-filter: [-gb]', extra_filter=['+gb'])
    check((True, True, True), 'group-filter: [-ga]', extra_filter=['+ga'])
    check((False, True, True), 'group-filter: [-ga]', extra_filter=['+ga', '-ga'])
    check((True, True, True), 'group-filter: [-ga]', extra_filter=['+ga', '-gb'])
    check((False, False, True), 'group-filter: [-ga]', extra_filter=['-gb'])


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
        add_commit(
            project,
            'project.yml',
            files={
                'project.yml': '''
                       manifest:
                          group-filter: [-foo]
                       '''
            },
        )

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
        add_commit(project, 'setup commit', files={'west.yml': imported_fmt.format(group_filter)})
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
        f.write(
            textwrap.dedent(
                '''\
            manifest:
              version: 0.9
            '''
            )
        )
    m = Manifest.from_file()
    assert m.group_filter == []
    assert not hasattr(m, '_legacy_group_filter_warned')

    # Schema version 0.9, group-filter is used, no imports: still a warning.
    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write(
            textwrap.dedent(
                '''\
            manifest:
              version: 0.9
              group-filter: [-ga]
            '''
            )
        )
    m = Manifest.from_file()
    assert m.group_filter == ['-ga']
    assert hasattr(m, '_legacy_group_filter_warned')

    # Schema version 0.9, group-filter is used by an import: warning.
    with open(manifest_repo / 'west.yml', 'w') as f:
        f.write(
            textwrap.dedent(
                f'''\
            manifest:
              version: 0.9
              projects:
                - name: project1
                  revision: {sha1}
                  url: ignored
                  import: true
            '''
            )
        )
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
@pytest.mark.parametrize(
    'invalid', glob(os.path.join(THIS_DIRECTORY, 'manifests', 'invalid_*.yml'))
)
def test_invalid(invalid):
    with open(invalid) as f:
        data = yaml.safe_load(f.read())

    with pytest.raises(MalformedManifest):
        Manifest.from_data(source_data=data)
