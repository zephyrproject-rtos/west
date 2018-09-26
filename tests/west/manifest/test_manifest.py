# Copyright 2018 Foundries.io Ltd
#
# SPDX-License-Identifier: Apache-2.0

from glob import glob
import os
from unittest.mock import patch

import pytest

from west.manifest import manifest_projects, Project, MalformedManifest

THIS_DIRECTORY = os.path.dirname(__file__)


def check_projects(tmpdir, content, expected):
    with patch('west.util.west_topdir', return_value='/west_top'):
        content_yml = str(tmpdir.join('content.yml'))
        with open(content_yml, 'w') as f:
            f.write(content)
        projects = manifest_projects(content_yml)
        assert projects == expected


def test_no_defaults(tmpdir):
    # Manifests with no defaults should work.
    content = '''\
    manifest:
      remotes:
        - name: testremote1
          url: https://example1.com
        - name: testremote2
          url: https://example2.com

      projects:
        - name: testproject1
          remote: testremote1
          revision: rev1
        - name: testproject2
          remote: testremote2
    '''
    expected = [
        Project('testproject1',
                'https://example1.com/testproject1',
                'rev1',
                'testproject1',
                '/west_top/testproject1',
                None),
        Project('testproject2',
                'https://example2.com/testproject2',
                'master',
                'testproject2',
                '/west_top/testproject2',
                None),
    ]
    check_projects(tmpdir, content, expected)


def test_default_clone_depth(tmpdir):
    # Defaults and clone depth should work as in this example.
    content = '''\
    manifest:
      defaults:
        remote: testremote1
        revision: defaultrev

      remotes:
        - name: testremote1
          url: https://example1.com
        - name: testremote2
          url: https://example2.com

      projects:
        - name: testproject1
        - name: testproject2
          remote: testremote2
          revision: rev
          clone-depth: 1
    '''
    expected = [
        Project('testproject1',
                'https://example1.com/testproject1',
                'defaultrev',
                'testproject1',
                '/west_top/testproject1',
                None),
        Project('testproject2',
                'https://example2.com/testproject2',
                'rev',
                'testproject2',
                '/west_top/testproject2',
                1),
    ]
    check_projects(tmpdir, content, expected)


# Invalid manifests should raise MalformedManifest.
@pytest.mark.parametrize('invalid',
                         glob(os.path.join(THIS_DIRECTORY, 'invalid_*.yml')))
def test_invalid(invalid):
    with pytest.raises(MalformedManifest):
        manifest_projects(invalid)
