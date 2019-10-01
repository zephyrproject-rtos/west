import os
import re
import subprocess
import textwrap

import pytest

from west import configuration as config
from conftest import create_repo, add_commit, check_output, cmd, GIT, rev_parse

#
# Test fixtures
#

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
    expected = ['manifest HEAD zephyr cloned None',
                'Kconfiglib zephyr {} cloned None'.format(
                    os.path.join('subdir', 'Kconfiglib')),
                'net-tools master net-tools cloned 1']
    assert actual.splitlines() == expected

    # We should be able to find projects by absolute or relative path
    # when outside any project. Invalid projects should error out.
    klib_rel = os.path.join('subdir', 'Kconfiglib')
    klib_abs = str(west_update_tmpdir.join('subdir', 'Kconfiglib'))

    rel_outside = cmd('list -f "{{name}}" {}'.format(klib_rel)).strip()
    assert rel_outside == 'Kconfiglib'

    abs_outside = cmd('list -f "{{name}}" {}'.format(klib_abs)).strip()
    assert abs_outside == 'Kconfiglib'

    rel_inside = cmd('list -f "{name}" .', cwd=klib_abs).strip()
    assert rel_inside == 'Kconfiglib'

    abs_inside = cmd('list -f "{{name}}" {}'.format(klib_abs),
                     cwd=klib_abs).strip()
    assert abs_inside == 'Kconfiglib'

    with pytest.raises(subprocess.CalledProcessError):
        cmd('list NOT_A_PROJECT', cwd=klib_abs)

    with pytest.raises(subprocess.CalledProcessError):
        cmd('list NOT_A_PROJECT')


def test_manifest_freeze(west_update_tmpdir):
    # We should be able to freeze manifests.
    actual = cmd('manifest --freeze').splitlines()
    # Match the actual output against the expected line by line,
    # so failing lines can be printed in the test output.
    #
    # Since the actual remote URLs and SHAs are not predictable, we
    # don't match those precisely. However, we do expect the output to
    # match project order as specified in our manifest, that all
    # revisions are full 40-character SHAs, and there isn't any random
    # YAML tag crap.
    kconfig_rel = os.path.join('subdir', 'Kconfiglib')
    expected_res = ['^manifest:$',
                    '^  defaults:$',
                    '^    remote: test-local$',
                    '^    revision: master$',
                    '^  remotes:$',
                    '^  - name: test-local$',
                    '^    url-base: .*$',
                    '^  projects:$',
                    '^  - name: Kconfiglib$',
                    '^    remote: test-local$',
                    '^    revision: [a-f0-9]{40}$',
                    '^    path: {}$'.format(re.escape(kconfig_rel)),
                    '^  - name: net-tools$',
                    '^    remote: test-local$',
                    '^    revision: [a-f0-9]{40}$',
                    '^    clone-depth: 1$',
                    '^    west-commands: scripts/west-commands.yml$',
                    '^  self:$',
                    '^    path: zephyr$']

    for eline_re, aline in zip(expected_res, actual):
        assert re.match(eline_re, aline) is not None, (aline, eline_re)


def test_diff(west_init_tmpdir):
    # FIXME: Check output

    # Diff with no projects cloned shouldn't fail

    cmd('diff')

    # Neither should it fail after fetching one or both projects

    cmd('update net-tools')
    cmd('diff')

    cmd('update Kconfiglib')


def test_status(west_init_tmpdir):
    # FIXME: Check output

    # Status with no projects cloned shouldn't fail

    cmd('status')

    # Neither should it fail after fetching one or both projects

    cmd('update net-tools')
    cmd('status')

    cmd('update Kconfiglib')


def test_forall(west_init_tmpdir):
    # FIXME: Check output
    # The 'echo' command is available in both 'shell' and 'batch'

    # 'forall' with no projects cloned shouldn't fail

    cmd('forall -c "echo *"')

    # Neither should it fail after cloning one or both projects

    cmd('update net-tools')
    cmd('forall -c "echo *"')

    cmd('update Kconfiglib')
    cmd('forall -c "echo *"')


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


def test_init_local_manifest_project(repos_tmpdir):
    # Do a local clone of manifest repo
    zephyr_install_dir = repos_tmpdir.join('west_installation', 'zephyr')
    clone(str(repos_tmpdir.join('repos', 'zephyr')),
          str(zephyr_install_dir))

    cmd('init -l "{}"'.format(str(zephyr_install_dir)))

    # Verify Zephyr and .west/west has been installed during init -l
    # but not projects
    zid = repos_tmpdir.join('west_installation')
    assert zid.check(dir=1)
    assert zid.join('subdir', 'Kconfiglib').check(dir=0)
    assert zid.join('net-tools').check(dir=0)
    assert zid.join('zephyr').check(dir=1)
    assert zid.join('zephyr', '.git').check(dir=1)
    assert zid.join('zephyr', 'CODEOWNERS').check(file=1)
    assert zid.join('zephyr', 'include', 'header.h').check(file=1)
    assert zid.join('zephyr', 'subsys', 'bluetooth', 'code.c').check(file=1)

    cmd('update', cwd=str(zid))
    # The projects should be installled now
    assert zid.check(dir=1)
    assert zid.join('subdir', 'Kconfiglib').check(dir=1)
    assert zid.join('net-tools').check(dir=1)
    assert zid.join('subdir', 'Kconfiglib').check(dir=1)
    assert zid.join('subdir', 'Kconfiglib', '.git').check(dir=1)
    assert zid.join('subdir', 'Kconfiglib', 'kconfiglib.py').check(file=1)
    assert zid.join('net-tools').check(dir=1)
    assert zid.join('net-tools', '.git').check(dir=1)
    assert zid.join('net-tools', 'qemu-script.sh').check(file=1)


def test_init_local_already_initialized_failure(west_init_tmpdir):
    # Test that 'west init -l' on an initialized tmpdir errors out
    with pytest.raises(subprocess.CalledProcessError):
        cmd('init -l "{}"'.format(str(west_init_tmpdir)))


def test_init_local_missing_west_yml_failure(repos_tmpdir):
    # Test that 'west init -l' on repo without a 'west.yml' fails

    # Do a local clone of manifest repo
    zephyr_install_dir = repos_tmpdir.join('west_installation', 'zephyr')
    clone(str(repos_tmpdir.join('repos', 'zephyr')),
          str(zephyr_install_dir))
    os.remove(str(zephyr_install_dir.join('west.yml')))

    with pytest.raises(subprocess.CalledProcessError):
        cmd('init -l "{}"'.format(str(zephyr_install_dir)))


def test_extension_command_execution(west_init_tmpdir):
    with pytest.raises(subprocess.CalledProcessError):
        cmd('test-extension')

    cmd('update')

    actual = cmd('test-extension')
    assert actual.rstrip() == 'Testing test command 1'


def test_extension_command_multiproject(repos_tmpdir):
    # Test to ensure that multiple projects can define extension commands and
    # that those are correctly presented and executed.
    rr = repos_tmpdir.join('repos')
    remote_kconfiglib = str(rr.join('Kconfiglib'))
    remote_zephyr = str(rr.join('zephyr'))
    remote_west = str(rr.join('west'))

    # Update the manifest to specify extension commands in Kconfiglib.
    add_commit(remote_zephyr, 'test added extension command',
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
                            west-commands: scripts/west-commands.yml
                          - name: net-tools
                            west-commands: scripts/west-commands.yml
                        self:
                          path: zephyr
                      '''.format(west=remote_west, rr=str(rr)))})

    # Add an extension command to the Kconfiglib remote.
    add_commit(remote_kconfiglib, 'add west commands',
               files={'scripts/west-commands.yml': textwrap.dedent('''\
                      west-commands:
                        - file: scripts/test.py
                          commands:
                            - name: kconfigtest
                              class: Test
                      '''),
                      'scripts/test.py': textwrap.dedent('''\
                      from west.commands import WestCommand
                      class Test(WestCommand):
                          def __init__(self):
                              super(Test, self).__init__(
                                  'kconfigtest',
                                  'Kconfig test application',
                                  '')
                          def do_add_parser(self, parser_adder):
                              parser = parser_adder.add_parser(self.name)
                              return parser
                          def do_run(self, args, ignored):
                              print('Testing kconfig test')
                      '''),
                      })
    west_tmpdir = repos_tmpdir.join('west_installation')
    cmd('init -m "{}" "{}"'.format(str(repos_tmpdir.join('repos', 'zephyr')),
                                   str(west_tmpdir)))
    west_tmpdir.chdir()
    config.read_config()
    cmd('update')

    # The newline shenanigans are for Windows.
    help_text = '\n'.join(cmd('-h').splitlines())
    expected = '\n'.join([
        'extension commands from project Kconfiglib (path: {}):'.
        format(os.path.join('subdir', 'Kconfiglib')),
        '  kconfigtest:          (no help provided; try "west kconfigtest -h")',  # noqa: E501
        '',
        'extension commands from project net-tools (path: net-tools):',
        '  test-extension:       test-extension-help'])
    assert expected in help_text, help_text

    actual = cmd('test-extension')
    assert actual.rstrip() == 'Testing test command 1'

    actual = cmd('kconfigtest')
    assert actual.rstrip() == 'Testing kconfig test'


def test_extension_command_duplicate(repos_tmpdir):
    # Test to ensure that in case to subprojects introduces same command, it
    # will print a warning.
    rr = repos_tmpdir.join('repos')
    remote_kconfiglib = str(rr.join('Kconfiglib'))
    remote_zephyr = str(rr.join('zephyr'))
    remote_west = str(rr.join('west'))

    add_commit(remote_zephyr, 'test added extension command',
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
                            west-commands: scripts/west-commands.yml
                          - name: net-tools
                            west-commands: scripts/west-commands.yml
                        self:
                          path: zephyr
                      '''.format(west=remote_west, rr=str(rr)))})

    # Initialize the net-tools repository.
    add_commit(remote_kconfiglib, 'add west commands',
               files={'scripts/west-commands.yml': textwrap.dedent('''\
                      west-commands:
                        - file: scripts/test.py
                          commands:
                            - name: test-extension
                              class: Test
                      '''),
                      'scripts/test.py': textwrap.dedent('''\
                      from west.commands import WestCommand
                      class Test(WestCommand):
                          def __init__(self):
                              super(Test, self).__init__(
                                  'test-extension',
                                  'test application',
                                  '')
                          def do_add_parser(self, parser_adder):
                              parser = parser_adder.add_parser(self.name)
                              return parser
                          def do_run(self, args, ignored):
                              print('Testing kconfig test command')
                      '''),
                      })
    west_tmpdir = repos_tmpdir.join('west_installation')
    cmd('init -m "{}" "{}"'.format(str(repos_tmpdir.join('repos', 'zephyr')),
                                   str(west_tmpdir)))
    west_tmpdir.chdir()
    config.read_config()
    cmd('update')

    actual = cmd('test-extension', stderr=subprocess.STDOUT).splitlines()
    expected = [
        'WARNING: ignoring project net-tools extension command "test-extension"; command "test-extension" already defined as extension command',  # noqa: E501
        'Testing kconfig test command',
    ]

    assert actual == expected

def test_topdir_none(tmpdir):
    # Running west topdir outside of any installation ought to fail.

    tmpdir.chdir()
    with pytest.raises(subprocess.CalledProcessError):
        cmd('topdir')

def test_topdir_in_installation(west_init_tmpdir):
    # Running west topdir anywhere inside of an installation ought to
    # work, and return the same thing.

    expected = str(west_init_tmpdir)

    # This should be available immediately after west init.
    assert cmd('topdir').strip() == expected

    # After west update, it should continue to work, and return the
    # same thing (not getting confused when called from within a
    # project directory or a random user-created subdirectory, e.g.)
    cmd('update')
    assert cmd('topdir', cwd=str(west_init_tmpdir / 'subdir' /
                                 'Kconfiglib')).strip() == expected
    west_init_tmpdir.mkdir('pytest-foo')
    assert cmd('topdir', cwd=str(west_init_tmpdir /
                                 'pytest-foo')).strip() == expected

#
# Helper functions used by the test cases and fixtures.
#


def clone(repo, dst):
    # Creates a new branch.
    repo = str(repo)

    subprocess.check_call([GIT, 'clone', repo, dst])


def checkout_branch(repo, branch, create=False):
    # Creates a new branch.
    repo = str(repo)

    # Edit any files as specified by the user and add them to the index.
    if create:
        subprocess.check_call([GIT, 'checkout', '-b', branch], cwd=repo)
    else:
        subprocess.check_call([GIT, 'checkout', branch], cwd=repo)


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


def test_change_remote_conflict(west_update_tmpdir):
    # Test that `west update` will force fetch into local refs space when
    # remote has changed and cannot be fast forwarded.
    wct = west_update_tmpdir
    tmpdir = wct.join('..')

    rrepo = str(tmpdir.join('repos'))
    net_tools = str(tmpdir.join('repos', 'net-tools'))
    rwest = str(tmpdir.join('repos', 'west'))
    alt_repo = str(tmpdir.join('alt_repo'))
    alt_net_tools = str(tmpdir.join('alt_repo', 'net-tools'))
    create_repo(alt_net_tools)
    add_commit(alt_net_tools, 'test conflicting commit',
               files={'qemu-script.sh': 'echo alternate world net-tools\n'})

    revision = rev_parse(net_tools, 'HEAD')

    west_yml_content = textwrap.dedent('''\
                      west:
                        url: file://{west}
                      manifest:
                        defaults:
                          remote: test-local

                        remotes:
                          - name: test-local
                            url-base: file://{rr}

                        projects:
                          - name: net-tools
                            revision: {rev}
                        self:
                          path: zephyr
                      '''.format(west=rwest, rr=rrepo, rev=revision))
    add_commit(str(wct.join('zephyr')), 'test update manifest',
               files={'west.yml': west_yml_content})

    cmd('update')

    revision = rev_parse(alt_net_tools, 'HEAD')

    west_yml_content = textwrap.dedent('''\
                      west:
                        url: file://{west}
                      manifest:
                        defaults:
                          remote: test-local

                        remotes:
                          - name: test-local
                            url-base: file://{rr}
                          - name: test-alternate
                            url-base: file://{ar}

                        projects:
                          - name: net-tools
                            remote: test-alternate
                            revision: {rev}
                        self:
                          path: zephyr
                      '''.format(west=rwest, ar=alt_repo, rr=rrepo,
                                 rev=revision))

    add_commit(str(wct.join('zephyr')), 'test update manifest conflict',
               files={'west.yml': west_yml_content})

    cmd('update')
