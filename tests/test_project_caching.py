# Copyright (c) 2020, Nordic Semiconductor ASA

import os
import shutil

from conftest import (
    cmd,
    create_repo,
    create_workspace,
    rev_parse,
)

assert 'TOXTEMPDIR' in os.environ, "you must run these tests using tox"


def setup_cache_workspace(workspace, remote, foo_head, bar_head):
    # Shared helper code that sets up a workspace used to test the
    # 'west update --foo-cache' options.

    create_workspace(workspace)

    manifest_project = workspace / 'mp'
    with open(manifest_project / 'west.yml', 'w') as f:
        f.write(f'''
        manifest:
          projects:
          - name: foo
            path: subdir/foo
            url: file://{remote}
            revision: {foo_head}
          - name: bar
            url: file://{remote}
            revision: {bar_head}
        ''')


def test_update_name_cache(tmpdir):
    # Test that 'west update --name-cache' works and doesn't hit the
    # network if it doesn't have to.

    remote = tmpdir / 'remote'
    create_repo(remote)
    name_cache_dir = tmpdir / 'name_cache'
    create_repo(name_cache_dir / 'foo')
    create_repo(name_cache_dir / 'bar')
    foo_head = rev_parse(name_cache_dir / 'foo', 'HEAD')
    bar_head = rev_parse(name_cache_dir / 'bar', 'HEAD')

    workspace = tmpdir / 'workspace'
    setup_cache_workspace(workspace, remote, foo_head, bar_head)
    workspace.chdir()
    foo = workspace / 'subdir' / 'foo'
    bar = workspace / 'bar'

    # Test the command line option.
    cmd(['update', '--name-cache', name_cache_dir])
    assert foo.check(dir=1)
    assert bar.check(dir=1)
    assert rev_parse(foo, 'HEAD') == foo_head
    assert rev_parse(bar, 'HEAD') == bar_head

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


def test_update_path_cache(tmpdir):
    # Test that 'west update --path-cache' works and doesn't hit the
    # network if it doesn't have to.

    remote = tmpdir / 'remote'
    create_repo(remote)
    path_cache_dir = tmpdir / 'path_cache_dir'
    create_repo(path_cache_dir / 'subdir' / 'foo')
    create_repo(path_cache_dir / 'bar')
    foo_head = rev_parse(path_cache_dir / 'subdir' / 'foo', 'HEAD')
    bar_head = rev_parse(path_cache_dir / 'bar', 'HEAD')

    workspace = tmpdir / 'workspace'
    setup_cache_workspace(workspace, remote, foo_head, bar_head)
    workspace.chdir()
    foo = workspace / 'subdir' / 'foo'
    bar = workspace / 'bar'

    # Test the command line option.
    cmd(['update', '--path-cache', path_cache_dir])
    assert foo.check(dir=1)
    assert bar.check(dir=1)
    assert rev_parse(foo, 'HEAD') == foo_head
    assert rev_parse(bar, 'HEAD') == bar_head

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
