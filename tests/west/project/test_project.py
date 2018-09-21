import argparse
import os
import shlex
import shutil
import subprocess

import pytest

import commands.project

# Path to the template manifest used to construct a real one when
# running each test case.
MANIFEST_TEMPLATE_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), 'manifest.yml')
)

# Where the projects are cloned to
NET_TOOLS_PATH = 'net-tools'
KCONFIGLIB_PATH = 'sub/Kconfiglib'

COMMAND_OBJECTS = (
    commands.project.ListProjects(),
    commands.project.Fetch(),
    commands.project.Pull(),
    commands.project.Rebase(),
    commands.project.Branch(),
    commands.project.Checkout(),
    commands.project.Diff(),
    commands.project.Status(),
    commands.project.ForAll(),
)


def cmd(cmd):
    # We assume the manifest is in ../manifest.yml when tests are run.
    manifest_path = os.path.abspath(os.path.join(os.path.dirname(os.getcwd()),
                                                 'manifest.yml'))
    cmd += ' -m ' + manifest_path

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
            command_object.do_run(*parser.parse_known_args(shlex.split(cmd)))
            break
    else:
        assert False, "unknown command " + command_name


@pytest.fixture
def clean_west_topdir(tmpdir):
    # Initialize the repositories used for testing.
    repos = tmpdir.join('repositories')
    repos.mkdir()
    git = shutil.which('git')
    for path in ('net-tools', 'Kconfiglib'):
        fullpath = str(repos.join(path))
        subprocess.check_call([git, 'init', fullpath])
        # The repository gets user name and email set in case there is
        # no global default.
        #
        # The extra '--no-xxx' flags are for convenience when testing
        # on developer workstations, which may have global git
        # configuration to sign commits, etc.
        #
        # We don't want any of that, as it could require user
        # intervention or fail in environments where Git isn't
        # configured.
        subprocess.check_call([git, 'config', 'user.name', 'West Test'],
                              cwd=fullpath)
        subprocess.check_call([git, 'config', 'user.email',
                               'west-test@example.com'],
                              cwd=fullpath)
        subprocess.check_call([git, 'commit',
                               '--allow-empty',
                               '-m', 'empty commit',
                               '--no-verify',
                               '--no-gpg-sign',
                               '--no-post-rewrite'],
                              cwd=fullpath)
        if path == 'Kconfiglib':
            subprocess.check_call([git, 'branch', 'zephyr'], cwd=fullpath)

    # Create the per-tmpdir manifest file.
    with open(MANIFEST_TEMPLATE_PATH, 'r') as src:
        with open(str(tmpdir.join('manifest.yml')), 'w') as dst:
            dst.write(src.read().format(tmpdir=str(repos)))

    # Initialize and change to the installation directory.
    zephyrproject = tmpdir.join('zephyrproject')
    zephyrproject.mkdir()
    zephyrproject.mkdir('west')
    zephyrproject.join('west', '.west_topdir').ensure()
    zephyrproject.chdir()


def test_list_projects(clean_west_topdir):
    # TODO: Check output
    cmd('list-projects')


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
