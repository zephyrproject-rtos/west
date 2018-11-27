import argparse
import os
import shlex
import shutil
import subprocess
import sys

import pytest

from west import config
from west.commands import project
import west._bootstrap.main as bootstrap

# Where the projects are cloned to
NET_TOOLS_PATH = 'net-tools'
KCONFIGLIB_PATH = 'sub/Kconfiglib'

GIT = shutil.which('git')

COMMAND_OBJECTS = (
    project.List(),
    project.Fetch(),
    project.Pull(),
    project.Rebase(),
    project.Branch(),
    project.Checkout(),
    project.Diff(),
    project.Status(),
    project.Update(),
    project.ForAll(),
)


def cmd(cmd):
    # We assume the manifest is in manifest.yml when tests are run.
    cmd += ' -m manifest.yml'

    # cmd() takes the command as a string, which is less clunky to work with.
    # Split it according to shell rules.
    split_cmd = shlex.split(cmd)
    command_name = split_cmd[0]

    for command_object in COMMAND_OBJECTS:
        # Find the WestCommand object that implements the command
        if command_object.name == command_name:
            # Use it to parse the arguments
            parser = argparse.ArgumentParser()
            command_object.do_add_parser(parser.add_subparsers())

            # Pass the parsed arguments and unknown arguments to run it
            command_object.do_run(*parser.parse_known_args(split_cmd))
            break
    else:
        assert False, "unknown command " + command_name


@pytest.fixture
def clean_west_topdir(tmpdir):
    # Initialize some placeholder upstream repositories, in remote-repos/
    remote_repos_dir = tmpdir.mkdir('remote-repos')
    for project in 'net-tools', 'Kconfiglib', 'manifest', 'west':
        path = str(remote_repos_dir.join(project))
        create_repo(path)
        add_commit(path, 'initial')
        if project == 'Kconfiglib':
            subprocess.check_call([GIT, 'branch', 'zephyr'], cwd=path)

    # Create west/.west_topdir, to mark this directory as a West installation,
    # and a manifest.yml pointing to the repositories we created above
    tmpdir.join('west', '.west_topdir').ensure()
    tmpdir.join('manifest.yml').write('''
manifest:
  defaults:
    remote: repos
    revision: master

  remotes:
    - name: repos
      url-base: file://{}

  projects:
    - name: net-tools
    - name: Kconfiglib
      revision: zephyr
      path: sub/Kconfiglib
'''.format(remote_repos_dir))

    # Switch to the top-level West installation directory
    tmpdir.chdir()

    return tmpdir


def create_repo(path):
    # Initializes a Git repository in 'path', and adds an initial commit to it

    subprocess.check_call([GIT, 'init', path])
    add_commit(path, 'initial')


def add_commit(path, msg):
    # Adds an empty commit with message 'msg' to the repo in 'path'

    # Set name and email. This avoids a "Please tell me who you are" error when
    # there's no global default.
    subprocess.check_call([GIT, 'config', 'user.name', 'West Test'], cwd=path)
    subprocess.check_call([GIT, 'config', 'user.email',
                           'west-test@example.com'],
                          cwd=path)

    # The extra '--no-xxx' flags are for convenience when testing
    # on developer workstations, which may have global git
    # configuration to sign commits, etc.
    #
    # We don't want any of that, as it could require user
    # intervention or fail in environments where Git isn't
    # configured.
    subprocess.check_call(
        [GIT, 'commit', '--allow-empty', '-m', msg, '--no-verify',
         '--no-gpg-sign', '--no-post-rewrite'], cwd=path)


def test_list(clean_west_topdir):
    # TODO: Check output
    cmd('list')


def test_fetch(clean_west_topdir):
    # Clone all projects
    cmd('fetch --no-update')

    # Check that they got cloned
    assert os.path.isdir(NET_TOOLS_PATH)
    assert os.path.isdir(KCONFIGLIB_PATH)

    # Non-existent project
    with pytest.raises(SystemExit):
        cmd('fetch --no-update non-existent')

    # Update a specific project
    cmd('fetch --no-update net-tools')


def test_pull(clean_west_topdir):
    # Clone all projects
    cmd('pull --no-update')

    # Check that they got cloned
    assert os.path.isdir(NET_TOOLS_PATH)
    assert os.path.isdir(KCONFIGLIB_PATH)

    # Non-existent project
    with pytest.raises(SystemExit):
        cmd('pull --no-update non-existent')

    # Update a specific project
    cmd('pull --no-update net-tools')


def test_rebase(clean_west_topdir):
    # Clone just one project
    cmd('fetch --no-update net-tools')

    # Piggyback a check that just that project got cloned
    assert not os.path.exists(KCONFIGLIB_PATH)

    # Rebase the project (non-cloned project should be silently skipped)
    cmd('rebase')

    # Rebase the project again, naming it explicitly
    cmd('rebase net-tools')

    # Try rebasing a project that hasn't been cloned
    with pytest.raises(SystemExit):
        cmd('pull --no-update rebase Kconfiglib')

    # Clone the other project
    cmd('pull --no-update Kconfiglib')

    # Will rebase both projects now
    cmd('rebase')


def test_branches(clean_west_topdir):
    # Missing branch name
    with pytest.raises(SystemExit):
        cmd('checkout')


    # Clone just one project
    cmd('fetch --no-update net-tools')

    # Create a branch in the cloned project
    cmd('branch foo')

    # Check out the branch
    cmd('checkout foo')

    # Check out the branch again, naming the project explicitly
    cmd('checkout foo net-tools')

    # Try checking out a branch that doesn't exist in any project
    with pytest.raises(SystemExit):
        cmd('checkout nonexistent')

    # Try checking out a branch in a non-cloned project
    with pytest.raises(SystemExit):
        cmd('checkout foo Kconfiglib')

    # Clone the other project
    cmd('fetch --no-update Kconfiglib')

    # It still doesn't have the branch
    with pytest.raises(SystemExit):
        cmd('checkout foo Kconfiglib')

    # Create a differently-named branch it
    cmd('branch bar Kconfiglib')

    # That branch shouldn't exist in the other project
    with pytest.raises(SystemExit):
        cmd('checkout bar net-tools')

    # It should be possible to check out each branch even though they only
    # exists in one project
    cmd('checkout foo')
    cmd('checkout bar')

    # List all branches and the projects they appear in (TODO: Check output)
    cmd('branch')


def test_diff(clean_west_topdir):
    # TODO: Check output

    # Diff with no projects cloned shouldn't fail

    cmd('diff')

    # Neither should it fail after fetching one or both projects

    cmd('fetch --no-update net-tools')
    cmd('diff')

    cmd('fetch --no-update Kconfiglib')
    cmd('diff --cached')  # Pass a custom flag too


def test_status(clean_west_topdir):
    # TODO: Check output

    # Status with no projects cloned shouldn't fail

    cmd('status')

    # Neither should it fail after fetching one or both projects

    cmd('fetch --no-update net-tools')
    cmd('status')

    cmd('fetch --no-update Kconfiglib')
    cmd('status --long')  # Pass a custom flag too


def test_forall(clean_west_topdir):
    # TODO: Check output
    # The 'echo' command is available in both 'shell' and 'batch'

    # 'forall' with no projects cloned shouldn't fail

    cmd("forall -c 'echo *'")

    # Neither should it fail after fetching one or both projects

    cmd('fetch --no-update net-tools')
    cmd("forall -c 'echo *'")

    cmd('fetch --no-update Kconfiglib')
    cmd("forall -c 'echo *'")


def test_update(clean_west_topdir):
    # Test the 'west update' command. It calls through to the same backend
    # functions that are used for automatic updates and 'west init'
    # reinitialization.

    # Create placeholder local repos
    create_repo('west/manifest')
    create_repo('west/west')

    # Create a simple configuration file. Git requires absolute paths for local
    # repositories.
    clean_west_topdir.join('west/config').write('''
[manifest]
remote = {0}/remote-repos/manifest
revision = master
'''.format(clean_west_topdir))

    config.read_config()

    # modify the manifest to point to another west
    clean_west_topdir.join('manifest.yml').write('''
west:
  url: file://{}/remote-repos/west
  revision: master
'''.format(clean_west_topdir), 'a')

    # Fetch the net-tools repository
    cmd('fetch --no-update net-tools')

    # Add commits to the local repos
    for path in 'west/manifest', 'west/west', NET_TOOLS_PATH:
        add_commit(path, 'local')

    # Check that resetting the manifest repository removes the local commit
    cmd('update --reset-manifest')
    assert head_subject('west/manifest') == 'initial'
    assert head_subject('west/west') == 'local'     # Unaffected
    assert head_subject(NET_TOOLS_PATH) == 'local'  # Unaffected

    # Check that resetting the west repository removes the local commit
    cmd('update --reset-west')
    assert head_subject('west/west') == 'initial'
    assert head_subject(NET_TOOLS_PATH) == 'local'  # Unaffected

    # Check that resetting projects removes the local commit
    cmd('update --reset-projects')
    assert head_subject(NET_TOOLS_PATH) == 'initial'

    # Add commits to the upstream special repos
    for path in 'remote-repos/manifest', 'remote-repos/west':
        add_commit(path, 'upstream')

    # Check that updating the manifest repository gets the upstream commit
    cmd('update --update-manifest')
    assert head_subject('west/manifest') == 'upstream'
    assert head_subject('west/west') == 'initial'  # Unaffected

    # Check that updating the West repository triggers a restart
    with pytest.raises(project.WestUpdated):
        cmd('update --update-west')


def head_subject(path):
    # Returns the subject of the HEAD commit in the repository at 'path'

    return subprocess.check_output([GIT, 'log', '-n1', '--format=%s'],
                                   cwd=path).decode().rstrip()


def test_bootstrap_reinit(clean_west_topdir, monkeypatch):
    # Test that the bootstrap script calls 'west' with the expected --reset-*
    # flags flags when reinitializing

    def save_wrap_args(args):
        # Saves bootstrap.wrap() arguments into wrap_args
        nonlocal wrap_args
        wrap_args = args

    monkeypatch.setattr(bootstrap, 'wrap', save_wrap_args)

    with pytest.raises(SystemExit):
        bootstrap.init([])  # West already initialized

    for init_args, west_args in (
        (['-m',   'foo'], ['update', '--reset-manifest', '--reset-projects',
                           '--reset-west']),
        (['--mr', 'foo'], ['update', '--reset-manifest', '--reset-projects',
                           '--reset-west'])):

        # Reset wrap_args before each test so that it ends up as [] if wrap()
        # isn't called (for the --no-reset case)
        wrap_args = []
        bootstrap.init(init_args)
        assert wrap_args == west_args
