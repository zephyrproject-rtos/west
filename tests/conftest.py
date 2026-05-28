# Copyright (c) 2019, 2020 Nordic Semiconductor ASA
# Copyright 2026 The Zephyr Project Contributors.
#
# SPDX-License-Identifier: Apache-2.0

'''Shared fixtures and helpers for the active pytest suite.

This file is the post-port descendant of `tests-legacy/conftest.py`.
It carries forward the pieces that don't depend on the removed
`west.app.main` entry point — pure subprocess-driven git
helpers, env-isolation context managers, the autouse env
fixture that keeps tests from touching the user's real
`~/.config/west/...`, plus a `cmd` / `cmd_raises` pair that
spawns the rust binary instead of calling `main.main(argv)`
in-process.

The `cmd` helpers keep the legacy public signatures so a
migrating test can `from conftest import cmd, cmd_raises` and
call them as before. Internals changed though: any test that
patched in-process state via `mock.patch("west.app.main…")`
needs rewriting because that module is permanently gone.

Deliberately NOT carried forward:

- `cmd_subprocess` (legacy double-subprocess shape) — the new
  `cmd` IS the subprocess, so `cmd_subprocess` and `cmd` would
  be duplicates.
- The session-scoped multi-project workspace fixtures
  (`_session_repos`, `repos_tmpdir`, `west_init_tmpdir`,
  `west_update_tmpdir`). They're heavyweight and only
  `test_project*.py` need them; deferred until those files
  migrate.
'''

import contextlib
import io
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path, PurePath

# stdlib on 3.11+; `tomli` is the upstream code vendored as
# `tomllib` and ships as a 3.10 fallback (see the test deps in
# pyproject.toml).
try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

import pytest
import tomli_w
import yaml

GIT = shutil.which('git')
assert GIT and Path(GIT).exists()

# Set to True by the `_check_git_capabilities` autouse fixture if
# `git init --initial-branch=<name>` is supported (git ≥ 2.28).
# Older systems fall back to `git init` + `git checkout -B`.
GIT_INIT_HAS_BRANCH = False

WINDOWS = platform.system() == 'Windows'

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BIN_NAME = 'west.exe' if os.name == 'nt' else 'west'


def _locate_west_binary() -> str:
    '''Find the rust `west` binary the `cmd` helpers will spawn.

    Order of preference:

    1. ``WEST_TEST_BIN`` env override (CI / cross-builds).
    2. ``target/release/west`` (matches ``cargo build --release``
       and ``_build_backend.py``'s wheel build).
    3. ``target/debug/west`` (matches plain ``cargo build``).
    4. ``shutil.which("west")`` (post-install on PATH).

    Errors clearly when none of those resolve, so the "I forgot
    to run cargo build" failure mode is loud.
    '''
    override = os.environ.get('WEST_TEST_BIN')
    if override:
        return override
    for build in ('release', 'debug'):
        cand = _REPO_ROOT / 'target' / build / _BIN_NAME
        if cand.is_file():
            return str(cand)
    found = shutil.which('west')
    if found:
        return found
    raise RuntimeError(
        'west binary not found. Run `cargo build --release -p west-cli` '
        'or `pip install -e .` first, or set WEST_TEST_BIN.',
    )


_WEST_BIN = _locate_west_binary()


# =========================================================================
# Context managers
# =========================================================================


@contextlib.contextmanager
def yaml_editor(yaml_f):
    '''Open `yaml_f`, yield the parsed dict, write it back on
    exit. Mutating the yielded dict mutates the file. For the
    manifest file specifically use `manifest_editor` (which is
    format-aware) — this helper is for genuinely YAML-only inputs
    such as `west-commands.yml`.'''
    with open(yaml_f, 'r+'):
        pass  # fail fast if not writable
    with open(yaml_f) as f:
        mf = yaml.safe_load(f)
    yield mf
    with open(yaml_f, 'w') as f:
        yaml.safe_dump(mf, f, sort_keys=False)


# Manifest format dispatch — list of supported extensions and the
# (load, dump) pair for each. Used by `manifest_editor` to round-trip
# a manifest file regardless of its on-disk format, and by the
# parametric `manifest_format` fixture machinery.
_MANIFEST_FORMATS = {
    'yaml': {
        'ext': 'yml',
        'load': yaml.safe_load,
        # `sort_keys=False` keeps the template's hand-curated order;
        # `default_flow_style=False` produces the readable block style
        # the legacy template used.
        'dump': lambda d: yaml.safe_dump(d, sort_keys=False, default_flow_style=False),
    },
    'json': {
        'ext': 'json',
        'load': json.loads,
        'dump': lambda d: json.dumps(d, indent=2) + '\n',
    },
    'toml': {
        'ext': 'toml',
        'load': tomllib.loads,
        'dump': tomli_w.dumps,
    },
}


def _format_from_path(path):
    '''Resolve a manifest file path to a format key (`'yaml'`/`'json'`/`'toml'`).
    Used by `manifest_editor` to discover the on-disk format from the
    filename alone, mirroring `west_core::manifest::parse_body_by_extension`.'''
    suffix = Path(path).suffix.lstrip('.').lower()
    if suffix in ('yml', 'yaml'):
        return 'yaml'
    if suffix == 'json':
        return 'json'
    if suffix == 'toml':
        return 'toml'
    raise ValueError(f'unsupported manifest extension: {path!r}')


def _dump_manifest(data, fmt):
    '''Render a manifest dict to a string in the requested format.'''
    return _MANIFEST_FORMATS[fmt]['dump'](data)


def _load_manifest(text, fmt):
    '''Parse a manifest string back into a dict for the given format.'''
    return _MANIFEST_FORMATS[fmt]['load'](text)


@contextlib.contextmanager
def manifest_editor(workspace_dir):
    '''Edit the manifest file inside `workspace_dir`, regardless of its
    on-disk format. Yields the parsed dict and writes it back on exit
    using the same serializer the file came in with.

    The fixture stack guarantees exactly one `west.{yml,toml,json}`
    under `<workspace_dir>/zephyr/`; mismatched globs assert.'''
    matches = sorted((Path(workspace_dir) / 'zephyr').glob('west.*'))
    if len(matches) != 1:
        raise AssertionError(
            f'expected exactly one manifest file in {workspace_dir}/zephyr, got {matches!r}',
        )
    path = matches[0]
    fmt = _format_from_path(path)
    with open(path) as f:
        data = _load_manifest(f.read(), fmt)
    yield data
    with open(path, 'w') as f:
        f.write(_dump_manifest(data, fmt))


@contextlib.contextmanager
def tmp_west_topdir(path):
    '''Create a `.west/` directory under `path` for the lifetime of
    the with-block, then remove it.'''
    west_dir = Path(path) / '.west'
    west_dir.mkdir(parents=True)
    try:
        yield
    finally:
        west_dir.rmdir()


@contextlib.contextmanager
def update_env(env):
    '''Temporarily mutate `os.environ`. Keys with `None` values are
    unset; other keys are set to the given value. Full restore on
    exit.'''
    env_bak = dict(os.environ)
    env_vars = {}
    for k, v in env.items():
        if v is None and k in os.environ:
            del os.environ[k]
        elif v is not None:
            env_vars[k] = v
    os.environ.update(env_vars)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(env_bak)


@contextlib.contextmanager
def chdir(path):
    '''Temporarily change cwd; restore on exit.'''
    oldpwd = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(oldpwd)


# =========================================================================
# Autouse fixtures
# =========================================================================


@pytest.fixture(scope='session', autouse=True)
def _check_git_capabilities(tmpdir_factory):
    '''Once per session, detect whether `git init --initial-branch`
    is supported. The helpers below branch on `GIT_INIT_HAS_BRANCH`.

    We use the git binary directly (not `WestCommand._parse_git_version`)
    to keep the conftest behaviour independent of the code under
    test.'''
    global GIT_INIT_HAS_BRANCH
    tmpdir = tmpdir_factory.mktemp('west-check-git-caps-tmpdir')
    try:
        subprocess.run(
            [GIT, 'init', '--initial-branch', 'foo', os.fspath(tmpdir)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        GIT_INIT_HAS_BRANCH = True
    except subprocess.CalledProcessError:
        pass


@pytest.fixture(autouse=True)
def setup_teardown_test_environment(tmpdir_factory):
    '''Per-test isolation. Switches cwd into a fresh tempdir and
    points `WEST_CONFIG_SYSTEM` / `WEST_CONFIG_GLOBAL` at tempfiles
    so tests never touch the developer's real config. `ZEPHYR_BASE`
    is set to a deliberately-bogus path to surface accidental reads
    against it.'''
    tmpdir = Path(tmpdir_factory.mktemp('test-configs'))
    tmp_cwd = tmpdir_factory.mktemp('tmp-cwd')
    system = tmpdir / 'config.system'
    glbl = tmpdir / 'config.global'
    with (
        chdir(tmp_cwd),
        update_env({
            'WEST_CONFIG_SYSTEM': str(system),
            'WEST_CONFIG_GLOBAL': str(glbl),
            'WEST_CONFIG_LOCAL': None,
            'ZEPHYR_BASE': str(tmpdir / 'no-zephyr-here'),
        }),
    ):
        yield


# =========================================================================
# Test fixtures
# =========================================================================


@pytest.fixture
def config_tmpdir(tmpdir):
    '''Per-test cwd-and-WEST_CONFIG_LOCAL sandbox. Lighter than
    `west_init_tmpdir` — no manifest repo, just a tempdir with
    `WEST_CONFIG_LOCAL` pointed inside it. Useful for tests that
    only exercise the configuration layer.'''
    local_config = tmpdir / 'config.local'
    with chdir(tmpdir), update_env({'WEST_CONFIG_LOCAL': str(local_config)}):
        yield tmpdir


# Manifest template materialised by `repos_tmpdir`. The `url-base`
# value `THE_URL_BASE` is substituted with `file://<tmpdir>/repos`
# at fixture time; everything else is verbatim. The shape mirrors
# the legacy `tests-legacy/conftest.py` YAML literal so migrating
# tests don't need to re-derive what's at which path.
#
# Stored as a dict so `repos_tmpdir` can render it to YAML, JSON, or
# TOML on demand via the parametric `manifest_format` fixture. All
# three serializers round-trip the same `Manifest` per
# `parse_yaml_toml_json_equivalent` in west-core.
_MANIFEST_DATA = {
    'manifest': {
        'defaults': {'remote': 'test-local'},
        'remotes': [
            {'name': 'test-local', 'url-base': 'THE_URL_BASE'},
        ],
        'projects': [
            {
                'name': 'Kconfiglib',
                'description': (
                    'Kconfiglib is an implementation of\nthe Kconfig language written in Python.\n'
                ),
                'revision': 'zephyr',
                'path': 'subdir/Kconfiglib',
                'groups': ['Kconfiglib-group'],
                'submodules': True,
            },
            {'name': 'tagged_repo', 'revision': 'v1.0'},
            {
                'name': 'net-tools',
                'description': 'Networking tools.',
                'clone-depth': 1,
                'west-commands': 'scripts/west-commands.yml',
            },
        ],
        'self': {'path': 'zephyr'},
    },
}


@pytest.fixture
def manifest_format(request):
    '''Parametric fixture covering the three manifest input formats
    west accepts. Defaults to `'yaml'`; tests opt into broader
    coverage with `@pytest.mark.parametrize('manifest_format',
    ['yaml', 'toml', 'json'], indirect=True)`. The `repos_tmpdir`,
    `west_init_tmpdir`, and `west_update_tmpdir` fixtures all
    consume this value transitively, so a single decorator runs the
    test against all three formats end-to-end.'''
    return getattr(request, 'param', 'yaml')


@pytest.fixture(scope='session')
def _session_repos(tmp_path_factory):
    '''Session-scoped helper. Don't use directly; tests want
    `repos_tmpdir` / `west_init_tmpdir` / `west_update_tmpdir`.

    Builds the four "remote" git repositories once per session
    (`Kconfiglib`, `tagged_repo`, `net-tools`, `zephyr`) so per-test
    fixtures can clone from them cheaply. Returns the path to the
    directory that holds them.
    '''
    import textwrap

    session_repos = str(tmp_path_factory.mktemp('session_repos'))
    print('initializing session repositories in', session_repos)
    shutil.rmtree(session_repos, ignore_errors=True)

    rp = {}
    for repo in 'Kconfiglib', 'tagged_repo', 'net-tools', 'zephyr':
        path = os.path.join(session_repos, repo)
        rp[repo] = path
        create_repo(path)

    add_commit(
        rp['zephyr'],
        'base zephyr commit',
        files={
            'CODEOWNERS': '',
            'include/header.h': '#pragma once\n',
            'subsys/bluetooth/code.c': 'void foo(void) {}\n',
        },
    )

    create_branch(rp['Kconfiglib'], 'zephyr', checkout=True)
    add_commit(
        rp['Kconfiglib'],
        'test kconfiglib commit',
        files={'kconfiglib.py': 'print("hello world kconfiglib")\n'},
    )

    add_commit(rp['tagged_repo'], 'tagged_repo commit', files={'test.txt': 'hello world'})
    add_tag(rp['tagged_repo'], 'v1.0')

    add_commit(
        rp['net-tools'],
        'test net-tools commit',
        files={
            'qemu-script.sh': 'echo hello world net-tools\n',
            'scripts/west-commands.yml': textwrap.dedent('''\
                west-commands:
                - file: scripts/test.py
                  commands:
                  - name: test-extension
                    class: TestExtension
                    help: test-extension-help
                '''),
            'scripts/test.py': textwrap.dedent('''\
                from west.commands import WestCommand
                class TestExtension(WestCommand):
                    def __init__(self):
                        super().__init__('test-extension',
                                         description='description of test extension')
                    def do_add_parser(self, parser_adder):
                        parser = parser_adder.add_parser(self.name)
                        return parser
                    def do_run(self, args, ignored):
                        print('Testing test command 1')
                '''),
        },
    )

    print('finished initializing session repositories')
    return session_repos


@pytest.fixture
def repos_tmpdir(tmpdir, _session_repos, manifest_format):
    '''Per-test "remote" repos cloned from `_session_repos`.

    Layout after this fixture runs:

      <tmpdir>/repos/
      ├── Kconfiglib (branch: zephyr)
      ├── tagged_repo (branch: master, tag: v1.0)
      ├── net-tools (branch: master)
      └── zephyr (branch: master) — manifest repo with
                                    `west.{yml,toml,json}`

    The manifest is `_MANIFEST_DATA` rendered to the format chosen
    by the `manifest_format` fixture (default: `'yaml'`); the
    `url-base` placeholder is substituted with `file://<tmpdir>/repos`
    on the dict, *before* serialization, so the result is well-formed
    for all three formats. Returns `tmpdir` (NOT `tmpdir/repos`) so
    workspace-building fixtures can compose.
    '''
    kconfiglib, tagged_repo, net_tools, zephyr = (
        os.path.join(_session_repos, x)
        for x in ['Kconfiglib', 'tagged_repo', 'net-tools', 'zephyr']
    )
    repos = tmpdir.mkdir('repos')
    repos.chdir()
    for r in [kconfiglib, tagged_repo, net_tools, zephyr]:
        subprocess.check_call([GIT, 'clone', r])

    # Deep-copy the template so per-test substitutions don't leak
    # into the next test (test order is non-deterministic under
    # pytest-xdist).
    import copy

    data = copy.deepcopy(_MANIFEST_DATA)
    data['manifest']['remotes'][0]['url-base'] = str(tmpdir.join('repos'))
    ext = _MANIFEST_FORMATS[manifest_format]['ext']
    manifest_file = f'west.{ext}'
    add_commit(
        str(repos.join('zephyr')),
        'add manifest',
        files={manifest_file: _dump_manifest(data, manifest_format)},
    )
    return tmpdir


@pytest.fixture
def west_init_tmpdir(repos_tmpdir, manifest_format):
    '''Per-test workspace initialized via `west init` against the
    `repos_tmpdir` fixture's local "remote" manifest repo.

    Workspace lives at `<repos_tmpdir>/workspace`; chdirs into it
    and yields the path. The workspace's projects are NOT cloned —
    use `west_update_tmpdir` if you need them on disk.

    `--manifest-file west.<ext>` is passed at init time so the
    workspace's `manifest.file` config matches the format the
    `repos_tmpdir` fixture committed to the bare manifest repo.
    '''
    west_tmpdir = repos_tmpdir / 'workspace'
    manifest = repos_tmpdir / 'repos' / 'zephyr'
    ext = _MANIFEST_FORMATS[manifest_format]['ext']
    cmd([
        'init',
        '--url',
        f'file://{manifest}',
        '--manifest-file',
        f'west.{ext}',
        str(west_tmpdir),
    ])
    with chdir(west_tmpdir):
        yield west_tmpdir


@pytest.fixture
def west_update_tmpdir(west_init_tmpdir):
    '''Like `west_init_tmpdir`, plus a `west update` to clone all
    projects defined in the manifest.'''
    cmd('update', cwd=west_init_tmpdir)
    return west_init_tmpdir


# =========================================================================
# `west` CLI helpers
# =========================================================================


def _run_west(argv, cwd=None, env=None) -> subprocess.CompletedProcess:
    if isinstance(argv, str):
        argv = shlex.split(argv)
    argv = [str(a) for a in argv]
    with update_env(env or {}):
        return subprocess.run(
            [_WEST_BIN] + argv,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            check=False,
        )


def cmd(cmd, cwd=None, stderr: io.StringIO | None = None, env=None):
    '''Run a west command and return its captured stdout.

    On non-zero exit raises `SystemExit(returncode)`. If `stderr`
    is an `io.StringIO`, the subprocess's stderr is written there;
    otherwise stderr goes to the test process's `sys.stderr` (where
    pytest's `capsys` / `capfd` will catch it).
    '''
    result = _run_west(cmd, cwd=cwd, env=env)
    if stderr is not None:
        stderr.write(result.stderr)
    else:
        sys.stderr.write(result.stderr)
    if result.returncode != 0:
        raise SystemExit(result.returncode)
    return result.stdout


def cmd_raises(cmd, expected_exception_type, stdout=None, cwd=None, env=None):
    '''Run a west command expected to fail. Returns
    `(exc_info, stderr_str)` per the legacy contract.

    `SystemExit(returncode)` is synthesized inside
    `pytest.raises(...)` so the captured `exc_info` matches what
    in-process `main.main()` produced: tests checking
    `exc_info.value.code` see the int return code; tests
    checking error text use the returned `stderr_str`.
    '''
    result = _run_west(cmd, cwd=cwd, env=env)
    if stdout is not None:
        stdout.write(result.stdout)
    with pytest.raises(expected_exception_type) as exc_info:
        if result.returncode != 0:
            raise SystemExit(result.returncode)
        pytest.fail(f'west {cmd!r} unexpectedly succeeded (exit 0)')
    return exc_info, result.stderr


# =========================================================================
# Helper functions
# =========================================================================


def check_output(*args, **kwargs):
    '''Like `subprocess.check_output`, but returns a decoded string
    and prints diagnostics on non-zero exit.'''
    try:
        out_bytes = subprocess.check_output(*args, **kwargs)
    except subprocess.CalledProcessError as e:
        print('*** check_output: nonzero return code', e.returncode, file=sys.stderr)
        print('cwd =', os.getcwd(), 'args =', args, 'kwargs =', kwargs, file=sys.stderr)
        print('subprocess output:', file=sys.stderr)
        print(e.output.decode(), file=sys.stderr)
        raise
    return out_bytes.decode(sys.getdefaultencoding())


def create_workspace(workspace_dir, and_git=True):
    '''Build a bare-bones west workspace under `workspace_dir`:
    `.west/config.toml` with `manifest.path = "mp"`, an `mp/` directory,
    and (if `and_git`) an initialized git repo inside `mp/`.'''
    if not os.path.isdir(workspace_dir):
        workspace_dir.mkdir()
    dot_west = workspace_dir / '.west'
    dot_west.mkdir()
    with open(dot_west / 'config.toml', 'w') as f:
        f.write('[manifest]\npath = "mp"\n')
    mp = workspace_dir / 'mp'
    mp.mkdir()
    if and_git:
        create_repo(mp)


def create_repo(path, initial_branch='master'):
    '''Initialize a git repo at `path` on branch `initial_branch`
    and land an empty initial commit. The default branch name
    matches the legacy fixtures so tests that hard-coded `master`
    keep working; pass `initial_branch='main'` when starting
    fresh.'''
    path = os.fspath(path)
    if GIT_INIT_HAS_BRANCH:
        subprocess.check_call([GIT, 'init', '--initial-branch', initial_branch, path])
    else:
        subprocess.check_call([GIT, 'init', path])
        # `-B` rather than `-b`: on older git (e.g. Ubuntu 20.04's
        # 2.25.1) `git init <path>` may have already created the
        # branch, which plain `-b` would reject as duplicate.
        subprocess.check_call([GIT, 'checkout', '-B', initial_branch], cwd=path)
    config_repo(path)
    add_commit(path, f'initial {uuid.uuid4()}')


def config_repo(path):
    '''Set per-repo user.name and user.email so commits don't fail
    on machines without global git identity.'''
    subprocess.check_call([GIT, 'config', 'user.name', 'West Test'], cwd=path)
    subprocess.check_call([GIT, 'config', 'user.email', 'west-test@example.com'], cwd=path)


def create_branch(path, branch, checkout=False):
    subprocess.check_call([GIT, 'branch', branch], cwd=path)
    if checkout:
        checkout_branch(path, branch)


def checkout_branch(path, branch, detach=False):
    detach = ['--detach'] if detach else []
    subprocess.check_call([GIT, 'checkout', branch] + detach, cwd=path)


def add_commit(repo, msg, files=None, reconfigure=True):
    '''Commit `msg` to `repo`. `files` is an optional
    `{relative-path: contents}` map; missing files are written, then
    `git add`-ed. `--allow-empty` so empty `files=None` calls still
    produce a commit. The cascade of `--no-*` flags neutralizes
    developer-machine git config (sign / verify / post-rewrite
    hooks) that would otherwise prompt or fail.'''
    repo = os.fspath(repo)
    if reconfigure:
        config_repo(repo)
    if files:
        for path, contents in files.items():
            if not isinstance(path, str):
                path = str(path)
            dirname, basename = os.path.dirname(path), os.path.basename(path)
            fulldir = os.path.join(repo, dirname)
            if not os.path.isdir(fulldir):
                os.makedirs(fulldir)
            with open(os.path.join(fulldir, basename), 'w') as f:
                f.write(contents)
            subprocess.check_call([GIT, 'add', path], cwd=repo)
    subprocess.check_call(
        [
            GIT,
            'commit',
            '-a',
            '--allow-empty',
            '-m',
            msg,
            '--no-verify',
            '--no-gpg-sign',
            '--no-post-rewrite',
        ],
        cwd=repo,
    )


def add_tag(repo, tag, commit='HEAD', msg=None):
    repo = os.fspath(repo)
    if msg is None:
        msg = 'tag ' + tag
    # `--no-sign` overrides `tag.gpgSign=true` if set globally.
    subprocess.check_call([GIT, 'tag', '-m', msg, '--no-sign', tag, commit], cwd=repo)


def remote_get_url(repo, remote='origin'):
    repo = os.fspath(repo)
    out = subprocess.check_output([GIT, 'remote', 'get-url', remote], cwd=repo)
    return out.decode(sys.getdefaultencoding()).strip()


def rev_parse(repo, revision):
    repo = os.fspath(repo)
    out = subprocess.check_output([GIT, 'rev-parse', revision], cwd=repo)
    return out.decode(sys.getdefaultencoding()).strip()


def rev_list(repo):
    repo = os.fspath(repo)
    out = subprocess.check_output([GIT, 'rev-list', '--all'], cwd=repo)
    return out.decode(sys.getdefaultencoding()).strip()


def check_proj_consistency(actual, expected):
    '''Assert that two `Project` instances are equal across the
    fields the test suite cares about, with extra invariants
    (paths must be absolute, posixpath/abspath must round-trip
    cleanly).'''
    assert actual.name == expected.name
    assert actual.path == expected.path
    if actual.topdir is None or expected.topdir is None:
        assert actual.topdir is None and expected.topdir is None
        assert actual.abspath is None and expected.abspath is None
        assert actual.posixpath is None and expected.posixpath is None
    else:
        assert actual.topdir and actual.abspath and actual.posixpath
        assert expected.topdir and expected.abspath and expected.posixpath
        a_top, e_top = PurePath(actual.topdir), PurePath(expected.topdir)
        a_abs, e_abs = PurePath(actual.abspath), PurePath(expected.abspath)
        a_psx, e_psx = PurePath(actual.posixpath), PurePath(expected.posixpath)
        assert a_top.is_absolute()
        assert e_top.is_absolute()
        assert a_abs.is_absolute()
        assert e_abs.is_absolute()
        assert a_psx.is_absolute()
        assert e_psx.is_absolute()
        assert a_top == e_top
        assert a_abs == e_abs
        assert a_psx == e_psx
    assert actual.url == expected.url or (
        WINDOWS and Path(expected.url).is_dir() and (PurePath(actual.url) == PurePath(expected.url))
    )
    assert actual.clone_depth == expected.clone_depth
    assert actual.revision == expected.revision
    assert actual.west_commands == expected.west_commands
