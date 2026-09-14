# Copyright (c) 2020, Nordic Semiconductor ASA

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from conftest import (
    GIT,
    add_commit,
    chdir,
    cmd,
    cmd_raises,
    create_branch,
    create_repo,
    create_workspace,
    remote_get_url,
    rev_list,
    rev_parse,
)

from west.commands import WestCommand

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


def setup_nested_cache_workspace(workspace, outer_remote, outer_head, inner_remote, inner_head):
    # Shared helper code that sets up a workspace in which one
    # project's path contains another project's path.

    create_workspace(workspace)

    # The directory tree of the workspace looks like following:
    # (workspace)
    # └── outer
    #     └── nested
    #         └── inner
    #
    # 'inner' is listed first on purpose: 'west update' visits projects
    # in manifest order, so 'outer' is initialized after its own
    # directory has already been created for 'inner'.

    manifest_project = workspace / 'mp'
    with open(manifest_project / 'west.yml', 'w') as f:
        f.write(f'''
        manifest:
          projects:
          - name: inner
            path: outer/nested/inner
            url: file://{inner_remote}
            revision: {inner_head}
          - name: outer
            path: outer
            url: file://{outer_remote}
            revision: {outer_head}
        ''')


def setup_nested_path_cache(tmpdir):
    path_cache_dir = tmpdir / 'path_cache_dir'
    outer_cache = path_cache_dir / 'outer'
    inner_cache = outer_cache / 'nested' / 'inner'
    create_repo(outer_cache)
    create_repo(inner_cache)

    outer_head = rev_parse(outer_cache, 'HEAD')
    inner_head = rev_parse(inner_cache, 'HEAD')
    workspace = tmpdir / 'workspace'
    setup_nested_cache_workspace(
        workspace,
        outer_remote=(Path('non-existent') / 'outer'),
        outer_head=outer_head,
        inner_remote=(Path('non-existent') / 'inner'),
        inner_head=inner_head,
    )
    return path_cache_dir, workspace, outer_head, inner_head


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
    setup_cache_workspace(
        workspace,
        foo_remote=(Path('non-existent') / 'here'),
        foo_head=foo_head,
        bar_remote=(Path('non-existent') / 'there'),
        bar_head=bar_head,
    )
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
    setup_cache_workspace(
        workspace,
        foo_remote=(Path('non-existent') / 'here'),
        foo_head=foo_head,
        bar_remote=(Path('non-existent') / 'there'),
        bar_head=bar_head,
    )
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


def test_update_cache_nested_projects(tmpdir):
    # Test that a cached 'west update' can initialize a project whose
    # path contains another project that was updated first.
    #
    # 'git clone' refuses to write into a directory that already exists
    # and is not empty, so west cannot clone such a project from the
    # cache. Without a cache the same layout works, because 'git init'
    # has no such restriction.
    #
    # Note: the remote URLs are non-existent, so this can only pass if
    # every object still comes from the cache.

    # The directory tree of the path cache looks like following:
    # (path cache)
    # └── outer
    #     └── nested
    #         └── inner

    path_cache_dir, workspace, outer_head, inner_head = setup_nested_path_cache(tmpdir)
    workspace.chdir()
    outer = workspace / 'outer'
    inner = workspace / 'outer' / 'nested' / 'inner'

    # A relative cache path must keep the same meaning when the outer
    # project's in-place initialization runs Git from inside the project.
    path_cache_arg = os.path.relpath(path_cache_dir, workspace)
    cmd(['update', '--path-cache', path_cache_arg])

    assert outer.check(dir=1)
    assert inner.check(dir=1)
    assert rev_parse(outer, 'HEAD') == outer_head
    assert rev_parse(inner, 'HEAD') == inner_head
    assert remote_get_url(outer) == "file://" + os.fspath(Path('non-existent') / 'outer')
    assert remote_get_url(inner) == "file://" + os.fspath(Path('non-existent') / 'inner')


def test_update_cache_nested_project_sha_from_remote_ref(tmpdir):
    # A local clone copies non-tip objects reachable only through remote-tracking
    # refs. In-place cache seeding must make the same objects available.
    #
    # Naming such an object in a fetch needs protocol v2, so west only asks
    # for it from git v2.18 on. Below that the seed copies branch tips only
    # and the revision comes from the remote instead, which this test's
    # deliberately non-existent URL cannot serve.
    git_version = WestCommand._parse_git_version(subprocess.check_output([GIT, '--version']))
    if git_version is None or git_version < (2, 18, 0):
        pytest.skip('seeding an unadvertised object requires fetch protocol v2 (git v2.18)')

    outer_remote = tmpdir / 'outer_remote'
    create_repo(outer_remote)
    create_branch(outer_remote, 'side', checkout=True)
    add_commit(outer_remote, 'side commit')
    outer_head = rev_parse(outer_remote, 'HEAD')
    add_commit(outer_remote, 'later side commit')
    subprocess.check_call([GIT, 'checkout', 'master'], cwd=outer_remote)

    path_cache_dir = tmpdir / 'path_cache_dir'
    outer_cache = path_cache_dir / 'outer'
    subprocess.check_call([GIT, 'clone', '--', outer_remote, outer_cache])
    create_repo(outer_cache / 'nested' / 'inner')
    inner_head = rev_parse(outer_cache / 'nested' / 'inner', 'HEAD')

    workspace = tmpdir / 'workspace'
    setup_nested_cache_workspace(
        workspace,
        outer_remote=(Path('non-existent') / 'outer'),
        outer_head=outer_head,
        inner_remote=(Path('non-existent') / 'inner'),
        inner_head=inner_head,
    )
    workspace.chdir()

    env = {
        'GIT_CONFIG_COUNT': '1',
        'GIT_CONFIG_KEY_0': 'protocol.version',
        'GIT_CONFIG_VALUE_0': '0',
    }
    cmd(['update', '--path-cache', path_cache_dir], env=env)

    assert rev_parse(workspace / 'outer', 'HEAD') == outer_head


def test_update_cache_nested_project_sha_from_remote_if_cache_stale(tmpdir):
    outer_remote = tmpdir / 'outer_remote'
    create_repo(outer_remote)

    path_cache_dir = tmpdir / 'path_cache_dir'
    outer_cache = path_cache_dir / 'outer'
    subprocess.check_call([GIT, 'clone', '--', outer_remote, outer_cache])
    add_commit(outer_remote, 'uncached commit')
    outer_head = rev_parse(outer_remote, 'HEAD')

    create_repo(outer_cache / 'nested' / 'inner')
    inner_head = rev_parse(outer_cache / 'nested' / 'inner', 'HEAD')

    workspace = tmpdir / 'workspace'
    setup_nested_cache_workspace(
        workspace,
        outer_remote=outer_remote,
        outer_head=outer_head,
        inner_remote=(Path('non-existent') / 'inner'),
        inner_head=inner_head,
    )
    workspace.chdir()

    cmd(['update', '--path-cache', path_cache_dir])

    assert rev_parse(workspace / 'outer', 'HEAD') == outer_head
    assert rev_parse(workspace / 'outer' / 'nested' / 'inner', 'HEAD') == inner_head


def test_update_cache_nested_project_retries_failed_seed(tmpdir):
    path_cache_dir, workspace, outer_head, inner_head = setup_nested_path_cache(tmpdir)
    outer_cache = path_cache_dir / 'outer'
    workspace.chdir()

    # Make the outer cache invalid for one update attempt, then restore it.
    # A retry must seed the newly initialized outer repository again instead
    # of treating the failed initialization as a complete clone.
    saved_git_dir = tmpdir / 'outer.git'
    shutil.move(os.fspath(outer_cache / '.git'), saved_git_dir)
    cmd_raises(['update', '--path-cache', path_cache_dir], SystemExit)
    assert rev_parse(workspace / 'outer' / 'nested' / 'inner', 'HEAD') == inner_head
    assert not (workspace / 'outer' / '.git').exists()
    shutil.move(saved_git_dir, os.fspath(outer_cache / '.git'))

    cmd(['update', '--path-cache', path_cache_dir])

    assert rev_parse(workspace / 'outer', 'HEAD') == outer_head
    assert rev_parse(workspace / 'outer' / 'nested' / 'inner', 'HEAD') == inner_head


def test_update_cache_nested_project_preserves_preexisting_git_dir(tmpdir):
    path_cache_dir, workspace, _, _ = setup_nested_path_cache(tmpdir)
    outer_cache = path_cache_dir / 'outer'
    outer_git_dir = workspace / 'outer' / '.git'
    outer_git_dir.ensure(dir=True)
    sentinel = outer_git_dir / 'keep-me'
    sentinel.write('user data')
    workspace.chdir()

    saved_git_dir = tmpdir / 'outer.git'
    shutil.move(os.fspath(outer_cache / '.git'), saved_git_dir)
    cmd_raises(['update', '--path-cache', path_cache_dir], SystemExit)

    assert sentinel.read() == 'user data'


def test_update_cache_nested_project_reports_failed_cleanup(tmpdir, monkeypatch):
    path_cache_dir, workspace, _, _ = setup_nested_path_cache(tmpdir)
    outer_cache = path_cache_dir / 'outer'
    workspace.chdir()

    # Force cache seeding and its cleanup to fail. The cleanup error must be
    # reported without replacing the original update failure.
    shutil.move(os.fspath(outer_cache / '.git'), tmpdir / 'outer.git')

    def fail_cleanup(_):
        raise OSError('cleanup failed')

    monkeypatch.setattr(shutil, 'rmtree', fail_cleanup)
    _, stderr = cmd_raises(['update', '--path-cache', path_cache_dir], SystemExit)

    cleanup_error = f'failed to remove incomplete repository {workspace / "outer" / ".git"}'
    assert f'{cleanup_error}: cleanup failed' in stderr
    assert 'update failed for project outer' in stderr


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
    setup_cache_workspace(
        workspace,
        foo_remote=(tmpdir / 'remotes' / 'foo'),
        foo_head=foo_head,
        bar_remote=(tmpdir / 'remotes' / 'bar'),
        bar_head=bar_head,
    )
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
    foo_hash = min(os.listdir(auto_cache_dir / 'foo'))
    bar_hash = min(os.listdir(auto_cache_dir / 'bar'))
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
    shutil.move(os.fspath(auto_cache_dir / "foo"), os.fspath(auto_cache_dir / "foo") + ".moved")
    shutil.move(os.fspath(auto_cache_dir / "bar"), os.fspath(auto_cache_dir / "bar") + ".moved")
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
    setup_cache_workspace(
        other_workspace,
        foo_remote=(tmpdir / 'remotes' / 'foo'),
        foo_head=foo_head_new,
        bar_remote=(tmpdir / 'remotes' / 'bar'),
        bar_head=bar_head_new,
    )
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


def test_update_auto_cache_skipped_remote_update(tmpdir):
    foo_remote = Path(tmpdir / 'remotes' / 'foo')
    bar_remote = Path(tmpdir / 'remotes' / 'bar')
    auto_cache_dir = Path(tmpdir / 'auto_cache_dir')

    def create_foo_bar_commits():
        add_commit(foo_remote, 'new commit')
        add_commit(bar_remote, 'new commit')
        foo_head = rev_parse(foo_remote, 'HEAD')
        bar_head = rev_parse(bar_remote, 'HEAD')
        return foo_head, bar_head

    def setup_workspace_and_west_update(workspace, foo_head, bar_head):
        setup_cache_workspace(
            workspace,
            foo_remote=foo_remote,
            foo_head=foo_head,
            bar_remote=bar_remote,
            bar_head=bar_head,
        )
        with chdir(workspace):
            stdout = cmd(['-v', 'update', '--auto-cache', auto_cache_dir])
        return stdout

    create_repo(foo_remote)
    create_repo(bar_remote)
    foo_commit1, bar_commit1 = create_foo_bar_commits()
    foo_commit2, bar_commit2 = create_foo_bar_commits()

    # run initial west update to setup auto-cache and get cache directories
    setup_workspace_and_west_update(
        tmpdir / 'workspace1',
        foo_head=foo_commit1,
        bar_head=bar_commit1,
    )

    # read the auto-cache hashes from foo and bar
    (bar_hash,) = [p for p in (auto_cache_dir / 'bar').iterdir() if p.is_dir()]
    auto_cache_dir_bar = auto_cache_dir / 'bar' / bar_hash
    (foo_hash,) = [p for p in (auto_cache_dir / 'foo').iterdir() if p.is_dir()]
    auto_cache_dir_foo = auto_cache_dir / 'foo' / foo_hash

    # Imitate that foo remote is temporarily offline by moving it temporarily.
    # Since foo and bar revisions are used which are already contained in the auto-cache,
    # west update should work with according messages as there is no need to update remotes.
    foo_moved = Path(tmpdir / 'remotes' / 'foo.moved')
    shutil.move(foo_remote, foo_moved)
    stdout = setup_workspace_and_west_update(
        tmpdir / 'workspace2',
        foo_head=foo_commit2,
        bar_head=bar_commit2,
    )
    shutil.move(foo_moved, foo_remote)
    msgs = [
        f"foo: auto-cache remote update is skipped as it already contains commit {foo_commit2}",
        f"foo: cloning from {auto_cache_dir_foo}",
        f"bar: auto-cache remote update is skipped as it already contains commit {bar_commit2}",
        f"bar: cloning from {auto_cache_dir_bar}",
    ]
    for msg in msgs:
        assert msg in stdout

    # If a new commit is used, the auto-cache should be updated with remote
    foo_commit3, bar_commit3 = create_foo_bar_commits()
    stdout = setup_workspace_and_west_update(
        tmpdir / 'workspace3',
        foo_head=foo_commit3,
        bar_head=bar_commit3,
    )
    msgs = [
        f"foo: update auto-cache ({auto_cache_dir_foo}) with remote",
        f"foo: cloning from {auto_cache_dir_foo}",
        f"bar: update auto-cache ({auto_cache_dir_bar}) with remote",
        f"bar: cloning from {auto_cache_dir_bar}",
    ]
    for msg in msgs:
        assert msg in stdout

    # If a branch is used as revision, the auto-cache must be updated.
    stdout = setup_workspace_and_west_update(
        tmpdir / 'workspace4',
        foo_head='master',
        bar_head='master',
    )
    msgs = [
        f"foo: update auto-cache ({auto_cache_dir_foo}) with remote",
        f"foo: cloning from {auto_cache_dir_foo}",
        f"bar: update auto-cache ({auto_cache_dir_bar}) with remote",
        f"bar: cloning from {auto_cache_dir_bar}",
    ]
    for msg in msgs:
        assert msg in stdout


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
    subprocess.check_call(
        [
            GIT,
            'clone',
            os.fspath(tmpdir / 'name_cache_remotes' / 'foo'),
            os.fspath(name_cache_dir / "foo"),
        ]
    )
    subprocess.check_call(
        [
            GIT,
            'clone',
            os.fspath(tmpdir / 'name_cache_remotes' / 'bar'),
            os.fspath(name_cache_dir / "bar"),
        ]
    )

    # setup remote repositories and local cache for --path-cache
    # (path_cache)
    # ├── bar
    # └── subdir
    #     └── foo
    create_repo(tmpdir / 'path_cache_remotes' / 'foo')
    create_repo(tmpdir / 'path_cache_remotes' / 'bar')
    path_cache_foo_head = rev_parse(tmpdir / 'path_cache_remotes' / 'foo', 'HEAD')
    path_cache_bar_head = rev_parse(tmpdir / 'path_cache_remotes' / 'bar', 'HEAD')
    subprocess.check_call(
        [
            GIT,
            'clone',
            os.fspath(tmpdir / 'path_cache_remotes' / 'foo'),
            os.fspath(path_cache_dir / "subdir" / "foo"),
        ]
    )
    subprocess.check_call(
        [
            GIT,
            'clone',
            os.fspath(tmpdir / 'path_cache_remotes' / 'bar'),
            os.fspath(path_cache_dir / "bar"),
        ]
    )

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
    setup_cache_workspace(
        workspace1,
        foo_remote=(Path('non-existent') / 'here'),
        foo_head=name_cache_foo_head,
        bar_remote=(Path('non-existent') / 'there'),
        bar_head=name_cache_bar_head,
    )
    workspace1.chdir()
    cmd(
        [
            'update',
            '--name-cache',
            os.fspath(name_cache_dir),
            '--path-cache',
            os.fspath(path_cache_dir),
        ]
    )
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
    shutil.move(os.fspath(name_cache_dir / 'foo'), os.fspath(name_cache_dir / 'foo.moved'))
    workspace2 = tmpdir / 'workspace2'
    foo = workspace2 / 'subdir' / 'foo'
    bar = workspace2 / 'bar'
    setup_cache_workspace(
        workspace2,
        foo_remote=(Path('non-existent') / 'here'),
        foo_head=path_cache_foo_head,
        bar_remote=(Path('non-existent') / 'there'),
        bar_head=name_cache_bar_head,
    )
    workspace2.chdir()
    cmd(
        [
            'update',
            '--name-cache',
            os.fspath(name_cache_dir),
            '--path-cache',
            os.fspath(path_cache_dir),
        ]
    )
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
    setup_cache_workspace(
        workspace3,
        foo_remote=(Path('non-existent') / 'here'),
        foo_head=path_cache_foo_head,
        bar_remote=(Path('non-existent') / 'there'),
        bar_head=path_cache_bar_head,
    )
    workspace3.chdir()
    cmd(
        [
            'update',
            '--path-cache',
            os.fspath(path_cache_dir),
            '--auto-cache',
            os.fspath(auto_cache_dir),
        ]
    )
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
    setup_cache_workspace(
        workspace4,
        foo_remote=(tmpdir / 'auto_cache_remotes' / 'foo'),
        foo_head=auto_cache_foo_head,
        bar_remote=(tmpdir / 'auto_cache_remotes' / 'bar'),
        bar_head=auto_cache_bar_head,
    )
    workspace4.chdir()

    assert not (auto_cache_dir / 'foo').exists()
    assert not (auto_cache_dir / 'bar').exists()
    cmd(
        [
            'update',
            '--name-cache',
            os.fspath(Path('non-existent')),
            '--path-cache',
            os.fspath(Path('non-existent')),
            '--auto-cache',
            os.fspath(auto_cache_dir),
        ]
    )
    assert foo.check(dir=1)
    assert bar.check(dir=1)
    assert rev_parse(foo, 'HEAD') == auto_cache_foo_head
    assert rev_parse(bar, 'HEAD') == auto_cache_bar_head
    assert remote_get_url(foo) == "file://" + os.fspath(tmpdir / 'auto_cache_remotes' / 'foo')
    assert remote_get_url(bar) == "file://" + os.fspath(tmpdir / 'auto_cache_remotes' / 'bar')
    assert (auto_cache_dir / 'foo').exists()
    assert (auto_cache_dir / 'bar').exists()
