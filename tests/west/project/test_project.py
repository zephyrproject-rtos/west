import os
from os.path import dirname
import shlex
import shutil
import subprocess
import sys
import textwrap

import pytest

from west import config

GIT = shutil.which('git')
# Assumes this file is west/tests/west/project/test_project.py, returns
# path to toplevel 'west'
THIS_WEST = os.path.abspath(dirname(dirname(dirname(dirname(__file__)))))


#
# Test fixtures
#

@pytest.fixture
def repos_tmpdir(tmpdir):
    '''Fixture for tmpdir with "remote" repositories, manifest, and west.

    These can then be used to bootstrap an installation and run
    project-related commands on it with predictable results.

    Switches directory to, and returns, the top level tmpdir -- NOT
    the subdirectory containing the repositories themselves.

    Initializes placeholder upstream repositories in <tmpdir>/remote-repos/
    with the following contents:

    repos/
    ├── west (branch: master)
    │   └── (contains this west's worktree contents)
    ├── manifest (branch: master)
    │   └── west.yml
    ├── Kconfiglib (branch: zephyr)
    │   └── kconfiglib.py
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

    west:
      url: file://<tmpdir>/west
    manifest:
      defaults:
        remote: test-local
      remotes:
        - name: test-local
          url-base: file://<tmpdir>/remote-repos
      projects:
        - name: Kconfiglib
          revision: zephyr
          path: subdir/Kconfiglib
        - name: net-tools
          clone_depth: 1
      self:
        path: zephyr
    '''
    rr = tmpdir.mkdir('repos')  # "remote" repositories
    rp = {}                     # individual repository paths under rr

    # Mirror this west tree into a "remote" west repository under rr.
    wdst = rr.join('west')
    mirror_west_repo(wdst)
    rp['west'] = str(wdst)

    # Create the other repositories.
    for repo in 'net-tools', 'Kconfiglib', 'zephyr':
        path = str(rr.join(repo))
        rp[repo] = path
        create_repo(path)

    # Initialize the manifest repository.
    add_commit(rp['zephyr'], 'test manifest',
               files={'west.yml': textwrap.dedent('''\
                      west:
                        url: file://{west}
                      manifest:
                        defaults:
                          remote: test-local

                        remotes:
                          - name: test-local
                            url-base: file://{rr}

                        projects:
                          - name: Kconfiglib
                            revision: zephyr
                            path: subdir/Kconfiglib
                          - name: net-tools
                        self:
                          path: zephyr
                      '''.format(west=rp['west'], rr=str(rr)))})

    # Initialize the Kconfiglib repository.
    subprocess.check_call([GIT, 'checkout', '-b', 'zephyr'],
                          cwd=rp['Kconfiglib'])
    add_commit(rp['Kconfiglib'], 'test kconfiglib commit',
               files={'kconfiglib.py': 'print("hello world kconfiglib")\n'})

    # Initialize the net-tools repository.
    add_commit(rp['net-tools'], 'test net-tools commit',
               files={'qemu-script.sh': 'echo hello world net-tools\n'})

    # Initialize the zephyr repository.
    add_commit(rp['zephyr'], 'test zephyr commit',
               files={'CODEOWNERS': '',
                      'include/header.h': '#pragma once\n',
                      'subsys/bluetooth/code.c': 'void foo(void) {}\n'})

    # Switch to and return the top-level temporary directory.
    #
    # This can be used to populate a west installation alongside.

    # Switch to the top-level West installation directory
    tmpdir.chdir()
    return tmpdir


@pytest.fixture
def west_init_tmpdir(repos_tmpdir):
    '''Fixture for a tmpdir with 'remote' repositories and 'west init' run.

    Uses the remote repositories from the repos_tmpdir fixture to
    create a west installation using the system bootstrapper's init
    command -- and thus the test environment must install the
    bootstrapper from the current west source code tree under test.

    The contents of the west installation aren't checked at all.
    This is left up to the test cases.

    The directory that 'west init' created is returned as a
    py.path.local, with the current working directory set there.'''
    west_tmpdir = repos_tmpdir.join('west_installation')
    cmd('init -m "{}" "{}"'.format(str(repos_tmpdir.join('repos', 'zephyr')),
                                   str(west_tmpdir)))
    west_tmpdir.chdir()
    config.read_config()
    return west_tmpdir


@pytest.fixture
def west_update_tmpdir(west_init_tmpdir):
    '''Like west_init_tmpdir, but also runs west update.'''
    cmd('update', cwd=str(west_init_tmpdir))
    return west_init_tmpdir


#
# Test cases
#

def test_installation(west_update_tmpdir):
    # Basic test that west_update_tmpdir bootstrapped correctly. This
    # is a basic test of west init and west update.

    # Make sure the expected files and directories exist in the right
    # places.
    wct = west_update_tmpdir
    assert wct.check(dir=1)
    assert wct.join('subdir', 'Kconfiglib').check(dir=1)
    assert wct.join('subdir', 'Kconfiglib', '.git').check(dir=1)
    assert wct.join('subdir', 'Kconfiglib', 'kconfiglib.py').check(file=1)
    assert wct.join('net-tools').check(dir=1)
    assert wct.join('net-tools', '.git').check(dir=1)
    assert wct.join('net-tools', 'qemu-script.sh').check(file=1)
    assert wct.join('zephyr').check(dir=1)
    assert wct.join('zephyr', '.git').check(dir=1)
    assert wct.join('zephyr', 'CODEOWNERS').check(file=1)
    assert wct.join('zephyr', 'include', 'header.h').check(file=1)
    assert wct.join('zephyr', 'subsys', 'bluetooth', 'code.c').check(file=1)


def test_list(west_update_tmpdir):
    # Projects shall be listed in the order they appear in the manifest.
    # Check the behavior for some format arguments of interest as well.
    actual = cmd('list -f "{name} {revision} {path} {cloned} {clone_depth}"')
    expected = ['zephyr (not set) zephyr (cloned) None',
                'Kconfiglib zephyr {} (cloned) None'.format(
                    os.path.join('subdir', 'Kconfiglib')),
                'net-tools master net-tools (cloned) None']
    assert actual.splitlines() == expected


def test_diff(west_init_tmpdir):
    # FIXME: Check output

    # Diff with no projects cloned shouldn't fail

    cmd('diff')

    # Neither should it fail after fetching one or both projects

    cmd('update net-tools')
    cmd('diff')

    cmd('update Kconfiglib')
    cmd('diff --cached')  # Pass a custom flag too


def test_status(west_init_tmpdir):
    # FIXME: Check output

    # Status with no projects cloned shouldn't fail

    cmd('status')

    # Neither should it fail after fetching one or both projects

    cmd('update net-tools')
    cmd('status')

    cmd('update Kconfiglib')
    cmd('status --long')  # Pass a custom flag too


def test_forall(west_init_tmpdir):
    # FIXME: Check output
    # The 'echo' command is available in both 'shell' and 'batch'

    # 'forall' with no projects cloned shouldn't fail

    cmd("forall -c 'echo *'")

    # Neither should it fail after cloning one or both projects

    cmd('update net-tools')
    cmd("forall -c 'echo *'")

    cmd('update Kconfiglib')
    cmd("forall -c 'echo *'")


def test_update_west(west_init_tmpdir):
    # Test the 'west selfupdate' command. It calls through to the same backend
    # functions that are used for automatic updates and 'west init'
    # reinitialization.

    # update the net-tools repository
    cmd('update net-tools')

    west_prev = head_subject('.west/west')

    # Add commits to the local repos. We need to reconfigure
    # explicitly as these are clones, and west doesn't handle that for
    # us.
    for path in 'zephyr', '.west/west', 'net-tools':
        add_commit(path, 'test-update-local', reconfigure=True)

    # Check that resetting the west repository removes the local commit
    cmd('selfupdate --reset-west')
    assert head_subject('zephyr') == 'test-update-local'  # Unaffected
    assert head_subject('.west/west') == west_prev
    assert head_subject('net-tools') == 'test-update-local'  # Unaffected


def test_update_projects(west_init_tmpdir):
    # Test the 'west update' command. It calls through to the same backend
    # functions that are used for automatic updates and 'west init'
    # reinitialization.

    # update all repositories
    cmd('update')

    # Add commits to the local repos. We need to reconfigure
    # explicitly as these are clones, and west doesn't handle that for
    # us.
    (nt_mr_0, nt_mr_1,
     nt_head_0, nt_head_1,
     kl_mr_0, kl_mr_1,
     kl_head_0, kl_head_1) = update_helper(west_init_tmpdir, 'update')

    assert nt_mr_0 != nt_mr_1, 'failed to update net-tools manifest-rev'
    assert nt_head_0 != nt_head_1, 'failed to update net-tools HEAD'
    assert kl_mr_0 != kl_mr_1, 'failed to update kconfiglib manifest-rev'
    assert kl_head_0 != kl_head_1, 'failed to update kconfiglib HEAD'


def test_update_projects_local_branch_commits(west_init_tmpdir):
    # Test the 'west update' command when working on local branch with local
    # commits and then updating project to upstream commit.
    # It calls through to the same backend functions that are used for
    # automatic updates and 'west init' reinitialization.

    # update all repositories
    cmd('update')

    # Create a local branch and add commits
    checkout_branch('net-tools', 'local_net_tools_test_branch', create=True)
    checkout_branch('subdir/Kconfiglib', 'local_kconfig_test_branch',
                    create=True)
    add_commit('net-tools', 'test local branch commit', reconfigure=True)
    add_commit('subdir/Kconfiglib', 'test local branch commit',
               reconfigure=True)
    net_tools_prev = head_subject('net-tools')
    kconfiglib_prev = head_subject('subdir/Kconfiglib')

    # Add commits to the upstream repos. We need to reconfigure
    # explicitly as these are clones, and west doesn't handle that for
    # us.
    (nt_mr_0, nt_mr_1,
     nt_head_0, nt_head_1,
     kl_mr_0, kl_mr_1,
     kl_head_0, kl_head_1) = update_helper(west_init_tmpdir, 'update')

    assert nt_mr_0 != nt_mr_1, 'failed to update net-tools manifest-rev'
    assert nt_head_0 != nt_head_1, 'failed to update net-tools HEAD'
    assert kl_mr_0 != kl_mr_1, 'failed to update kconfiglib manifest-rev'
    assert kl_head_0 != kl_head_1, 'failed to update kconfiglib HEAD'

    # Verify local branch is still present and untouched
    assert net_tools_prev != head_subject('net-tools')
    assert kconfiglib_prev != head_subject('subdir/Kconfiglib')
    checkout_branch('net-tools', 'local_net_tools_test_branch')
    checkout_branch('subdir/Kconfiglib', 'local_kconfig_test_branch')
    assert net_tools_prev == head_subject('net-tools')
    assert kconfiglib_prev == head_subject('subdir/Kconfiglib')


def test_init_again(west_init_tmpdir):
    # Test that 'west init' on an initialized tmpdir errors out

    with pytest.raises(subprocess.CalledProcessError):
        cmd('init')

    with pytest.raises(subprocess.CalledProcessError):
        cmd('init -m foo')


#
# Helper functions used by the test cases and fixtures.
#

def create_repo(path):
    # Initializes a Git repository in 'path', and adds an initial commit to it

    subprocess.check_call([GIT, 'init', path])

    config_repo(path)
    add_commit(path, 'initial')


def config_repo(path):
    # Set name and email. This avoids a "Please tell me who you are" error when
    # there's no global default.
    subprocess.check_call([GIT, 'config', 'user.name', 'West Test'], cwd=path)
    subprocess.check_call([GIT, 'config', 'user.email',
                           'west-test@example.com'],
                          cwd=path)


def add_commit(repo, msg, files=None, reconfigure=True):
    # Adds a commit with message 'msg' to the repo in 'repo'
    #
    # If 'files' is given, it must be a dictionary mapping files to
    # edit to the contents they should contain in the new
    # commit. Otherwise, the commit will be empty.
    #
    # If 'reconfigure' is True, the user.name and user.email git
    # configuration variables will be set in 'repo' using config_repo().
    repo = str(repo)

    if reconfigure:
        config_repo(repo)

    # Edit any files as specified by the user and add them to the index.
    if files:
        for path, contents in files.items():
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
        [GIT, 'commit', '-a', '--allow-empty', '-m', msg, '--no-verify',
         '--no-gpg-sign', '--no-post-rewrite'], cwd=repo)


def checkout_branch(repo, branch, create=False):
    # Creates a new branch.
    repo = str(repo)

    # Edit any files as specified by the user and add them to the index.
    if create:
        subprocess.check_call([GIT, 'checkout', '-b', branch], cwd=repo)
    else:
        subprocess.check_call([GIT, 'checkout', branch], cwd=repo)


def check_output(*args, **kwargs):
    # Like subprocess.check_output, but returns a string in the
    # default encoding instead of a byte array.
    try:
        out_bytes = subprocess.check_output(*args, **kwargs)
    except subprocess.CalledProcessError as e:
        print('*** check_output: nonzero return code', e.returncode,
              file=sys.stderr)
        print('cwd =', os.getcwd(), 'args =', args,
              'kwargs =', kwargs, file=sys.stderr)
        print('subprocess output:', file=sys.stderr)
        print(e.output.decode(), file=sys.stderr)
        raise
    return out_bytes.decode(sys.getdefaultencoding())


def mirror_west_repo(dst):
    # Create a west repository in dst which mirrors the exact state of
    # the current tree, except ignored files.
    #
    # This is done in a simple way:
    #
    # 1. recursively copy THIS_WEST there (except .git and ignored files)
    # 2. init a new git repository there
    # 3. add the entire tree, and commit
    #
    # (We can't just clone THIS_WEST because we want to allow
    # developers to test their working trees without having to make a
    # commit -- remember, 'west init' clones the remote.)
    wut = str(dst)  # "west under test"

    # Copy the west working tree, except ignored files.
    def ignore(directory, files):
        # Get newline separated list of ignored files, as a string.
        try:
            ignored = check_output([GIT, 'check-ignore'] + files,
                                   cwd=directory)
        except subprocess.CalledProcessError as e:
            # From the manpage: return values 0 and 1 respectively
            # mean that some and no argument files were ignored. These
            # are both OK. Treat other return values as errors.
            if e.returncode not in (0, 1):
                raise
            else:
                ignored = e.output.decode(sys.getdefaultencoding())

        # Convert ignored to a set of file names as strings.
        ignored = set(ignored.splitlines())

        # Also ignore the .git directory itself.
        if '.git' in files:
            ignored.add('.git')

        return ignored
    shutil.copytree(THIS_WEST, wut, ignore=ignore)

    # Create a fresh .git and commit existing directory tree.
    create_repo(wut)
    subprocess.check_call([GIT, 'add', '-A'], cwd=wut)
    add_commit(wut, 'west under test')


def cmd(cmd, cwd=None):
    # Run a west command in a directory (cwd defaults to os.getcwd()).
    #
    # This helper takes the command as a string, which is less clunky
    # to work with than a list. It is split according to shell rules
    # before being run.
    #
    # This helper relies on the test environment to ensure that the
    # 'west' executable is a bootstrapper installed from the current
    # west source code.
    #
    # stdout from cmd is captured and returned. The command is run in
    # a python subprocess so that program-level setup and teardown
    # happen fresh.

    try:
        return check_output(shlex.split('west ' + cmd), cwd=cwd)
    except subprocess.CalledProcessError:
        print('cmd: west:', shutil.which('west'), file=sys.stderr)
        raise


def head_subject(path):
    # Returns the subject of the HEAD commit in the repository at 'path'

    return subprocess.check_output([GIT, 'log', '-n1', '--format=%s'],
                                   cwd=path).decode().rstrip()


def update_helper(west_tmpdir, command):
    # Helper command for causing a change in two remote repositories,
    # then running a project command on the west installation.
    #
    # Adds a commit to both of the kconfiglib and net-tools projects
    # remotes, then run `command`.
    #
    # Captures the 'manifest-rev' and HEAD SHAs in both repositories
    # before and after running the command, returning them in a tuple
    # like this:
    #
    # (net-tools-manifest-rev-before,
    #  net-tools-manifest-rev-after,
    #  net-tools-HEAD-before,
    #  net-tools-HEAD-after,
    #  kconfiglib-manifest-rev-before,
    #  kconfiglib-manifest-rev-after,
    #  kconfiglib-HEAD-before,
    #  kconfiglib-HEAD-after)

    nt_remote = str(west_tmpdir.join('..', 'repos', 'net-tools'))
    nt_local = str(west_tmpdir.join('net-tools'))
    kl_remote = str(west_tmpdir.join('..', 'repos', 'Kconfiglib'))
    kl_local = str(west_tmpdir.join('subdir', 'Kconfiglib'))

    nt_mr_0 = check_output([GIT, 'rev-parse', 'manifest-rev'], cwd=nt_local)
    kl_mr_0 = check_output([GIT, 'rev-parse', 'manifest-rev'], cwd=kl_local)
    nt_head_0 = check_output([GIT, 'rev-parse', 'HEAD'], cwd=nt_local)
    kl_head_0 = check_output([GIT, 'rev-parse', 'HEAD'], cwd=kl_local)

    add_commit(nt_remote, 'another net-tools commit')
    add_commit(kl_remote, 'another kconfiglib commit')

    cmd(command)

    nt_mr_1 = check_output([GIT, 'rev-parse', 'manifest-rev'], cwd=nt_local)
    kl_mr_1 = check_output([GIT, 'rev-parse', 'manifest-rev'], cwd=kl_local)
    nt_head_1 = check_output([GIT, 'rev-parse', 'HEAD'], cwd=nt_local)
    kl_head_1 = check_output([GIT, 'rev-parse', 'HEAD'], cwd=kl_local)

    return (nt_mr_0, nt_mr_1,
            nt_head_0, nt_head_1,
            kl_mr_0, kl_mr_1,
            kl_head_0, kl_head_1)
