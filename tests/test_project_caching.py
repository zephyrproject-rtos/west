# Copyright (c) 2020, Nordic Semiconductor ASA

import io
import os
import shutil
import subprocess
import textwrap
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
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

from west.app.project import Update
from west.manifest import Project

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


def cache_command(root, jobs=3):
    command = Update()
    command.args = SimpleNamespace(jobs=jobs, unbuffered=False)
    command.updated = set()
    command.auto_cache = os.fspath(root / 'auto-cache')
    command.name_cache = command.path_cache = None
    command._topdir = os.fspath(root)
    command._git_ver = (2, 40, 0)
    return command


@pytest.mark.parametrize(
    'state',
    [
        'empty',
        'populated',
        'alias',
        'real-path-alias',
        'name-cache',
        'missing-case-alias',
        'missing-unicode-alias',
    ],
)
def test_parallel_auto_cache_transaction_lock(tmp_path, monkeypatch, state, directory_alias):
    command = cache_command(tmp_path)
    missing_alias = state in ('missing-case-alias', 'missing-unicode-alias')
    if missing_alias:
        command.auto_cache = os.fspath(tmp_path / 'Cache caf\u00e9')
    projects = [
        Project(name, url, path=f'projects/{name}', topdir=tmp_path)
        for name, url in [('a', 'shared'), ('b', 'shared'), ('c', 'independent')]
    ]
    real_auto_cache = command.project_auto_cache
    cache = Path(real_auto_cache(projects[0]))
    alias = None
    if state != 'empty' and not missing_alias:
        cache.mkdir(parents=True)
    if state == 'name-cache':
        command.name_cache = os.fspath(tmp_path / 'readonly')
        (Path(command.name_cache) / 'a').mkdir(parents=True)
    if state == 'alias' and os.name == 'nt':
        alias = os.fspath(cache).upper()
    elif state in ('alias', 'real-path-alias'):
        link = tmp_path / 'cache-alias'
        directory_alias(link, cache)
        alias = os.fspath(link)
    elif missing_alias:
        original = 'Cache caf\u00e9'
        other = 'cache caf\u00e9' if state == 'missing-case-alias' else 'Cache cafe\u0301'
        probe = tmp_path / 'cache-probe'
        (probe / original).mkdir(parents=True)
        if not (probe / other).exists() or not (probe / original).samefile(probe / other):
            pytest.skip('filesystem does not alias these cache names')
        alias = os.fspath(tmp_path / other / cache.relative_to(command.auto_cache))

    shared_entered, c_entered, attempted, release = [threading.Event() for _ in range(4)]
    first_thread = []
    blocked_owners = set()
    real_lock = threading.Lock
    state_lock = real_lock()
    active = Counter()
    maximum = Counter()

    class ObservedLock:
        def __init__(self):
            self.lock = real_lock()
            self.owner = None

        def acquire(self, blocking=True, timeout=-1):
            who = threading.get_ident()
            if blocking and self.owner is not None and who != self.owner:
                with state_lock:
                    blocked_owners.add(self.owner)
                    if first_thread and self.owner == first_thread[0]:
                        attempted.set()
            acquired = self.lock.acquire(blocking, timeout)
            if acquired:
                self.owner = who
            return acquired

        def release(self):
            self.owner = None
            self.lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *exc):
            self.release()

        def __getattr__(self, name):
            return getattr(self.lock, name)

    def auto_cache(project):
        if alias is not None and project.name == 'b':
            return alias
        return real_auto_cache(project)

    def update(project):
        with state_lock:
            active[project.url] += 1
            maximum[project.url] = max(maximum[project.url], active[project.url])
            if project.url == 'shared':
                if not first_thread:
                    first_thread.append(threading.get_ident())
                    shared_entered.set()
                    if first_thread[0] in blocked_owners:
                        attempted.set()
                else:
                    attempted.set()
        if project.name == 'c':
            c_entered.set()
        try:
            assert release.wait(45)
        finally:
            with state_lock:
                active[project.url] -= 1

    monkeypatch.setattr(command, 'project_auto_cache', auto_cache)
    monkeypatch.setattr(command, 'update', update)
    with ThreadPoolExecutor(max_workers=1) as coordinator:
        monkeypatch.setattr(threading, 'Lock', ObservedLock)
        future = coordinator.submit(command._update_projects, projects)
        try:
            assert shared_entered.wait(15)
            assert c_entered.wait(15)
            assert attempted.wait(15)
        finally:
            release.set()
        assert future.result(timeout=15) == []
    assert maximum['shared'] == 1
    assert maximum['independent'] == 1
    assert command.updated == {'a', 'b', 'c'}


@pytest.mark.parametrize('kind', ['name', 'path'])
def test_parallel_readonly_cache_preserves_priority(tmp_path, monkeypatch, kind):
    command = cache_command(tmp_path, jobs=2)
    projects = [
        Project(name, 'shared', path=f'projects/{name}', topdir=tmp_path) for name in ['a', 'b']
    ]
    readonly = tmp_path / 'readonly'
    setattr(command, f'{kind}_cache', os.fspath(readonly))
    for project in projects:
        (readonly / (project.name if kind == 'name' else project.path)).mkdir(parents=True)
    chosen = []
    monkeypatch.setattr(
        command, 'update', lambda project: chosen.append(command.project_cache(project))
    )
    assert command._update_projects(projects) == []
    assert command.updated == {'a', 'b'}
    assert set(chosen) == {
        os.fspath(readonly / (p.name if kind == 'name' else p.path)) for p in projects
    }
    assert not Path(command.auto_cache).exists()


def shared_cache_workspace(root, *, broken=False):
    shared, independent = root / 'shared', root / 'independent'
    if not broken:
        create_repo(shared)
    create_repo(independent)
    workspace = root / 'workspace'
    create_workspace(workspace)
    projects = [
        Project(
            name,
            remote.as_uri(),
            path=f'projects/{name}',
            topdir=workspace,
            revision='master' if broken and name != 'c' else rev_parse(remote, 'HEAD'),
        )
        for name, remote in [('a', shared), ('b', shared), ('c', independent)]
    ]
    manifest = {
        'manifest': {
            'projects': [
                {'name': p.name, 'path': p.path, 'url': p.url, 'revision': p.revision}
                for p in projects
            ]
        }
    }
    (workspace / 'mp' / 'west.yml').write_text(yaml.safe_dump(manifest), encoding='utf-8')
    cache = root / 'cache \u96ea'
    command = cache_command(root)
    command.auto_cache = os.fspath(cache)
    return workspace, projects, command


@pytest.mark.parametrize('populated,refresh_fails', [(False, False), (True, False), (True, True)])
def test_parallel_shared_auto_cache_git_results(tmp_path, monkeypatch, populated, refresh_fails):
    workspace, projects, command = shared_cache_workspace(tmp_path)
    shared_cache = Path(command.project_auto_cache(projects[0]))
    if populated:
        shared_cache.parent.mkdir(parents=True)
        subprocess.check_call([GIT, 'clone', '--mirror', '--', projects[0].url, shared_cache])
        command.create_auto_cache_info(projects[0], os.fspath(shared_cache))
    mirror_clones = Counter()
    refreshes = []
    lock = threading.Lock()
    real_git = Project.git

    def git(project, args, **kwargs):
        argv = args.split() if isinstance(args, str) else list(args)
        if argv[:2] == ['clone', '--mirror']:
            with lock:
                mirror_clones[project.url] += 1
        if refresh_fails and project.url == projects[0].url and argv[:2] == ['remote', 'update']:
            assert kwargs.get('check') is False
            with lock:
                refreshes.append(project.name)
            return real_git(
                project,
                ['-c', "alias.fail=!printf 'injected refresh failure\\n' >&2; exit 1", 'fail'],
                **kwargs,
            )
        return real_git(project, args, **kwargs)

    monkeypatch.setattr(Project, 'git', git)
    stderr = io.StringIO()
    args = ['update', '-j', '3', '--auto-cache', command.auto_cache]
    if refresh_fails:
        args += ['--fetch', 'always']
    cmd(args, cwd=workspace, stderr=stderr)
    assert mirror_clones[projects[0].url] == (0 if populated else 1)
    assert mirror_clones[projects[2].url] == 1
    for project in projects:
        assert rev_parse(project.abspath, 'HEAD') == project.revision
        assert rev_parse(project.abspath, 'refs/heads/manifest-rev') == project.revision
    assert Path(os.fspath(shared_cache) + '.info').is_file()
    if refresh_fails:
        assert set(refreshes) == {'a', 'b'}
        assert 'injected refresh failure' in stderr.getvalue()


def test_parallel_failed_auto_cache_clone_preserves_failure(tmp_path):
    workspace, projects, command = shared_cache_workspace(tmp_path, broken=True)
    raised, stderr = cmd_raises(
        ['update', '-j', '3', '--auto-cache', command.auto_cache], SystemExit, cwd=workspace
    )
    assert raised.value.code == 1
    assert 'update failed for projects: a, b' in stderr
    assert rev_parse(projects[2].abspath, 'HEAD') == projects[2].revision
    assert not Path(command.project_auto_cache(projects[0]) + '.info').exists()


def test_update_name_cache(tmpdir, update_jobs):
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


def test_update_path_cache(tmpdir, update_jobs):
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


def test_update_auto_cache(tmpdir, update_jobs):
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


def test_update_auto_cache_skipped_remote_update(tmpdir, update_jobs):
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


def test_update_caches_priorities(tmpdir, update_jobs):
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


def test_update_cache_clone_no_checkout(tmpdir, monkeypatch):
    # Test that cloning from a cache does not check out the cache's
    # HEAD. Basic cache coverage is in test_update_name_cache().
    # Here the manifest revision is older than the cache's HEAD.
    name_cache_dir = tmpdir / 'name_cache'
    create_repo(name_cache_dir / 'foo')
    create_repo(name_cache_dir / 'bar')
    add_commit(name_cache_dir / 'foo', 'add old.txt', files={'old.txt': 'old'})
    foo_head = rev_parse(name_cache_dir / 'foo', 'HEAD')
    add_commit(name_cache_dir / 'foo', 'add new.txt', files={'new.txt': 'new'})
    bar_head = rev_parse(name_cache_dir / 'bar', 'HEAD')

    workspace = tmpdir / 'workspace'
    setup_cache_workspace(
        workspace,
        foo_remote=(Path('ignored') / 'here'),
        foo_head=foo_head,
        bar_remote=(Path('ignored') / 'there'),
        bar_head=bar_head,
    )
    workspace.chdir()
    foo = workspace / 'subdir' / 'foo'

    # Ensure that the cache's HEAD files are not written on clone; the checkout deletes
    # them again, so we cannot check after the update but we must check during update.
    # Therefore use monkeypatch to check right after init_project() that no file has been written.
    def checked_init_project(self, project, orig=Update.init_project):
        orig(self, project)
        assert os.listdir(project.abspath) == ['.git']

    monkeypatch.setattr(Update, 'init_project', checked_init_project)

    cmd(['update', '--name-cache', name_cache_dir])
    assert rev_parse(foo, 'HEAD') == foo_head
    assert (foo / 'old.txt').check(file=1)
    assert not (foo / 'new.txt').exists()
    status = subprocess.check_output([GIT, 'status', '--porcelain'], cwd=foo)
    assert status.decode().strip() == ''
    # only west's own branch is left
    local_branches = subprocess.check_output(
        [GIT, 'for-each-ref', '--format=%(refname)', 'refs/heads'], cwd=foo
    )
    assert local_branches.decode().split() == ['refs/heads/manifest-rev']
