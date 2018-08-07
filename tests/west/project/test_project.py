import argparse
import os
import shlex
import tempfile

import pytest

import west.cmd.project


MANIFEST_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), 'manifest.yml')
)

# Where the projects are cloned to
NET_TOOLS_PATH = 'net-tools'
KCONFIGLIB_PATH = 'sub/kconfiglib'

COMMAND_OBJECTS = (
    west.cmd.project.ListProjects(),
    west.cmd.project.Fetch(),
    west.cmd.project.Pull(),
    west.cmd.project.Rebase(),
    west.cmd.project.Branch(),
    west.cmd.project.Checkout(),
    west.cmd.project.Diff(),
    west.cmd.project.Status(),
)


def cmd(cmd):
    cmd += ' -m ' + MANIFEST_PATH

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
    tmpdir.mkdir('.west')
    tmpdir.chdir()


def test_list_projects(clean_west_topdir):
    # TODO: Check output
    cmd('list-projects')


def test_fetch(clean_west_topdir):
    # Clone all projects
    cmd('fetch')

    # Check that they got cloned
    assert os.path.isdir(NET_TOOLS_PATH)
    assert os.path.isdir(KCONFIGLIB_PATH)

    # Non-existent project
    with pytest.raises(SystemExit):
        cmd('fetch non-existent')

    # Update a specific project
    cmd('fetch net-tools')


def test_pull(clean_west_topdir):
    # Clone all projects
    cmd('pull')

    # Check that they got cloned
    assert os.path.isdir(NET_TOOLS_PATH)
    assert os.path.isdir(KCONFIGLIB_PATH)

    # Non-existent project
    with pytest.raises(SystemExit):
        cmd('pull non-existent')

    # Update a specific project
    cmd('pull net-tools')


def test_rebase(clean_west_topdir):
    # Clone just one project
    cmd('fetch net-tools')

    # Piggyback a check that just that project got cloned
    assert not os.path.exists(KCONFIGLIB_PATH)

    # Rebase the project (non-cloned project should be silently skipped)
    cmd('rebase')

    # Rebase the project again, naming it explicitly
    cmd('rebase net-tools')

    # Try rebasing a project that hasn't been cloned
    with pytest.raises(SystemExit):
        cmd('pull rebase Kconfiglib')

    # Clone the other project
    cmd('pull Kconfiglib')

    # Will rebase both projects now
    cmd('rebase')


def test_branches(clean_west_topdir):
    # Missing branch name
    with pytest.raises(SystemExit):
        cmd('branch')

    # Missing branch name
    with pytest.raises(SystemExit):
        cmd('checkout')


    # Clone just one project
    cmd('fetch net-tools')

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
    cmd('fetch Kconfiglib')

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


def test_diff(clean_west_topdir):
    # TODO: Check output

    # Diff with no projects cloned shouldn't fail

    cmd('diff')

    # Neither should it fail after fetching one or both projects

    cmd('fetch net-tools')
    cmd('diff')

    cmd('fetch Kconfiglib')
    cmd('diff --cached')  # Pass a custom flag too


def test_status(clean_west_topdir):
    # TODO: Check output

    # Status with no projects cloned shouldn't fail
    cmd('status')

    # Neither should it fail after fetching one or both projects
    cmd('fetch net-tools')
    cmd('status')

    # Neither should it fail after fetching one or both projects
    cmd('fetch Kconfiglib')
    # Pass a custom flag too
    cmd('status --long')  # Pass a custom flag too
