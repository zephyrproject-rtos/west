# Copyright (c) 2019, 2020 Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

import contextlib
import io
import os
import platform
import shutil
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path, PurePath

import pytest

from west.app import main

GIT = shutil.which('git')
assert GIT and Path(GIT).exists()

# Git capabilities are discovered at runtime in
# _check_git_capabilities().

# This will be set to True if 'git init --branch' is available.
#
# This feature was available from git release v2.28 and was added in
# commit 32ba12dab2acf1ad11836a627956d1473f6b851a ("init: allow
# specifying the initial branch name for the new repository") as part
# of the git community's choice to avoid a default initial branch
# name.
GIT_INIT_HAS_BRANCH = False

# If you change this, keep the docstring in repos_tmpdir() updated also.
MANIFEST_TEMPLATE = '''\
manifest:
  defaults:
    remote: test-local

  remotes:
    - name: test-local
      url-base: THE_URL_BASE

  projects:
    - name: Kconfiglib
      description: |
        Kconfiglib is an implementation of
        the Kconfig language written in Python.
      revision: zephyr
      path: subdir/Kconfiglib
      groups:
        - Kconfiglib-group
      submodules: true
    - name: tagged_repo
      revision: v1.0
    - name: net-tools
      description: Networking tools.
      clone-depth: 1
      west-commands: scripts/west-commands.yml
  self:
    path: zephyr
'''

WINDOWS = platform.system() == 'Windows'

#
# Contextmanager
#


@contextlib.contextmanager
def tmp_west_topdir(path: str | Path):
    """
    Temporarily create a west topdir for the duration of the `with` block by
    creating a .west directory at given path. The directory is removed again
    when the `with` block exits.
    """
    west_dir = Path(path) / '.west'
    west_dir.mkdir(parents=True)
    try:
        yield
    finally:
        # remove the directory (must be empty)
        west_dir.rmdir()


@contextlib.contextmanager
def update_env(env: dict[str, str | None]):
    """
    Temporarily update the process environment variables.
    This context manager updates `os.environ` with the key-value pairs
    provided in the `env` dictionary for the duration of the `with` block.
    The existing environment is preserved and fully restored when the block
    exits. If the value is set to None, the environment variable is unset.
    """
    env_bak = dict(os.environ)
    env_vars = {}
    for k, v in env.items():
        # unset if value is None
        if v is None and k in os.environ:
            del os.environ[k]
        # set env variable to new value only if v is not None
        elif v is not None:
            env_vars[k] = v
    # apply the new environment
    os.environ.update(env_vars)
    try:
        yield
    finally:
        # reset to previous environment
        os.environ.clear()
        os.environ.update(env_bak)


@contextlib.contextmanager
def chdir(path):
    """
    Temporarily change the current working directory.
    This context manager changes the current working directory to `path`
    for the duration of the `with` block. After the block exits, the
    working directory is restored to its original value.
    """
    oldpwd = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(oldpwd)


#
# Test fixtures (autouse=True)
#


@pytest.fixture(scope='session', autouse=True)
def _check_git_capabilities(tmpdir_factory):
    # Do checks for git behaviors. Right now this is limited to
    # deciding whether or not 'git init --branch' is supported.
    #
    # We aren't using WestCommand._parse_git_version() here just to
    # try to keep the conftest behavior independent of the code being
    # tested.
    global GIT_INIT_HAS_BRANCH

    tmpdir = tmpdir_factory.mktemp("west-check-git-caps-tmpdir")

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
    """
    The fixture ensures an isolated test environment.

    It creates a new temporary directory which is used as working directory.
    This ensures a clean start for each test and prevents tests from affecting
    another one through changes to the working directory.

    The fixture ensures that the user's actual configuration files are neither
    used nor touched during test, as WEST_CONFIG_* env variables are set,
    whereby no config files are created at these locations.

    The fixture sets ZEPHYR_BASE (to avoid complaints in subcommand stderr),
    but to a spurious location (so that attempts to read from inside of it are
    caught here).

    The fixture also ensures that any environment modifications made by a test
    do not leak into subsequent tests, as the environment is restored when the
    `update_env` with-block exits.
    """
    tmpdir = Path(tmpdir_factory.mktemp("test-configs"))
    tmp_cwd = tmpdir_factory.mktemp("tmp-cwd")

    # config paths
    system = tmpdir / 'config.system'
    glbl = tmpdir / 'config.global'

    # run with environment variables set
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


#
# Test fixtures
#


@pytest.fixture(scope='session')
def _session_repos(tmp_path_factory):
    '''Just a helper, do not use directly.'''

    # It saves time to create repositories once at session scope, then
    # clone the results as needed in per-test fixtures.
    session_repos = str(tmp_path_factory.mktemp('session_repos'))
    print('initializing session repositories in', session_repos)
    shutil.rmtree(session_repos, ignore_errors=True)

    # Create the repositories.
    rp = {}  # individual repository paths
    for repo in 'Kconfiglib', 'tagged_repo', 'net-tools', 'zephyr':
        path = os.path.join(session_repos, repo)
        rp[repo] = path
        create_repo(path)

    # Initialize the "zephyr" repository.
    # The caller needs to add west.yml with the right url-base.
    add_commit(
        rp['zephyr'],
        'base zephyr commit',
        files={
            'CODEOWNERS': '',
            'include/header.h': '#pragma once\n',
            'subsys/bluetooth/code.c': 'void foo(void) {}\n',
        },
    )

    # Initialize the Kconfiglib repository.
    create_branch(rp['Kconfiglib'], 'zephyr', checkout=True)
    add_commit(
        rp['Kconfiglib'],
        'test kconfiglib commit',
        files={'kconfiglib.py': 'print("hello world kconfiglib")\n'},
    )

    # Initialize the tagged_repo repository.
    add_commit(rp['tagged_repo'], 'tagged_repo commit', files={'test.txt': 'hello world'})
    add_tag(rp['tagged_repo'], 'v1.0')

    # Initialize the net-tools repository.
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
                                         'test-extension-help',
                                         '')
                    def do_add_parser(self, parser_adder):
                        parser = parser_adder.add_parser(self.name)
                        return parser
                    def do_run(self, args, ignored):
                        print('Testing test command 1')
                '''),
        },
    )

    # Return the top-level temporary directory. Don't clean it up on
    # teardown, so the contents can be inspected post-portem.
    print('finished initializing session repositories')
    return session_repos


@pytest.fixture
def repos_tmpdir(tmpdir, _session_repos):
    '''Fixture for tmpdir with "remote" repositories.

    These can then be used to bootstrap a workspace and run
    project-related commands on it with predictable results.

    Switches directory to, and returns, the top level tmpdir -- NOT
    the subdirectory containing the repositories themselves.

    Initializes placeholder upstream repositories in tmpdir with the
    following contents:

    repos/
    ├── Kconfiglib (branch: zephyr)
    │   └── kconfiglib.py
    ├── tagged_repo (branch: master, tag: v1.0)
    │   └── test.txt
    ├── net-tools (branch: master)
    │   └── qemu-script.sh
    └── zephyr (branch: master)
        ├── CODEOWNERS
        ├── west.yml
        ├── include
        │   └── header.h
        └── subsys
            └── bluetooth
                └── code.c

    The contents of west.yml are:

    manifest:
      defaults:
        remote: test-local
      remotes:
        - name: test-local
          url-base: file://<tmpdir>/repos
      projects:
        - name: Kconfiglib
          revision: zephyr
          path: subdir/Kconfiglib
          submodules: true
        - name: tagged_repo
          revision: v1.0
        - name: net-tools
          clone-depth: 1
          west-commands: scripts/west-commands.yml
      self:
        path: zephyr

    '''
    kconfiglib, tagged_repo, net_tools, zephyr = [
        os.path.join(_session_repos, x)
        for x in ['Kconfiglib', 'tagged_repo', 'net-tools', 'zephyr']
    ]
    repos = tmpdir.mkdir('repos')
    repos.chdir()
    for r in [kconfiglib, tagged_repo, net_tools, zephyr]:
        subprocess.check_call([GIT, 'clone', r])

    manifest = MANIFEST_TEMPLATE.replace('THE_URL_BASE', str(tmpdir.join('repos')))
    add_commit(str(repos.join('zephyr')), 'add manifest', files={'west.yml': manifest})
    return tmpdir


@pytest.fixture
def west_init_tmpdir(repos_tmpdir):
    '''Fixture for a tmpdir with 'remote' repositories and 'west init' run.

    Uses the remote repositories from the repos_tmpdir fixture to
    create a west workspace using west init.

    The contents of the west workspace aren't checked at all.
    This is left up to the test cases.

    The directory that 'west init' created is returned as a
    py.path.local, with the current working directory set there.'''
    west_tmpdir = repos_tmpdir / 'workspace'
    manifest = repos_tmpdir / 'repos' / 'zephyr'
    cmd(['init', '-m', str(manifest), str(west_tmpdir)])
    with chdir(west_tmpdir):
        yield west_tmpdir


@pytest.fixture
def west_update_tmpdir(west_init_tmpdir):
    '''Like west_init_tmpdir, but also runs west update.'''
    cmd('update', cwd=west_init_tmpdir)
    return west_init_tmpdir


@pytest.fixture
def config_tmpdir(tmpdir):
    # Fixture for running from a temporary directory with
    # environmental overrides in place so local configuration file
    # live inside of it. This ensures we don't touch the user's local config.
    #
    # Using this makes the tests run faster than if we used
    # west_init_tmpdir from conftest.py, and also ensures that the
    # configuration code doesn't depend on features like the existence
    # of a manifest file, helping separate concerns.

    # determine a local config in tmp dir
    local_config = tmpdir / 'config.local'

    # run the test within tmpdir and with env variable set
    with chdir(tmpdir), update_env({'WEST_CONFIG_LOCAL': str(local_config)}):
        yield tmpdir


#
# Helper functions
#


def check_output(*args, **kwargs):
    # Like subprocess.check_output, but returns a string in the
    # default encoding instead of a byte array.
    try:
        out_bytes = subprocess.check_output(*args, **kwargs)
    except subprocess.CalledProcessError as e:
        print('*** check_output: nonzero return code', e.returncode, file=sys.stderr)
        print('cwd =', os.getcwd(), 'args =', args, 'kwargs =', kwargs, file=sys.stderr)
        print('subprocess output:', file=sys.stderr)
        print(e.output.decode(), file=sys.stderr)
        raise
    return out_bytes.decode(sys.getdefaultencoding())


def _cmd(cmd, cwd=None, env=None):
    # Executes a west command by invoking the `main()` function with the
    # provided command arguments.
    # Parameters:
    #   cwd: The working directory in which to execute the command.
    #   env: A dictionary of extra environment variables to apply temporarily
    #        during execution.

    # ensure that cmd is a list of strings
    cmd = cmd.split() if isinstance(cmd, str) else cmd
    cmd = [str(c) for c in cmd]

    # run main()
    with (
        chdir(cwd or Path.cwd()),
        update_env(env or {}),
    ):
        try:
            main.main(cmd)
        except SystemExit as e:
            if e.code:
                raise e
        except Exception as e:
            print(f'Uncaught exception type {e}', file=sys.stderr)
            raise e


def cmd(cmd: list | str, cwd=None, stderr: io.StringIO | None = None, env=None):
    # Same as _cmd(), but it captures and returns combined stdout and stderr.
    # Optionally stderr can be captured separately into given stderr.
    # Note that this function does not capture any stdout or stderr from an
    # internally invoked subprocess.
    stdout_buf = io.StringIO()
    stderr_buf = stderr or stdout_buf
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        _cmd(cmd, cwd, env)
    return stdout_buf.getvalue()


def cmd_raises(cmd: list | str, expected_exception_type, stdout=None, cwd=None, env=None):
    # Similar to '_cmd' but an expected exception is caught.
    # The exception is returned together with stderr.
    # Optionally stdout is captured into given stdout (io.StringIO)
    stdout_buf = stdout or sys.stdout
    stderr_buf = io.StringIO()
    with (
        contextlib.redirect_stdout(stdout_buf),
        contextlib.redirect_stderr(stderr_buf),
        pytest.raises(expected_exception_type) as exc_info,
    ):
        _cmd(cmd, cwd=cwd, env=env)
    return exc_info, stderr_buf.getvalue()


def cmd_subprocess(cmd: list | str, *args, **kwargs):
    # This function behaves similarly to `cmd()`, but executes the command in a
    # separate Python subprocess, capturing all stdout output.
    # The captured stdout includes both Python-level output and the output of
    # any subprocesses spawned internally. This makes the function particularly
    # useful in test cases where the code under test launches subprocesses and
    # the combined stdout needs to be verified.
    # The main drawback is that it cannot be debugged within Python, so it
    # should only be used sparingly in tests.
    cmd = cmd if isinstance(cmd, list) else cmd.split()
    cmd = [sys.executable, main.__file__] + cmd
    print('running (subprocess):', cmd)
    ret = check_output(cmd, *args, **kwargs)
    return ret


def create_workspace(workspace_dir, and_git=True):
    # Manually create a bare-bones west workspace inside
    # workspace_dir. The manifest.path config option is 'mp'. The
    # manifest repository directory is created, and the git
    # repository inside is initialized unless and_git is False.
    if not os.path.isdir(workspace_dir):
        workspace_dir.mkdir()
    dot_west = workspace_dir / '.west'
    dot_west.mkdir()
    with open(dot_west / 'config', 'w') as f:
        f.write('[manifest]\npath = mp')
    mp = workspace_dir / 'mp'
    mp.mkdir()
    if and_git:
        create_repo(mp)


def create_repo(path, initial_branch='master'):
    # Initializes a Git repository in 'path', and adds an initial
    # commit to it in a new branch 'initial_branch'. We're currently
    # keeping the old default initial branch to keep assumptions made
    # elsewhere in the test code working with newer versions of git.
    path = os.fspath(path)

    if GIT_INIT_HAS_BRANCH:
        subprocess.check_call([GIT, 'init', '--initial-branch', initial_branch, path])
    else:
        subprocess.check_call([GIT, 'init', path])
        # -B instead of -b because on some versions of git (at
        # least 2.25.1 as shipped by Ubuntu 20.04), if 'git init path'
        # created an 'initial_branch' already, we get errors that it
        # already exists with plain '-b'.
        subprocess.check_call([GIT, 'checkout', '-B', initial_branch], cwd=path)

    config_repo(path)
    # make an individual commit to ensure a unique commit id
    add_commit(path, f'initial {uuid.uuid4()}')


def config_repo(path):
    # Set name and email. This avoids a "Please tell me who you are" error when
    # there's no global default.
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
    # Adds a commit with message 'msg' to the repo in 'repo'
    #
    # If 'files' is given, it must be a dictionary mapping files to
    # edit to the contents they should contain in the new
    # commit. Otherwise, the commit will be empty.
    #
    # If 'reconfigure' is True, the user.name and user.email git
    # configuration variables will be set in 'repo' using config_repo().
    repo = os.fspath(repo)

    if reconfigure:
        config_repo(repo)

    # Edit any files as specified by the user and add them to the index.
    if files:
        for path, contents in files.items():
            if not isinstance(path, str):
                path = str(path)
            dirname, basename = os.path.dirname(path), os.path.basename(path)
            fulldir = os.path.join(repo, dirname)
            if not os.path.isdir(fulldir):
                # Allow any errors (like trying to create a directory
                # where a file already exists) to propagate up.
                os.makedirs(fulldir)
            with open(os.path.join(fulldir, basename), 'w') as f:
                f.write(contents)
            subprocess.check_call([GIT, 'add', path], cwd=repo)

    # The extra '--no-xxx' flags are for convenience when testing
    # on developer workstations, which may have global git
    # configuration to sign commits, etc.
    #
    # We don't want any of that, as it could require user
    # intervention or fail in environments where Git isn't
    # configured.
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

    # Override tag.gpgSign with --no-sign, in case the test
    # environment has that set to true.
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
    # Check equality of all project fields (projects themselves are
    # not comparable), with extra semantic consistency checking
    # for paths.
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
