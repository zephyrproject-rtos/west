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
