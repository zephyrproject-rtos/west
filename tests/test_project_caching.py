# Copyright (c) 2020, Nordic Semiconductor ASA

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

from conftest import (
    GIT,
    add_commit,
    cmd,
    create_branch,
    create_repo,
    create_workspace,
    remote_get_url,
    rev_list,
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

    # The directory tree of the workspace looks like following:
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

    # The directory tree of the workspace looks like following:
    # (workspace)
    # ├── bar
    # └── subdir
    #     └── foo

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

    # The directory tree of the workspace looks like following:
    # (workspace)
    # ├── bar
    # └── subdir
    #     └── foo

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


def test_update_auto_cache(tmpdir):
    # Test that 'west update --auto-cache' works and does set up the local
    # cache correctly.

    # The directory tree of the workspace looks like following:
    # (workspace)
    # ├── bar
    # └── subdir
    #     └── foo

    # The tree of the auto cache looks like following:
    # (path cache)
    # ├── bar
    # │   └── <hash>
    # │   └── <hash>.info
    # └── foo
    #     └── <hash>
    # │   └── <hash>.info

    create_repo(tmpdir / 'remotes' / 'foo')
    create_repo(tmpdir / 'remotes' / 'bar')
    foo_head = rev_parse(tmpdir / 'remotes' / 'foo', 'HEAD')
    bar_head = rev_parse(tmpdir / 'remotes' / 'bar', 'HEAD')

    auto_cache_dir = tmpdir / 'auto_cache_dir'

    workspace = tmpdir / 'workspace'
    setup_cache_workspace(workspace,
                          foo_remote=(tmpdir / 'remotes' / 'foo'),
                          foo_head=foo_head,
                          bar_remote=(tmpdir / 'remotes' / 'bar'),
                          bar_head=bar_head)
    workspace.chdir()
    foo = workspace / 'subdir' / 'foo'
    bar = workspace / 'bar'

    # Test the command line option.
    cmd(['update', '--auto-cache', os.fspath(auto_cache_dir)])
    assert foo.check(dir=1)
    assert bar.check(dir=1)
    assert (auto_cache_dir / "foo").check(dir=1)
    assert (auto_cache_dir / "bar").check(dir=1)
    assert rev_parse(foo, 'HEAD') == foo_head
    assert rev_parse(bar, 'HEAD') == bar_head

    # Check that some info file was created with basic info
    # e.g. /path/to/auto/cache/foo/<hash>.info
    foo_hash = sorted(os.listdir(auto_cache_dir / 'foo'))[0]
    bar_hash = sorted(os.listdir(auto_cache_dir / 'bar'))[0]
    expected_foo_info = textwrap.dedent(f"""
        The following local cache directory was automatically created by west:
        - Local Cache:  {foo_hash}
        - Project Url:  file://{tmpdir / 'remotes' / 'foo'}
    """)
    with open(auto_cache_dir / 'foo' / foo_hash + '.info') as f:
        assert f.read() == expected_foo_info
    expected_bar_info = textwrap.dedent(f"""
        The following local cache directory was automatically created by west:
        - Local Cache:  {bar_hash}
        - Project Url:  file://{tmpdir / 'remotes' / 'bar'}
    """)
    with open(auto_cache_dir / 'bar' / bar_hash + '.info') as f:
        assert f.read() == expected_bar_info

    # Move the repositories out of the way and test the configuration option.
    # (We can't use shutil.rmtree here because Windows.)
    shutil.move(os.fspath(foo), os.fspath(foo) + ".moved")
    shutil.move(os.fspath(bar), os.fspath(bar) + ".moved")
    shutil.move(os.fspath(auto_cache_dir / "foo"),
                os.fspath(auto_cache_dir / "foo") + ".moved")
    shutil.move(os.fspath(auto_cache_dir / "bar"),
                os.fspath(auto_cache_dir / "bar") + ".moved")
    cmd(['config', 'update.auto-cache', os.fspath(auto_cache_dir)])
    cmd(['update'])
    assert foo.check(dir=1)
    assert bar.check(dir=1)
    assert (auto_cache_dir / "foo").check(dir=1)
    assert (auto_cache_dir / "bar").check(dir=1)
    assert rev_parse(foo, 'HEAD') == foo_head
    assert rev_parse(bar, 'HEAD') == bar_head

    # Test that auto-sync works in auto-cache, so a newly added commit in
    # the remote repository should be available after update in the workspace
    # repository and in auto-cache repository (initial workspace setup)
    add_commit(tmpdir / 'remotes' / 'foo', 'new commit')
    add_commit(tmpdir / 'remotes' / 'bar', 'new commit')
    foo_head_new = rev_parse(tmpdir / 'remotes' / 'foo', 'HEAD')
    bar_head_new = rev_parse(tmpdir / 'remotes' / 'bar', 'HEAD')
    other_workspace = tmpdir / 'other_workspace'
    setup_cache_workspace(other_workspace,
                          foo_remote=(tmpdir / 'remotes' / 'foo'),
                          foo_head=foo_head_new,
                          bar_remote=(tmpdir / 'remotes' / 'bar'),
                          bar_head=bar_head_new)
    other_workspace.chdir()
    other_workspace_foo = other_workspace / 'subdir' / 'foo'
    other_workspace_bar = other_workspace / 'bar'
    assert foo_head_new not in rev_list(auto_cache_dir / "foo" / foo_hash)
    assert bar_head_new not in rev_list(auto_cache_dir / "bar" / bar_hash)
    cmd(['update', '--auto-cache', os.fspath(auto_cache_dir)])
    assert other_workspace_foo.check(dir=1)
    assert other_workspace_bar.check(dir=1)
    assert rev_parse(other_workspace_foo, 'HEAD') == foo_head_new
    assert rev_parse(other_workspace_bar, 'HEAD') == bar_head_new
    assert foo_head_new in rev_list(auto_cache_dir / "foo" / foo_hash)
    assert bar_head_new in rev_list(auto_cache_dir / "bar" / bar_hash)

    # Test that auto-sync works in auto-cache, so a newly added commit on any
    # branch in the remote repository is present after update in the workspace
    # repository and in auto-cache repository (existing workspace).
    create_branch(tmpdir / 'remotes' / 'foo', 'anybranch', checkout=True)
    create_branch(tmpdir / 'remotes' / 'bar', 'anybranch', checkout=True)
    add_commit(tmpdir / 'remotes' / 'foo', 'newer commit')
    add_commit(tmpdir / 'remotes' / 'bar', 'newer commit')
    foo_head_newer = rev_parse(tmpdir / 'remotes' / 'foo', 'HEAD')
    bar_head_newer = rev_parse(tmpdir / 'remotes' / 'bar', 'HEAD')
    assert foo_head_newer not in rev_list(auto_cache_dir / "foo" / foo_hash)
    assert bar_head_newer not in rev_list(auto_cache_dir / "bar" / bar_hash)
    # update west.yml manifest to the newer commit id
    manifest_path = Path(other_workspace / 'mp' / 'west.yml')
    mainfest_content = manifest_path.read_text()
    mainfest_content = mainfest_content.replace(foo_head_new, foo_head_newer)
    mainfest_content = mainfest_content.replace(bar_head_new, bar_head_newer)
    manifest_path.write_text(mainfest_content)
    cmd(['update', '--auto-cache', os.fspath(auto_cache_dir)])
    assert rev_parse(other_workspace_foo, 'HEAD') == foo_head_newer
    assert rev_parse(other_workspace_bar, 'HEAD') == bar_head_newer
    assert foo_head_newer in rev_list(auto_cache_dir / "foo" / foo_hash)
    assert bar_head_newer in rev_list(auto_cache_dir / "bar" / bar_hash)


def test_update_caches_priorities(tmpdir):
    # Test that the correct cache is used if multiple caches are specified
    # e.g. if 'west update --name-cache X --path-cache Y --auto-cache Z'

    name_cache_dir = tmpdir / 'name_cache_dir'
    path_cache_dir = tmpdir / 'path_cache_dir'
    auto_cache_dir = tmpdir / 'auto_cache_dir'

    # setup local cache for --name-cache
    # (name_cache)
    # ├── bar
    # └── foo
    create_repo(tmpdir / 'name_cache_remotes' / 'foo')
    create_repo(tmpdir / 'name_cache_remotes' / 'bar')
    name_cache_foo_head = rev_parse(tmpdir / 'name_cache_remotes' / 'foo', 'HEAD')
    name_cache_bar_head = rev_parse(tmpdir / 'name_cache_remotes' / 'bar', 'HEAD')
    subprocess.check_call([GIT, 'clone',
                           os.fspath(tmpdir / 'name_cache_remotes' / 'foo'),
                           os.fspath(name_cache_dir / "foo")])
    subprocess.check_call([GIT, 'clone',
                           os.fspath(tmpdir / 'name_cache_remotes' / 'bar'),
                           os.fspath(name_cache_dir / "bar")])

    # setup remote repositories and local cache for --path-cache
    # (path_cache)
    # ├── bar
    # └── subdir
    #     └── foo
    create_repo(tmpdir / 'path_cache_remotes' / 'foo')
    create_repo(tmpdir / 'path_cache_remotes' / 'bar')
    path_cache_foo_head = rev_parse(tmpdir / 'path_cache_remotes' / 'foo', 'HEAD')
    path_cache_bar_head = rev_parse(tmpdir / 'path_cache_remotes' / 'bar', 'HEAD')
    subprocess.check_call([GIT, 'clone',
                           os.fspath(tmpdir / 'path_cache_remotes' / 'foo'),
                           os.fspath(path_cache_dir / "subdir" / "foo")])
    subprocess.check_call([GIT, 'clone',
                           os.fspath(tmpdir / 'path_cache_remotes' / 'bar'),
                           os.fspath(path_cache_dir / "bar")])

    # setup remote repositories for auto cache
    create_repo(tmpdir / 'auto_cache_remotes' / 'foo')
    create_repo(tmpdir / 'auto_cache_remotes' / 'bar')
    auto_cache_foo_head = rev_parse(tmpdir / 'auto_cache_remotes' / 'foo', 'HEAD')
    auto_cache_bar_head = rev_parse(tmpdir / 'auto_cache_remotes' / 'bar', 'HEAD')

    # Test that foo and bar are created within the workspace. They must be
    # checked out at correct commit id and their remote url is set to original
    # remote url (not local cache path).
    # Note: Remote url can be non-existent since it will clone from local cache

    # setup new workspace and assert that --name-cache is used (highest prio)
    # (workspace)
    # ├── bar (cloned from name cache)
    # └── subdir
    #     └── foo (cloned from name cache)
    workspace1 = tmpdir / 'workspace1'
    foo = workspace1 / 'subdir' / 'foo'
    bar = workspace1 / 'bar'
    setup_cache_workspace(workspace1,
                          foo_remote=(Path('non-existent') / 'here'),
                          foo_head=name_cache_foo_head,
                          bar_remote=(Path('non-existent') / 'there'),
                          bar_head=name_cache_bar_head)
    workspace1.chdir()
    cmd(['update',
         '--name-cache', os.fspath(name_cache_dir),
         '--path-cache', os.fspath(path_cache_dir)])
    assert foo.check(dir=1)
    assert bar.check(dir=1)
    assert rev_parse(foo, 'HEAD') == name_cache_foo_head
    assert rev_parse(bar, 'HEAD') == name_cache_bar_head
    assert remote_get_url(foo) == "file://" + os.fspath(Path('non-existent') / 'here')
    assert remote_get_url(bar) == "file://" + os.fspath(Path('non-existent') / 'there')

    # setup new workspace: mix --name-cache and --path-cache.
    # --name-cache should be used for all repositories present there.
    # Other repositories are then searched in --path-cache.
    # Remove foo from name cache so that foo cannot be found there anymore.
    # (workspace)
    # ├── bar (cloned from name cache)
    # └── subdir
    #     └── foo (cloned from path cache)
    shutil.move(os.fspath(name_cache_dir / 'foo'),
                os.fspath(name_cache_dir / 'foo.moved'))
    workspace2 = tmpdir / 'workspace2'
    foo = workspace2 / 'subdir' / 'foo'
    bar = workspace2 / 'bar'
    setup_cache_workspace(workspace2,
                          foo_remote=(Path('non-existent') / 'here'),
                          foo_head=path_cache_foo_head,
                          bar_remote=(Path('non-existent') / 'there'),
                          bar_head=name_cache_bar_head)
    workspace2.chdir()
    cmd(['update',
         '--name-cache', os.fspath(name_cache_dir),
         '--path-cache', os.fspath(path_cache_dir)])
    assert foo.check(dir=1)
    assert bar.check(dir=1)
    assert rev_parse(foo, 'HEAD') == path_cache_foo_head
    assert rev_parse(bar, 'HEAD') == name_cache_bar_head
    assert remote_get_url(foo) == "file://" + os.fspath(Path('non-existent') / 'here')
    assert remote_get_url(bar) == "file://" + os.fspath(Path('non-existent') / 'there')

    # setup new workspace: --path-cache is preferred over --auto-cache
    # (workspace)
    # ├── bar (cloned from name cache)
    # └── subdir
    #     └── foo (cloned from path cache)
    workspace3 = tmpdir / 'workspace3'
    foo = workspace3 / 'subdir' / 'foo'
    bar = workspace3 / 'bar'
    setup_cache_workspace(workspace3,
                          foo_remote=(Path('non-existent') / 'here'),
                          foo_head=path_cache_foo_head,
                          bar_remote=(Path('non-existent') / 'there'),
                          bar_head=path_cache_bar_head)
    workspace3.chdir()
    cmd(['update',
         '--path-cache', os.fspath(path_cache_dir),
         '--auto-cache', os.fspath(auto_cache_dir)])
    assert foo.check(dir=1)
    assert bar.check(dir=1)
    assert rev_parse(foo, 'HEAD') == path_cache_foo_head
    assert rev_parse(bar, 'HEAD') == path_cache_bar_head
    assert remote_get_url(foo) == "file://" + os.fspath(Path('non-existent') / 'here')
    assert remote_get_url(bar) == "file://" + os.fspath(Path('non-existent') / 'there')

    # setup new workspace: fallback to --auto-cache if not found in other caches.
    # Since the auto cache is not filled, the real remote url has to be used.
    workspace4 = tmpdir / 'workspace4'
    foo = workspace4 / 'subdir' / 'foo'
    bar = workspace4 / 'bar'
    setup_cache_workspace(workspace4,
                          foo_remote=(tmpdir / 'auto_cache_remotes' / 'foo'),
                          foo_head=auto_cache_foo_head,
                          bar_remote=(tmpdir / 'auto_cache_remotes' / 'bar'),
                          bar_head=auto_cache_bar_head)
    workspace4.chdir()

    assert not (auto_cache_dir / 'foo').exists()
    assert not (auto_cache_dir / 'bar').exists()
    cmd(['update',
         '--name-cache', os.fspath(Path('non-existent')),
         '--path-cache', os.fspath(Path('non-existent')),
         '--auto-cache', os.fspath(auto_cache_dir)])
    assert foo.check(dir=1)
    assert bar.check(dir=1)
    assert rev_parse(foo, 'HEAD') == auto_cache_foo_head
    assert rev_parse(bar, 'HEAD') == auto_cache_bar_head
    assert remote_get_url(foo) == "file://" + os.fspath(tmpdir / 'auto_cache_remotes' / 'foo')
    assert remote_get_url(bar) == "file://" + os.fspath(tmpdir / 'auto_cache_remotes' / 'bar')
    assert (auto_cache_dir / 'foo').exists()
    assert (auto_cache_dir / 'bar').exists()
