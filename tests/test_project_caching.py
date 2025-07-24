# Copyright (c) 2020, Nordic Semiconductor ASA

import os
import shutil
from pathlib import Path

from conftest import (
    cmd,
    create_repo,
    create_workspace,
    remote_get_url,
    rev_parse,
)

assert 'TOXTEMPDIR' in os.environ, "you must run these tests using tox"

#
# Helpers
#

def setup_cache_workspace(workspace, foo_remote, foo_head, bar_remote, bar_head):
    # Shared helper code that sets up a workspace used to test the
    # 'west update --foo-cache' options.

    create_workspace(workspace)

    # The directory tree looks like following:
    # (workspace)
    # ├── bar
    # └── subdir
    #     └── foo

    manifest_project = workspace / 'mp'
    with open(manifest_project / 'west.yml', 'w') as f:
        f.write(f'''
        manifest:
          projects:
          - name: foo
            path: subdir/foo
            url: file://{foo_remote}
            revision: {foo_head}
          - name: bar
            url: file://{bar_remote}
            revision: {bar_head}
        ''')

#
# Test cases
#

def test_update_name_cache(tmpdir):
    # Test that 'west update --name-cache' works and doesn't hit the
    # network if it doesn't have to.

    # The directory tree of the name cache looks like following:
    # (name cache)
    # ├── bar
    # └── foo

    name_cache_dir = tmpdir / 'name_cache'
    create_repo(name_cache_dir / 'foo')
    create_repo(name_cache_dir / 'bar')
    foo_head = rev_parse(name_cache_dir / 'foo', 'HEAD')
    bar_head = rev_parse(name_cache_dir / 'bar', 'HEAD')

    # setup the workspace (remote url can be non-existent, since the repository
    # should be cloned from local cache)
    workspace = tmpdir / 'workspace'
    setup_cache_workspace(workspace,
                          foo_remote=(Path('non-existent') / 'here'),
                          foo_head=foo_head,
                          bar_remote=(Path('non-existent') / 'there'),
                          bar_head=bar_head)
    workspace.chdir()
    foo = workspace / 'subdir' / 'foo'
    bar = workspace / 'bar'

    # Test that foo and bar are created within the workspace. They must be
    # checked out at correct commit id and their remote url is set to original
    # remote url (not local cache path).

    # Test the command line option.
    cmd(['update', '--name-cache', name_cache_dir])
    assert foo.check(dir=1)
    assert bar.check(dir=1)
    assert rev_parse(foo, 'HEAD') == foo_head
    assert rev_parse(bar, 'HEAD') == bar_head
    assert remote_get_url(foo) == "file://" + os.fspath(Path('non-existent') / 'here')
    assert remote_get_url(bar) == "file://" + os.fspath(Path('non-existent') / 'there')

    # Move the repositories out of the way and test the configuration option.
    # (We can't use shutil.rmtree here because Windows.)
    shutil.move(os.fspath(foo), os.fspath(tmpdir))
    shutil.move(os.fspath(bar), os.fspath(tmpdir))
    cmd(['config', 'update.name-cache', name_cache_dir])
    cmd('update')
    assert foo.check(dir=1)
    assert bar.check(dir=1)
    assert rev_parse(foo, 'HEAD') == foo_head
    assert rev_parse(bar, 'HEAD') == bar_head
    assert remote_get_url(foo) == "file://" + os.fspath(Path('non-existent') / 'here')
    assert remote_get_url(bar) == "file://" + os.fspath(Path('non-existent') / 'there')


def test_update_path_cache(tmpdir):
    # Test that 'west update --path-cache' works and doesn't hit the
    # network if it doesn't have to.
    # Note: Remote url can be non-existent since it will clone from local cache

    # The directory tree of the name cache looks like following:
    # (path cache)
    # ├── bar
    # └── subdir
    #     └── foo

    path_cache_dir = tmpdir / 'path_cache_dir'
    create_repo(path_cache_dir / 'subdir' / 'foo')
    create_repo(path_cache_dir / 'bar')
    foo_head = rev_parse(path_cache_dir / 'subdir' / 'foo', 'HEAD')
    bar_head = rev_parse(path_cache_dir / 'bar', 'HEAD')

    # setup the workspace (remote url can be non-existent, since the repository
    # should be cloned from local cache)
    workspace = tmpdir / 'workspace'
    setup_cache_workspace(workspace,
                          foo_remote=(Path('non-existent') / 'here'),
                          foo_head=foo_head,
                          bar_remote=(Path('non-existent') / 'there'),
                          bar_head=bar_head)
    workspace.chdir()
    foo = workspace / 'subdir' / 'foo'
    bar = workspace / 'bar'

    # Test that foo and bar are created within the workspace. They must be
    # checked out at correct commit id and their remote url is set to original
    # remote url (not local cache path).

    # Test the command line option.
    cmd(['update', '--path-cache', path_cache_dir])
    assert foo.check(dir=1)
    assert bar.check(dir=1)
    assert rev_parse(foo, 'HEAD') == foo_head
    assert rev_parse(bar, 'HEAD') == bar_head
    assert remote_get_url(foo) == "file://" + os.fspath(Path('non-existent') / 'here')
    assert remote_get_url(bar) == "file://" + os.fspath(Path('non-existent') / 'there')

    # Move the repositories out of the way and test the configuration option.
    # (We can't use shutil.rmtree here because Windows.)
    shutil.move(os.fspath(foo), os.fspath(tmpdir))
    shutil.move(os.fspath(bar), os.fspath(tmpdir))
    cmd(['config', 'update.path-cache', path_cache_dir])
    cmd('update')
    assert foo.check(dir=1)
    assert bar.check(dir=1)
    assert rev_parse(foo, 'HEAD') == foo_head
    assert rev_parse(bar, 'HEAD') == bar_head
    assert remote_get_url(foo) == "file://" + os.fspath(Path('non-existent') / 'here')
    assert remote_get_url(bar) == "file://" + os.fspath(Path('non-existent') / 'there')
