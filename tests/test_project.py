# Copyright (c) 2020, Nordic Semiconductor ASA

import collections
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path, PurePath

import pytest

from west import configuration as config
from west.manifest import Manifest, ManifestProject, Project, \
    ManifestImportFailed
from west.manifest import ImportFlag as MIF
from conftest import create_branch, create_workspace, create_repo, \
    add_commit, add_tag, check_output, cmd, GIT, rev_parse, \
    check_proj_consistency, WINDOWS

assert 'TOXTEMPDIR' in os.environ, "you must run these tests using tox"

#
# Helpers
#

# A container for the remote locations of the repositories involved in
# a west update. These attributes may be None.
UpdateRemotes = collections.namedtuple('UpdateRemotes',
                                       'net_tools kconfiglib tagged_repo')

# A container type which holds the remote and local repository paths
# used in tests of 'west update'. This also contains manifest-rev
# branches and HEAD commits before (attributes ending in _0) and after
# (ending in _1) the 'west update' is done by update_helper().
#
# These may be None.
#
# See conftest.py for details on the manifest used in west update
# testing, and update_helper() below as well.
UpdateResults = collections.namedtuple('UpdateResults',
                                       'nt_remote nt_local '
                                       'kl_remote kl_local '
                                       'tr_remote tr_local '
                                       'nt_mr_0 nt_mr_1 '
                                       'kl_mr_0 kl_mr_1 '
                                       'tr_mr_0 tr_mr_1 '
                                       'nt_head_0 nt_head_1 '
                                       'kl_head_0 kl_head_1 '
                                       'tr_head_0 tr_head_1')

# Helper list for forming commands that add submodules from
# remotes which may be on the local file system. Such cases
# must be explicitly authorized since git 2.28.1. For details,
# see:
#
# https://github.blog/2022-10-18-git-security-vulnerabilities-announced/#cve-2022-39253
SUBMODULE_ADD = [GIT,
                 '-c', 'protocol.file.allow=always',
                 'submodule',
                 'add']

# Helper string for the same purpose when running west update.
PROTOCOL_FILE_ALLOW = '--submodule-init-config protocol.file.allow=always'

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

def _list_f(format):
    return ['list', '-f', format]


def _match_multiline_regex(expected, actual):
    for eline_re, aline in zip(expected, actual):
        assert re.match(eline_re, aline) is not None, (aline, eline_re)


def test_workspace(west_update_tmpdir):
    # Basic test that west_update_tmpdir bootstrapped correctly. This
    # is a basic test of west init and west update.

    # Make sure the expected files and directories exist in the right
    # places.
    wct = west_update_tmpdir
    assert wct.check(dir=1)
    assert wct.join('subdir', 'Kconfiglib').check(dir=1)
    assert wct.join('subdir', 'Kconfiglib', '.git').check(dir=1)
    assert wct.join('subdir', 'Kconfiglib', 'kconfiglib.py').check(file=1)
    assert wct.join('tagged_repo').check(dir=1)
    assert wct.join('tagged_repo', '.git').check(dir=1)
    assert wct.join('tagged_repo', 'test.txt').check()
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
    actual = cmd(_list_f('{name} {revision} {path} {cloned} {clone_depth}'))
    expected = ['manifest HEAD zephyr cloned None',
                'Kconfiglib zephyr subdir/Kconfiglib cloned None',
                'tagged_repo v1.0 tagged_repo cloned None',
                'net-tools master net-tools cloned 1']
    assert actual.splitlines() == expected

    # We should be able to find projects by absolute or relative path
    # when outside any project. Invalid projects should error out.
    klib_rel = os.path.join('subdir', 'Kconfiglib')
    klib_abs = str(west_update_tmpdir.join('subdir', 'Kconfiglib'))

    rel_outside = cmd(_list_f('{name}') + [klib_rel]).strip()
    assert rel_outside == 'Kconfiglib'

    abs_outside = cmd(_list_f('{name}') + [klib_abs]).strip()
    assert abs_outside == 'Kconfiglib'

    rel_inside = cmd('list -f {name} .', cwd=klib_abs).strip()
    assert rel_inside == 'Kconfiglib'

    abs_inside = cmd(_list_f('{name}') + [klib_abs], cwd=klib_abs).strip()
    assert abs_inside == 'Kconfiglib'

    with pytest.raises(subprocess.CalledProcessError):
        cmd('list NOT_A_PROJECT', cwd=klib_abs)

    with pytest.raises(subprocess.CalledProcessError):
        cmd('list NOT_A_PROJECT')


def test_list_manifest(west_update_tmpdir):
    # The manifest's "self: path:" should only be used to print
    # path-type format strings with --manifest-path-from-yaml.

    os.mkdir('manifest_moved')
    shutil.copy('zephyr/west.yml', 'manifest_moved/west.yml')
    cmd('config manifest.path manifest_moved')

    path = cmd('list -f {path} manifest').strip()
    abspath = cmd('list -f {abspath} manifest').strip()
    posixpath = cmd('list -f {posixpath} manifest').strip()
    assert path == 'manifest_moved'
    assert Path(abspath) == west_update_tmpdir / 'manifest_moved'
    assert posixpath == Path(west_update_tmpdir).as_posix() + '/manifest_moved'

    path = cmd('list --manifest-path-from-yaml '
               '-f {path} manifest').strip()
    abspath = cmd('list --manifest-path-from-yaml '
                  '-f {abspath} manifest').strip()
    posixpath = cmd('list --manifest-path-from-yaml '
                    '-f {posixpath} manifest').strip()
    assert path == 'zephyr'
    assert Path(abspath) == Path(str(west_update_tmpdir / 'zephyr'))
    assert posixpath == Path(west_update_tmpdir).as_posix() + '/zephyr'


def test_list_groups(west_init_tmpdir):
    with open('zephyr/west.yml', 'w') as f:
        f.write("""
        manifest:
          defaults:
            remote: r
          remotes:
            - name: r
              url-base: https://example.com
          projects:
          - name: foo
            groups:
            - foo-group-1
            - foo-group-2
          - name: bar
            path: path-for-bar
          - name: baz
            groups:
            - baz-group
          group-filter: [-foo-group-1,-foo-group-2,-baz-group]
        """)

    def check(command_string, expected):
        out_lines = cmd(command_string).splitlines()
        assert out_lines == expected

    check(_list_f('{name} .{groups}. {path}'),
          ['manifest .. zephyr',
           'bar .. path-for-bar'])

    check(_list_f('{name} .{groups}. {path}') + ['foo'],
          ['foo .foo-group-1,foo-group-2. foo'])

    check(_list_f('{name} .{groups}. {path}') + ['baz'],
          ['baz .baz-group. baz'])

    check(_list_f("{name} .{groups}. {path}") + 'foo bar baz'.split(),
          ['foo .foo-group-1,foo-group-2. foo',
           'bar .. path-for-bar',
           'baz .baz-group. baz'])

    check(_list_f('{name} .{groups}. {path}') + ['--all'],
          ['manifest .. zephyr',
           'foo .foo-group-1,foo-group-2. foo',
           'bar .. path-for-bar',
           'baz .baz-group. baz'])

    cmd('config manifest.group-filter +foo-group-1')
    check(_list_f('{name} .{groups}. {path}'),
          ['manifest .. zephyr',
           'foo .foo-group-1,foo-group-2. foo',
           'bar .. path-for-bar'])


def test_list_sha(west_update_tmpdir):
    # Regression test for listing with {sha}. This should print N/A
    # for the first project, which is the ManifestProject.

    assert cmd('list -f {sha}').startswith("N/A")


def test_manifest_freeze(west_update_tmpdir):
    # We should be able to freeze manifests.
    actual = cmd('manifest --freeze').splitlines()
    # Match the actual output against the expected line by line,
    # so failing lines can be printed in the test output.
    #
    # Since the actual remote URLs and SHAs are not predictable, we
    # don't match those precisely. However, we do expect:
    #
    # - the output to match project order as specified in our
    #   manifest
    # - attributes are listed in NURPCW order (name, url, ...)
    # - all revisions are full 40-character SHAs
    # - there isn't any random YAML tag crap
    expected_res = ['^manifest:$',
                    '^  projects:$',
                    '^  - name: Kconfiglib$',
                    '^    description: |',
                    '^      Kconfiglib is an implementation of$',
                    '^      the Kconfig language written in Python.$',
                    '^    url: .*$',
                    '^    revision: [a-f0-9]{40}$',
                    '^    path: subdir/Kconfiglib$',
                    '^    groups:$',
                    '^    - Kconfiglib-group$',
                    '^    submodules: true$',
                    '^  - name: tagged_repo$',
                    '^    url: .*$',
                    '^    revision: [a-f0-9]{40}$',
                    '^  - name: net-tools$',
                    '^    description: Networking tools.$',
                    '^    url: .*$',
                    '^    revision: [a-f0-9]{40}$',
                    '^    clone-depth: 1$',
                    '^    west-commands: scripts/west-commands.yml$',
                    '^  self:$',
                    '^    path: zephyr$']
    _match_multiline_regex(expected_res, actual)

def test_compare(config_tmpdir, west_init_tmpdir):
    # 'west compare' with no projects cloned should still work,
    # and not print anything.
    assert cmd('compare') == ''

    # Create an empty file and make sure the manifest repository
    # is included in 'west compare' output, and that we get some
    # information about the dirty tree from git.
    foo = west_init_tmpdir / 'zephyr' / 'foo'
    with open(foo, 'w'):
        pass
    actual = cmd('compare')
    assert actual.startswith('=== manifest')
    assert 'foo' in actual

    # --exit-code should work for the manifest repository too.
    with pytest.raises(subprocess.CalledProcessError):
        cmd('compare --exit-code')

    # Remove the file and verify compare output is empty again.
    os.unlink(foo)
    assert cmd('compare') == ''

    # Make sure project related output seems reasonable.
    cmd('update')
    kconfiglib = west_init_tmpdir / 'subdir' / 'Kconfiglib'
    bar = kconfiglib / 'bar'
    with open(bar, 'w'):
        pass
    actual = cmd('compare')
    assert actual.startswith('=== Kconfiglib (subdir/Kconfiglib):')
    assert 'bar' in actual

    # We shouldn't get any output for inactive projects by default, so
    # temporarily deactivate the Kconfiglib project and make sure that
    # works.
    cmd('config manifest.group-filter -- -Kconfiglib-group')
    assert cmd('compare') == ''
    # unless we ask for it with --all, or the project by name
    assert cmd('compare Kconfiglib').startswith(
        '=== Kconfiglib (subdir/Kconfiglib)')
    assert cmd('compare --all').startswith(
        '=== Kconfiglib (subdir/Kconfiglib)')
    # Activate the project again.
    cmd('config -d manifest.group-filter')

    # Verify --exit-code works as advertised, and clean up again.
    with pytest.raises(subprocess.CalledProcessError):
        cmd('compare --exit-code')
    os.unlink(bar)
    assert cmd('compare --exit-code') == ''

    # By default, a checked-out branch should print output, even if
    # the tree is otherwise clean...
    check_output(['git', 'checkout', '-b', 'mybranch'], cwd=kconfiglib)
    actual = cmd('compare')
    assert actual.startswith('=== Kconfiglib (subdir/Kconfiglib):')
    assert 'mybranch' in actual
    # unless we disable that explicitly...
    assert cmd('compare --ignore-branches') == ''
    # or the compare.ignore-branches configuration option is true...
    cmd('config compare.ignore-branches true')
    assert cmd('compare') == ''
    # unless we override that option on the command line.
    assert 'mybranch' in cmd('compare --no-ignore-branches')

def test_diff(west_init_tmpdir):
    # FIXME: Check output

    # Diff with no projects cloned shouldn't fail

    cmd('diff')
    cmd('diff --manifest')
    cmd('diff --stat')

    # Neither should it fail after fetching one or both projects

    cmd('update net-tools')
    cmd('diff')
    cmd('diff --stat')

    cmd('update Kconfiglib')


def test_status(west_init_tmpdir):
    # FIXME: Check output

    # Status with no projects cloned shouldn't fail

    cmd('status')
    cmd('status --short --branch')

    # Neither should it fail after fetching one or both projects

    cmd('update net-tools')
    cmd('status')
    cmd('status --short --branch')

    cmd('update Kconfiglib')


def test_forall(west_init_tmpdir):
    # Note that the 'echo' command is available in both Unix shells
    # and Windows .bat files.

    # 'forall' with no projects cloned shouldn't fail

    assert cmd(['forall', '-c', 'echo foo']).splitlines() == [
        '=== running "echo foo" in manifest (zephyr):',
        'foo']

    # Neither should it fail after cloning one or both projects

    cmd('update net-tools')
    assert cmd(['forall', '-c', 'echo foo']).splitlines() == [
        '=== running "echo foo" in manifest (zephyr):',
        'foo',
        '=== running "echo foo" in net-tools (net-tools):',
        'foo']

    # Use environment variables

    env_var = "%WEST_PROJECT_NAME%" if WINDOWS else "$WEST_PROJECT_NAME"

    assert cmd(['forall', '-c', f'echo {env_var}']).splitlines() == [
        f'=== running "echo {env_var}" in manifest (zephyr):',
        'manifest',
        f'=== running "echo {env_var}" in net-tools (net-tools):',
        'net-tools']

    cmd('update Kconfiglib')
    assert cmd(['forall', '-c', 'echo foo']).splitlines() == [
        '=== running "echo foo" in manifest (zephyr):',
        'foo',
        '=== running "echo foo" in Kconfiglib (subdir/Kconfiglib):',
        'foo',
        '=== running "echo foo" in net-tools (net-tools):',
        'foo']

    assert cmd('forall --group Kconfiglib-group -c'.split() + ['echo foo']
               ).splitlines() == [
                   '=== running "echo foo" in Kconfiglib (subdir/Kconfiglib):',
                   'foo',
               ]


def test_grep(west_init_tmpdir):
    # Make sure we don't find things we don't expect, and do find
    # things we do.

    actual_before_update = cmd('grep net-').strip()
    actual_before_update_lines = actual_before_update.splitlines()
    assert len(actual_before_update_lines) == 2
    assert re.fullmatch(r'=== manifest \(zephyr\):',
                        actual_before_update_lines[0])
    assert re.search('net-tools',
                     actual_before_update_lines[1])

    assert not re.search('hello', cmd('grep hello'))

    cmd('update')
    assert re.search('hello', cmd('grep hello'))

    # Make sure '--' is handled properly: the first one is for
    # west, and the second one is for the tool

    assert re.search('west-commands', cmd('grep -- -- -commands'))


def test_update_projects(west_init_tmpdir):
    # Test the 'west update' command. It calls through to the same backend
    # functions that are used for automatic updates and 'west init'
    # reinitialization.

    # create local repositories
    cmd('update')

    # Add commits to the local repos.
    ur = update_helper(west_init_tmpdir)

    # We updated all the repositories, so all paths and commits should
    # be valid refs (i.e. there shouldn't be a None or empty string
    # value in a ur attribute).
    assert all(ur)

    # Make sure we see different manifest-rev commits and HEAD revisions,
    # except for the repository whose version was locked at a tag.
    assert ur.nt_mr_0 != ur.nt_mr_1, 'failed updating net-tools manifest-rev'
    assert ur.kl_mr_0 != ur.kl_mr_1, 'failed updating kconfiglib manifest-rev'
    assert ur.tr_mr_0 == ur.tr_mr_1, 'tagged_repo manifest-rev changed'
    assert ur.nt_head_0 != ur.nt_head_1, 'failed updating net-tools HEAD'
    assert ur.kl_head_0 != ur.kl_head_1, 'failed updating kconfiglib HEAD'
    assert ur.tr_head_0 == ur.tr_head_1, 'tagged_repo HEAD changed'

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
    checkout_branch('tagged_repo', 'local_tagged_repo_test_branch',
                    create=True)
    add_commit('net-tools', 'test local branch commit', reconfigure=True)
    add_commit('subdir/Kconfiglib', 'test local branch commit',
               reconfigure=True)
    add_commit('tagged_repo', 'test local branch commit',
               reconfigure=True)
    net_tools_prev = head_subject('net-tools')
    kconfiglib_prev = head_subject('subdir/Kconfiglib')
    tagged_repo_prev = head_subject('tagged_repo')

    # Update the upstream repositories, getting an UpdateResults tuple
    # back.
    ur = update_helper(west_init_tmpdir)

    # We updated all the repositories, so all paths and commits should
    # be valid refs (i.e. there shouldn't be a None or empty string
    # value in a ur attribute).
    assert all(ur)

    # Verify each repository has moved to a new manifest-rev,
    # except tagged_repo, which has a manifest-rev locked to a tag.
    # Its HEAD should change, though.
    assert ur.nt_mr_0 != ur.nt_mr_1, 'failed updating net-tools manifest-rev'
    assert ur.kl_mr_0 != ur.kl_mr_1, 'failed updating kconfiglib manifest-rev'
    assert ur.tr_mr_0 == ur.tr_mr_1, 'tagged_repo manifest-rev changed'
    assert ur.nt_head_0 != ur.nt_head_1, 'failed updating net-tools HEAD'
    assert ur.kl_head_0 != ur.kl_head_1, 'failed updating kconfiglib HEAD'
    assert ur.tr_head_0 != ur.tr_head_1, 'failed updating tagged_repo HEAD'

    # Verify local branch is still present and untouched
    assert net_tools_prev != head_subject('net-tools')
    assert kconfiglib_prev != head_subject('subdir/Kconfiglib')
    assert tagged_repo_prev != head_subject('tagged_repo')
    checkout_branch('net-tools', 'local_net_tools_test_branch')
    checkout_branch('subdir/Kconfiglib', 'local_kconfig_test_branch')
    checkout_branch('tagged_repo', 'local_tagged_repo_test_branch')
    assert net_tools_prev == head_subject('net-tools')
    assert kconfiglib_prev == head_subject('subdir/Kconfiglib')
    assert tagged_repo_prev == head_subject('tagged_repo')

def test_update_tag_to_tag(west_init_tmpdir):
    # Verify we can update the tagged_repo repo to a new tag.

    # We only need to clone tagged_repo locally.
    cmd('update tagged_repo')

    def updater(remotes):
        # Create a v2.0 tag on the remote tagged_repo repository.
        add_commit(remotes.tagged_repo, 'another tagged_repo tagged commit')
        add_tag(remotes.tagged_repo, 'v2.0')

        # Update the manifest file to point the project's revision at
        # the new tag.
        manifest = Manifest.from_topdir(topdir=west_init_tmpdir)
        for p in manifest.projects:
            if p.name == 'tagged_repo':
                p.revision = 'v2.0'
                break
        else:
            assert False, 'no tagged_repo'
        with open(west_init_tmpdir / 'zephyr' / 'west.yml', 'w') as f:
            f.write(manifest.as_yaml())  # NOT as_frozen_yaml().

        # Pull down the v2.0 tag into west_init_tmpdir.
        cmd('update tagged_repo')
    ur = update_helper(west_init_tmpdir, updater=updater)

    # Make sure we have manifest-rev and HEADs before and after.
    assert ur.tr_mr_0
    assert ur.tr_mr_1
    assert ur.tr_head_0
    assert ur.tr_head_1

    # Make sure we have v1.0 and v2.0 tags locally.
    v1_0 = check_output([GIT, 'rev-parse', 'refs/tags/v1.0^{commit}'],
                        cwd=ur.tr_local)
    v2_0 = check_output([GIT, 'rev-parse', 'refs/tags/v2.0^{commit}'],
                        cwd=ur.tr_local)
    assert v1_0
    assert v2_0

    # Check that all the updates (including the first one in this
    # function) behaved correctly.
    assert ur.tr_mr_0 == v1_0
    assert ur.tr_mr_1 == v2_0
    assert ur.tr_head_0 == v1_0
    assert ur.tr_head_1 == v2_0

def test_update_head_0(west_init_tmpdir):
    # Verify that using HEAD~0 as the revision causes west to not touch the
    # local repo. In this case, local zephyr should remain unchanged, even
    # though it is referenced in the new top-level manifest 'my_manifest',
    # since it's referenced with HEAD~0. Check that a local commit remains, and
    # that local changes are kept as is. An invalid url ('na') is used to check
    # that west doesn't attempt to access it.
    # Expect only Kconfiglib to be updated, since it's mentioned in the new
    # manifest's name-allowlist.
    # Note: HEAD~0 is used instead of HEAD because HEAD causes west to instead
    # fetch the remote HEAD.
    # In git, HEAD is a reference, whereas HEAD~<n> is a valid revision but
    # not a reference. West fetches references, such as refs/heads/main or
    # HEAD, and commits not available locally, but will not fetch commits if
    # they are already available.
    # HEAD~0 is resolved to a specific commit that is locally available, and
    # therefore west will simply checkout the locally available commit,
    # identified by HEAD~0.

    # create local repositories
    cmd('update')

    local_zephyr = west_init_tmpdir / "zephyr"

    add_commit(local_zephyr, "local commit")
    local_commit = check_output([GIT, 'rev-parse', 'HEAD'], cwd=local_zephyr)

    with open(local_zephyr / "CODEOWNERS", 'a') as f:
        f.write("\n") # Make a local change (add a newline)

    my_manifest_dir = west_init_tmpdir / "my_manifest"
    my_manifest_dir.mkdir()

    my_manifest = Path(my_manifest_dir / "west.yml")
    my_manifest.write_text(
        '''
          manifest:
            projects:
              - name: zephyr
                revision: HEAD~0
                url: na
                import:
                  name-allowlist:
                    - Kconfiglib
        ''')

    cmd(["config", "manifest.path", my_manifest_dir])

    # Update the upstream repositories, getting an UpdateResults tuple
    # back.
    ur = update_helper(west_init_tmpdir)

    assert all(ur)

    assert ur.nt_mr_0 == ur.nt_mr_1, 'net-tools manifest-rev changed'
    assert ur.kl_mr_0 != ur.kl_mr_1, 'failed updating kconfiglib manifest-rev'
    assert ur.tr_mr_0 == ur.tr_mr_1, 'tagged_repo manifest-rev changed'
    assert ur.nt_head_0 == ur.nt_head_1, 'net-tools HEAD changed'
    assert ur.kl_head_0 != ur.kl_head_1, 'failed updating kconfiglib HEAD'
    assert ur.tr_head_0 == ur.tr_head_1, 'tagged_repo HEAD changed'

    local_commit2 = check_output([GIT, 'rev-parse', 'HEAD'], cwd=local_zephyr)
    modified_files = check_output([GIT, 'status', '--porcelain'],
                                  cwd=local_zephyr)

    assert local_commit == local_commit2, 'zephyr local commit changed'
    assert modified_files.strip() == "M CODEOWNERS", \
           'local zephyr change not preserved'

def test_update_some_with_imports(repos_tmpdir):
    # 'west update project1 project2' should work fine even when
    # imports are used, as long as the relevant projects are all
    # defined in the manifest repository.
    #
    # It currently should fail with a helpful message if the projects
    # are resolved via project imports.

    remotes = repos_tmpdir / 'repos'
    zephyr = remotes / 'zephyr'
    net_tools = remotes / 'net-tools'

    ws = repos_tmpdir / 'ws'
    create_workspace(ws)
    manifest_repo = ws / 'mp'
    create_repo(manifest_repo)
    add_commit(manifest_repo, 'manifest repo commit',
               # zephyr revision is implicitly master:
               files={'west.yml':
                      f'''
                      manifest:
                        projects:
                        - name: zephyr
                          url: {zephyr}
                          import: true
                        self:
                          import: foo.yml
                      ''',
                      'foo.yml':
                      f'''
                      manifest:
                        projects:
                        - name: net-tools
                          url: {net_tools}
                      '''})

    cmd(['init', '-l', manifest_repo])

    # Updating unknown projects should fail as always.

    with pytest.raises(subprocess.CalledProcessError):
        cmd('update unknown-project', cwd=ws)

    # Updating a list of projects when some are resolved via project
    # imports must fail.

    with pytest.raises(subprocess.CalledProcessError):
        cmd('update Kconfiglib net-tools', cwd=ws)

    # Updates of projects defined in the manifest repository or all
    # projects must succeed, and behave the same as if no imports
    # existed.

    cmd('update net-tools', cwd=ws)
    with pytest.raises(ManifestImportFailed):
        Manifest.from_topdir(topdir=ws)
    manifest = Manifest.from_topdir(topdir=ws,
                                    import_flags=MIF.IGNORE_PROJECTS)
    projects = manifest.get_projects(['net-tools', 'zephyr'])
    net_tools_project = projects[0]
    zephyr_project = projects[1]
    assert net_tools_project.is_cloned()
    assert not zephyr_project.is_cloned()

    cmd('update zephyr', cwd=ws)
    assert zephyr_project.is_cloned()

    cmd('update', cwd=ws)
    manifest = Manifest.from_topdir(topdir=ws)
    assert manifest.get_projects(['Kconfiglib'])[0].is_cloned()

def test_update_submodules_list(repos_tmpdir):
    # The west update command should not only update projects,
    # but also its submodules. Test uses two pairs of project
    # and submodule checking out submodule in the default
    # and custom location to verify both cases.

    # Repositories paths
    remotes = repos_tmpdir / 'repos'
    # tagged_repo is zephyr's submodule and will be updated
    # in the default location (i.e. zephyr/tagged_repo).
    zephyr = remotes / 'zephyr'
    tagged_repo = remotes / 'tagged_repo'
    # Kconfiglib is net_tools's submodule and will be updated
    # in the custom location (i.e. net-tools/third_parties/Kconfiglib).
    net_tools = remotes / 'net-tools'
    kconfiglib = remotes / 'Kconfiglib'
    kconfiglib_submodule = 'third_parties/Kconfiglib'

    # Creating west workspace and manifest repository.
    ws = repos_tmpdir / 'ws'
    create_workspace(ws)
    manifest_repo = ws / 'mp'
    create_repo(manifest_repo)

    # Commit west.yml describing projects and submodules dependencies.
    add_commit(manifest_repo, 'manifest repo commit',
               files={'west.yml':
                      f'''
                      manifest:
                        projects:
                        - name: zephyr
                          url: {zephyr}
                          submodules:
                            - path: tagged_repo
                        - name: net-tools
                          url: {net_tools}
                          submodules:
                            - name: Kconfiglib
                              path: {kconfiglib_submodule}
                       '''})
    cmd(['init', '-l', manifest_repo])

    # Make tagged_repo to be zephyr project submodule.
    subprocess.check_call(SUBMODULE_ADD +
                          [str(tagged_repo), 'tagged_repo'],
                          cwd=zephyr)
    # Commit changes to the zephyr repo.
    add_commit(zephyr, 'zephyr submodule change commit')

    # Make Kconfiglib to be net-tools project submodule.
    subprocess.check_call(SUBMODULE_ADD +
                          [str(kconfiglib), kconfiglib_submodule],
                          cwd=net_tools)
    # Commit changes to the net-tools repo.
    add_commit(net_tools, 'net-tools submodule change commit')

    # Get parsed data from the manifest.
    manifest = Manifest.from_topdir(topdir=ws,
                                    import_flags=MIF.IGNORE_PROJECTS)
    projects = manifest.get_projects(['zephyr', 'net-tools'])
    zephyr_project = projects[0]
    net_tools_project = projects[1]

    # Verify if tagged_repo submodule data are correct.
    assert zephyr_project.submodules
    assert zephyr_project.submodules[0].name is None
    assert zephyr_project.submodules[0].path == 'tagged_repo'
    # Verify if Kconfiglib submodule data are correct.
    assert net_tools_project.submodules
    assert net_tools_project.submodules[0].name == 'Kconfiglib'
    assert net_tools_project.submodules[0].path == kconfiglib_submodule

    # Check if projects repos are cloned - should not be.
    assert not zephyr_project.is_cloned()
    assert not net_tools_project.is_cloned()

    # Update only zephyr project.
    cmd(f'update {PROTOCOL_FILE_ALLOW} zephyr', cwd=ws)

    # Verify if only zephyr project was cloned.
    assert zephyr_project.is_cloned()
    assert not net_tools_project.is_cloned()

    # Verify if tagged-repo submodule was also cloned
    res = zephyr_project.git('rev-parse --show-cdup', check=False,
                             cwd=os.path.join(ws, 'zephyr', 'tagged_repo'),
                             capture_stderr=True, capture_stdout=True)
    assert not (res.returncode or res.stdout.strip())

    # Update all projects
    cmd(f'update {PROTOCOL_FILE_ALLOW}', cwd=ws)

    # Verify if both projects were cloned
    assert zephyr_project.is_cloned()
    assert net_tools_project.is_cloned()

    # Verify if Kconfiglib submodule was also cloned
    res = net_tools_project.git('rev-parse --show-cdup', check=False,
                                cwd=os.path.join(ws, 'net-tools',
                                                 kconfiglib_submodule),
                                capture_stderr=True, capture_stdout=True)
    assert not (res.returncode or res.stdout.strip())

    # Test freeze output with submodules
    # see test_manifest_freeze for details
    actual = cmd('manifest --freeze', cwd=ws).splitlines()
    expected_res = ['^manifest:$',
                    '^  projects:$',
                    '^  - name: zephyr$',
                    f'^    url: {re.escape(str(zephyr))}$',
                    '^    revision: [a-f0-9]{40}$',
                    '^    submodules:$',
                    '^    - path: tagged_repo$',
                    '^  - name: net-tools$',
                    f'^    url: {re.escape(str(net_tools))}$',
                    '^    revision: [a-f0-9]{40}$',
                    '^    submodules:$',
                    f'^    - path: {re.escape(str(kconfiglib_submodule))}$',
                    '^      name: Kconfiglib$',
                    '^  self:$',
                    '^    path: mp$']
    _match_multiline_regex(expected_res, actual)

def test_update_all_submodules(repos_tmpdir):
    # The west update command should not only update projects,
    # but also its submodules. Test verifies whether setting submodules
    # value to boolean True results in updating all project submodules
    # (even without specifying them implicitly as a list). Moreover it checks
    # if submodules are updated recursively.

    # Repositories paths
    remotes = repos_tmpdir / 'repos'
    # tagged_repo and net_tools are zephyr's submodules
    zephyr = remotes / 'zephyr'
    tagged_repo = remotes / 'tagged_repo'
    net_tools = remotes / 'net-tools'
    # Kconfiglib is net-tools submodule
    kconfiglib = remotes / 'Kconfiglib'

    # Creating west workspace and manifest repository.
    ws = repos_tmpdir / 'ws'
    create_workspace(ws)
    manifest_repo = ws / 'mp'
    create_repo(manifest_repo)

    # Commit west.yml describing projects and submodules dependencies.
    add_commit(manifest_repo, 'manifest repo commit',
               files={'west.yml':
                      f'''
                      manifest:
                        projects:
                        - name: zephyr
                          url: {zephyr}
                          submodules: true
                       '''})
    cmd(['init', '-l', manifest_repo])

    # Make tagged_repo to be zephyr project submodule.
    subprocess.check_call(SUBMODULE_ADD +
                          [str(tagged_repo), 'tagged_repo'],
                          cwd=zephyr)
    # Commit changes to the zephyr repo.
    add_commit(zephyr, 'zephyr submodule tagged_repo commit')

    # Make Kconfiglib to be net_tools submodule.
    subprocess.check_call(SUBMODULE_ADD +
                          [str(kconfiglib), 'Kconfiglib'],
                          cwd=net_tools)
    # Commit changes to the net_tools repo.
    add_commit(net_tools, 'net_tools submodule Kconfiglib commit')

    # Make net_tools to be zephyr project submodule.
    subprocess.check_call(SUBMODULE_ADD +
                          [str(net_tools), 'net-tools'],
                          cwd=zephyr)
    # Commit changes to the zephyr repo.
    add_commit(zephyr, 'zephyr submodule net-tools commit')

    # Get parsed data from the manifest.
    manifest = Manifest.from_topdir(topdir=ws,
                                    import_flags=MIF.IGNORE_PROJECTS)
    projects = manifest.get_projects(['zephyr'])
    zephyr_project = projects[0]

    # Verify if zephyr has submodules.
    assert zephyr_project.submodules

    # Check if project repo is cloned - should not be.
    assert not zephyr_project.is_cloned()

    # Update zephyr project.
    cmd(f'update {PROTOCOL_FILE_ALLOW} zephyr', cwd=ws)

    # Verify if zephyr project was cloned.
    assert zephyr_project.is_cloned()

    # Verify if tagged-repo submodule was also cloned.
    res = zephyr_project.git('rev-parse --show-cdup', check=False,
                             cwd=os.path.join(ws, 'zephyr', 'tagged_repo'),
                             capture_stderr=True, capture_stdout=True)
    assert not (res.returncode or res.stdout.strip())

    # Verify if net-tools submodule was also cloned.
    res = zephyr_project.git('rev-parse --show-cdup', check=False,
                             cwd=os.path.join(ws, 'zephyr', 'net-tools'),
                             capture_stderr=True, capture_stdout=True)
    assert not (res.returncode or res.stdout.strip())

    # Verify if Kconfiglib submodule was also cloned, as a result of recursive
    # update.
    res = zephyr_project.git('rev-parse --show-cdup', check=False,
                             cwd=os.path.join(ws, 'zephyr', 'net-tools',
                                              'Kconfiglib'),
                             capture_stderr=True, capture_stdout=True)
    assert not (res.returncode or res.stdout.strip())

    # Test freeze output with submodules
    # see test_manifest_freeze for details
    actual = cmd('manifest --freeze', cwd=ws).splitlines()
    expected_res = ['^manifest:$',
                    '^  projects:$',
                    '^  - name: zephyr$',
                    f'^    url: {re.escape(str(zephyr))}$',
                    '^    revision: [a-f0-9]{40}$',
                    '^    submodules: true$',
                    '^  self:$',
                    '^    path: mp$']
    _match_multiline_regex(expected_res, actual)

def test_update_no_submodules(repos_tmpdir):
    # Test verifies whether setting submodules value to boolean False does not
    # result in updating project submodules.

    # Repositories paths
    remotes = repos_tmpdir / 'repos'
    # tagged_repo and net_tools are zephyr's submodules
    zephyr = remotes / 'zephyr'
    tagged_repo = remotes / 'tagged_repo'
    net_tools = remotes / 'net-tools'

    # Creating west workspace and manifest repository.
    ws = repos_tmpdir / 'ws'
    create_workspace(ws)
    manifest_repo = ws / 'mp'
    create_repo(manifest_repo)

    # Commit west.yml describing projects and submodules dependencies.
    add_commit(manifest_repo, 'manifest repo commit',
               files={'west.yml':
                      f'''
                      manifest:
                        projects:
                        - name: zephyr
                          url: {zephyr}
                          submodules: false
                       '''})
    cmd(['init', '-l', manifest_repo])

    # Make tagged_repo to be zephyr project submodule.
    subprocess.check_call(SUBMODULE_ADD +
                          [str(tagged_repo), 'tagged_repo'],
                          cwd=zephyr)
    # Commit changes to the zephyr repo.
    add_commit(zephyr, 'zephyr submodule tagged_repo commit')

    # Make net_tools to be zephyr project submodule.
    subprocess.check_call(SUBMODULE_ADD +
                          [str(net_tools), 'net-tools'],
                          cwd=zephyr)
    # Commit changes to the zephyr repo.
    add_commit(zephyr, 'zephyr submodule net-tools commit')

    # Get parsed data from the manifest.
    manifest = Manifest.from_topdir(topdir=ws,
                                    import_flags=MIF.IGNORE_PROJECTS)
    projects = manifest.get_projects(['zephyr'])
    zephyr_project = projects[0]

    # Check if project repo is cloned - should not be.
    assert not zephyr_project.is_cloned()

    # Update zephyr project.
    cmd('update zephyr', cwd=ws)

    # Verify if zephyr project was cloned.
    assert zephyr_project.is_cloned()

    # Verify if tagged-repo submodule was also cloned (should not be).
    res = zephyr_project.git('rev-parse --show-cdup', check=False,
                             cwd=os.path.join(ws, 'zephyr', 'tagged_repo'),
                             capture_stderr=True, capture_stdout=True)
    assert (res.returncode or res.stdout.strip())

    # Verify if net-tools submodule was also cloned (should not be).
    res = zephyr_project.git('rev-parse --show-cdup', check=False,
                             cwd=os.path.join(ws, 'zephyr', 'net-tools'),
                             capture_stderr=True, capture_stdout=True)
    assert (res.returncode or res.stdout.strip())

    # Test freeze output with submodules
    # see test_manifest_freeze for details
    actual = cmd('manifest --freeze', cwd=ws).splitlines()
    expected_res = ['^manifest:$',
                    '^  projects:$',
                    '^  - name: zephyr$',
                    f'^    url: {re.escape(str(zephyr))}$',
                    '^    revision: [a-f0-9]{40}$',
                    '^  self:$',
                    '^    path: mp$']
    _match_multiline_regex(expected_res, actual)

def test_update_submodules_strategy(repos_tmpdir):
    # The west update command is able to update submodules using default
    # checkout strategy or rebase strategy, selected by adding -r argument
    # to the invoked command. Test verifies if both strategies are working
    # properly when updating submodules.

    # Repositories paths
    remotes = repos_tmpdir / 'repos'
    # tagged_repo is zephyr's submodule
    zephyr = remotes / 'zephyr'
    tagged_repo = remotes / 'tagged_repo'
    # Kconfiglib is net_tools's submodule
    net_tools = remotes / 'net-tools'
    kconfiglib = remotes / 'Kconfiglib'

    # Creating west workspace and manifest repository.
    ws = repos_tmpdir / 'ws'
    create_workspace(ws)
    manifest_repo = ws / 'mp'
    create_repo(manifest_repo)

    tagged_repo_dst_dir = os.path.join(ws, 'zephyr', 'tagged_repo')
    kconfiglib_dst_dir = os.path.join(ws, 'net-tools', 'Kconfiglib')

    # Commit west.yml describing projects and submodules dependencies.
    add_commit(manifest_repo, 'manifest repo commit',
               files={'west.yml':
                      f'''
                      manifest:
                        projects:
                        - name: zephyr
                          url: {zephyr}
                          submodules:
                            - name: tagged_repo
                              path: tagged_repo
                        - name: net-tools
                          url: {net_tools}
                          submodules:
                            - name: Kconfiglib
                              path: Kconfiglib
                       '''})
    cmd(['init', '-l', manifest_repo])

    # Make tagged_repo to be zephyr project submodule.
    subprocess.check_call(SUBMODULE_ADD +
                          [str(tagged_repo), 'tagged_repo'],
                          cwd=zephyr)
    # Commit changes to the zephyr repo.
    add_commit(zephyr, 'zephyr submodule change commit')

    # Make Kconfiglib to be net-tools project submodule.
    subprocess.check_call(SUBMODULE_ADD +
                          [str(kconfiglib), 'Kconfiglib'],
                          cwd=net_tools)
    # Commit changes to the net-tools repo.
    add_commit(net_tools, 'net-tools submodule change commit')

    # Get parsed data from the manifest.
    manifest = Manifest.from_topdir(topdir=ws,
                                    import_flags=MIF.IGNORE_PROJECTS)
    projects = manifest.get_projects(['zephyr', 'net-tools'])
    zephyr_project = projects[0]
    net_tools_project = projects[1]

    # Check if projects repos are cloned - should not be.
    assert not zephyr_project.is_cloned()
    assert not net_tools_project.is_cloned()

    # Update only zephyr project using checkout strategy (selected by default).
    cmd(f'update {PROTOCOL_FILE_ALLOW} zephyr', cwd=ws)

    # Verify if only zephyr project was cloned.
    assert zephyr_project.is_cloned()
    assert not net_tools_project.is_cloned()

    # Verify if tagged-repo submodule was also cloned
    res = zephyr_project.git('rev-parse --show-cdup', check=False,
                             cwd=tagged_repo_dst_dir, capture_stderr=True,
                             capture_stdout=True)
    assert not (res.returncode or res.stdout.strip())

    # Update only net-tools project using rebase strategy
    cmd(f'update {PROTOCOL_FILE_ALLOW} net-tools -r', cwd=ws)

    # Verify if both projects were cloned
    assert zephyr_project.is_cloned()
    assert net_tools_project.is_cloned()

    # Verify if Kconfiglib submodule was also cloned
    res = net_tools_project.git('rev-parse --show-cdup', check=False,
                                cwd=kconfiglib_dst_dir, capture_stderr=True,
                                capture_stdout=True)
    assert not (res.returncode or res.stdout.strip())

    # Save submodules HEAD revisions sha for verification purposes
    tagged_repo_head_sha = zephyr_project.sha('HEAD', cwd=tagged_repo_dst_dir)
    kconfiglib_head_sha = net_tools_project.sha('HEAD', cwd=kconfiglib_dst_dir)

    # Add commits to the submodules repos to modify their revisions
    add_commit(tagged_repo_dst_dir, 'tagged_repo test commit',
               files={'test.txt': "Test message"})
    add_commit(kconfiglib_dst_dir, 'Kconfiglib test commit',
               files={'test.txt': "Test message"})

    # Save submodules revisions sha after commit for verification purposes
    tagged_repo_new_sha = zephyr_project.sha('HEAD', cwd=tagged_repo_dst_dir)
    kconfiglib_new_sha = net_tools_project.sha('HEAD', cwd=kconfiglib_dst_dir)

    # Verify whether new revisions sha are different from the HEAD
    assert tagged_repo_head_sha != tagged_repo_new_sha
    assert kconfiglib_head_sha != kconfiglib_new_sha

    # Update only zephyr project using checkout strategy (selected by default).
    cmd('update zephyr', cwd=ws)

    # Verify if current submodule revision is HEAD, as checkout should drop
    # added commit.
    assert zephyr_project.sha('HEAD', cwd=tagged_repo_dst_dir) \
           == tagged_repo_head_sha

    # Update only net-tools project using rebase strategy
    cmd('update net-tools -r', cwd=ws)

    # Verify if current submodule revision is set to added commit, as rebase
    # should not drop it.
    assert net_tools_project.sha('HEAD', cwd=kconfiglib_dst_dir) \
           == kconfiglib_new_sha

    # Test freeze output with submodules
    # see test_manifest_freeze for details
    actual = cmd('manifest --freeze', cwd=ws).splitlines()
    expected_res = ['^manifest:$',
                    '^  projects:$',
                    '^  - name: zephyr$',
                    f'^    url: {re.escape(str(zephyr))}$',
                    '^    revision: [a-f0-9]{40}$',
                    '^    submodules:$',
                    '^    - path: tagged_repo$',
                    '^      name: tagged_repo$',
                    '^  - name: net-tools$',
                    f'^    url: {re.escape(str(net_tools))}$',
                    '^    revision: [a-f0-9]{40}$',
                    '^    submodules:$',
                    '^    - path: Kconfiglib$',
                    '^      name: Kconfiglib$',
                    '^  self:$',
                    '^    path: mp$']
    _match_multiline_regex(expected_res, actual)

@pytest.mark.xfail
def test_update_submodules_relpath(tmpdir):
    # Regression test for
    # https://github.com/zephyrproject-rtos/west/issues/545.
    #
    # We need to make sure that submodules with relative paths are
    # initialized correctly even when ther is no "origin" remote.
    #
    # Background: unless the config variable git.$SUBMODULENAME.url is
    # set, "git submodule init" relies on the "default remote" for
    # figuring out how to turn that relative path into something
    # useful. The "default remote" ends up being "origin" in the
    # situation where we are initializing a submodule from a
    # superproject which is a west project.
    #
    # However, west neither makes nor expects any guarantees
    # about any particular git remotes being present in a project, so
    # that only works if we set the config variable before
    # initializing the module.

    # The following paths begin with 'project' if they are west projects,
    # and 'submodule' if they are direct or transitive submodules of a
    # west project. Numeric suffixes reflect the tree structure of the
    # workspace:
    #
    # workspace
    # ├── manifest
    # ├── project-1
    # │   └── submodule-1-1
    # └── project-2
    #     ├── submodule-2-1
    #     └── submodule-2-2
    #         └── submodule-2-2-1
    #
    # Some of them are declared to west directly, some implicitly.
    pseudo_remotes = tmpdir / 'remotes'  # parent directory for 'remote' repos
    remote_repositories = [
        pseudo_remotes / path for path in
        ['manifest',
         'project-1',
         'submodule-1-1',
         'project-2',
         'submodule-2-1',
         'submodule-2-2',
         'submodule-2-2-1',
         ]
    ]
    for remote_repo in remote_repositories:
        create_repo(remote_repo)

    add_commit(pseudo_remotes / 'manifest', 'manifest west.yml',
               files={'west.yml': f'''
manifest:
  remotes:
    - name: not-origin
      url-base: file://{pseudo_remotes}
  defaults:
    remote: not-origin
  projects:
    - name: project-1
      submodules: true
    - name: project-2
      submodules:
        - path: submodule-2-1
        - name: sub-2-2
          path: submodule-2-2
'''})

    def add_submodule(superproject, submodule_name,
                      submodule_url, submodule_path):
        subprocess.check_call([GIT, '-C', os.fspath(superproject),
                               'submodule', 'add',
                               '--name', submodule_name,
                               submodule_url, submodule_path])
        add_commit(superproject, f'add submodule {submodule_name}')

    add_submodule(pseudo_remotes / 'project-1', 'sub-1-1',
                  '../submodule-1-1', 'submodule-1-1')
    add_submodule(pseudo_remotes / 'project-2', 'sub-2-1',
                  '../submodule-2-1', 'submodule-2-1')
    add_submodule(pseudo_remotes / 'project-2', 'sub-2-2',
                  '../submodule-2-2', 'submodule-2-2')
    add_submodule(pseudo_remotes / 'project-2' / 'submodule-2-2', 'sub-2-2-1',
                  '../submodule-2-2-1', 'submodule-2-2-1')

    workspace = tmpdir / 'workspace'
    remote_manifest = pseudo_remotes / 'manifest'
    cmd(f'init -m "{remote_manifest}" "{workspace}"')

    workspace.chdir()
    cmd('update')
    expected_dirs = [
        workspace / expected for expected in
        ['project-1',
         'project-1' / 'submodule-1-1',
         'project-2',
         'project-2' / 'submodule-2-1',
         'project-2' / 'submodule-2-2',
         'project-2' / 'submodule-2-2' / 'submodule-2-2-1',
         ]
    ]
    for expected_dir in expected_dirs:
        expected_dir.check(dir=1)

def test_update_recovery(tmpdir):
    # Make sure that the final 'west update' can recover from the
    # following turn of events:
    #
    #   1. 'm' is the manifest repository, 'p' is a project
    #   2. m/west.yml imports p at revision 'rbad'; p/west.yml at rbad
    #      contains an invalid manifest
    #   3. user runs 'west update', setting p's manifest-rev to rbad
    #      (and failing the update)
    #   4. user updates m/west.yml to point at p revision 'rgood',
    #      which contains good manifest data
    #   5. user runs 'west update' again
    #
    # The 'west update' in the last step should fix p's manifest-rev,
    # pointing it at rgood, and should succeed.

    # create path objects and string representations
    workspace = Path(tmpdir) / 'workspace'
    workspacestr = os.fspath(workspace)

    m = workspace / 'm'
    p = workspace / 'p'

    # Set up the workspace repositories.
    workspace.mkdir()
    create_repo(m)
    create_repo(p)

    # Create revision rbad, which contains a bogus manifest, in p.
    add_commit(p, 'rbad commit message', files={'west.yml': 'bogus_data'},
               reconfigure=False)
    rbad = rev_parse(p, 'HEAD')

    # Create revision rgood, which contains a good manifest, in p.
    add_commit(p, 'rgood commit message',
               files={'west.yml': 'manifest:\n  projects: []'},
               reconfigure=False)
    rgood = rev_parse(p, 'HEAD')

    # Set up the initial, 'bad' manifest.
    #
    # Use an invalid local file as the fetch URL: there's no reason
    # west should be fetching from the remote.
    with open(m / 'west.yml', 'w') as m_manifest:
        m_manifest.write(f'''
        manifest:
          projects:
          - name: p
            url: file://{tmpdir}/should-not-be-fetched
            revision: {rbad}
            import: true
        ''')

    # Use west init -l + west update to point p's manifest-rev at rbad.
    cmd(['init', '-l', m], cwd=workspacestr)
    with pytest.raises(subprocess.CalledProcessError):
        cmd('update', cwd=workspacestr)

    # Make sure p's manifest-rev points to the bad revision as expected.
    prev = rev_parse(p, 'refs/heads/manifest-rev')
    assert prev == rbad

    # Fix the main manifest to point at rgood.
    with open(m / 'west.yml', 'w') as m_manifest:
        m_manifest.write(f'''
        manifest:
          projects:
          - name: p
            url: file://{tmpdir}/should-not-be-fetched
            revision: {rgood}
            import: true
        ''')

    # Run the update, making sure it succeeds and p's manifest-rev
    # is fixed.
    cmd('update', cwd=workspacestr)
    prev = rev_parse(p, 'refs/heads/manifest-rev')
    assert prev == rgood


def setup_cache_workspace(workspace, remote, foo_head, bar_head):
    # Shared helper code that sets up a workspace used to test the
    # 'west update --foo-cache' options.

    create_workspace(workspace)

    manifest_project = workspace / 'mp'
    with open(manifest_project / 'west.yml', 'w') as f:
        f.write(f'''
        manifest:
          projects:
          - name: foo
            path: subdir/foo
            url: file://{remote}
            revision: {foo_head}
          - name: bar
            url: file://{remote}
            revision: {bar_head}
        ''')


def test_update_name_cache(tmpdir):
    # Test that 'west update --name-cache' works and doesn't hit the
    # network if it doesn't have to.

    remote = tmpdir / 'remote'
    create_repo(remote)
    name_cache_dir = tmpdir / 'name_cache'
    create_repo(name_cache_dir / 'foo')
    create_repo(name_cache_dir / 'bar')
    foo_head = rev_parse(name_cache_dir / 'foo', 'HEAD')
    bar_head = rev_parse(name_cache_dir / 'bar', 'HEAD')

    workspace = tmpdir / 'workspace'
    setup_cache_workspace(workspace, remote, foo_head, bar_head)
    workspace.chdir()
    foo = workspace / 'subdir' / 'foo'
    bar = workspace / 'bar'

    # Test the command line option.
    cmd(['update', '--name-cache', name_cache_dir])
    assert foo.check(dir=1)
    assert bar.check(dir=1)
    assert rev_parse(foo, 'HEAD') == foo_head
    assert rev_parse(bar, 'HEAD') == bar_head

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


def test_update_path_cache(tmpdir):
    # Test that 'west update --path-cache' works and doesn't hit the
    # network if it doesn't have to.

    remote = tmpdir / 'remote'
    create_repo(remote)
    path_cache_dir = tmpdir / 'path_cache_dir'
    create_repo(path_cache_dir / 'subdir' / 'foo')
    create_repo(path_cache_dir / 'bar')
    foo_head = rev_parse(path_cache_dir / 'subdir' / 'foo', 'HEAD')
    bar_head = rev_parse(path_cache_dir / 'bar', 'HEAD')

    workspace = tmpdir / 'workspace'
    setup_cache_workspace(workspace, remote, foo_head, bar_head)
    workspace.chdir()
    foo = workspace / 'subdir' / 'foo'
    bar = workspace / 'bar'

    # Test the command line option.
    cmd(['update', '--path-cache', path_cache_dir])
    assert foo.check(dir=1)
    assert bar.check(dir=1)
    assert rev_parse(foo, 'HEAD') == foo_head
    assert rev_parse(bar, 'HEAD') == bar_head

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


def setup_narrow(tmpdir):
    # Helper used by test_update_narrow() and test_update_narrow_depth1().

    remote = tmpdir / 'remote'

    create_repo(remote)
    add_commit(remote, 'second commit, main')
    add_tag(remote, 'tag')

    create_branch(remote, 'branch', checkout=True)
    add_commit(remote, 'second commit, branch', reconfigure=False)

    workspace = tmpdir / 'workspace'
    create_workspace(workspace)

    with open(workspace / 'mp' / 'west.yml', 'w') as f:
        f.write(f'''
        manifest:
          projects:
            - name: project
              revision: branch
              url: file://{remote}
        ''')

    return remote, workspace


def test_update_narrow(tmpdir):
    # Test that 'west update --narrow' doesn't fetch tags, and that
    # 'west update' respects the 'update.narrow' config option.

    remote, workspace = setup_narrow(tmpdir)
    workspace.chdir()

    def project_tags():
        return subprocess.check_output(
            [GIT, 'tag', '--list'], cwd=workspace / 'project'
        ).decode().splitlines()

    cmd('update --narrow')
    assert project_tags() == []

    cmd('config update.narrow true')
    cmd('update')
    assert project_tags() == []

    cmd('config update.narrow false')
    cmd('update')
    assert project_tags() != []


def test_update_narrow_depth1(tmpdir):
    # Test that 'west update --narrow -o=--depth=1' fetches exactly
    # one commit, regardless of how many there are in the remote
    # repository.

    remote, workspace = setup_narrow(tmpdir)

    cmd('update --narrow --fetch-opt=--depth=1', cwd=workspace)

    refs = subprocess.check_output(
        [GIT, 'for-each-ref'], cwd=workspace / 'project',
    ).decode().splitlines()

    assert len(refs) == 1


def test_init_again(west_init_tmpdir):
    # Test that 'west init' on an initialized tmpdir errors out
    # with a message that indicates it's already initialized.

    popen = subprocess.Popen('west init'.split(),
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE,
                             cwd=west_init_tmpdir)
    _, stderr = popen.communicate()
    assert popen.returncode
    assert b'already initialized' in stderr

    popen = subprocess.Popen('west init -m http://example.com'.split(),
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE,
                             cwd=west_init_tmpdir)
    _, stderr = popen.communicate()
    assert popen.returncode
    assert b'already initialized' in stderr

    manifest = west_init_tmpdir / '..' / 'repos' / 'zephyr'
    popen = subprocess.Popen(
        ['west', '-vvv', 'init', '-m', str(manifest), 'workspace'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        cwd=west_init_tmpdir.dirname)
    _, stderr = popen.communicate()
    assert popen.returncode
    assert b'already initialized' in stderr

def test_init_local_manifest_project(repos_tmpdir):
    # Do a local clone of manifest repo
    zephyr_install_dir = repos_tmpdir.join('workspace', 'zephyr')
    clone(str(repos_tmpdir.join('repos', 'zephyr')),
          str(zephyr_install_dir))

    cmd(['init', '-l', zephyr_install_dir])

    # Verify Zephyr has been installed during init -l, but not projects.
    zid = repos_tmpdir.join('workspace')
    assert zid.check(dir=1)
    assert zid.join('subdir', 'Kconfiglib').check(dir=0)
    assert zid.join('net-tools').check(dir=0)
    assert zid.join('tagged_repo').check(dir=0)
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
    assert zid.join('tagged_repo').check(dir=1)
    assert zid.join('subdir', 'Kconfiglib').check(dir=1)
    assert zid.join('subdir', 'Kconfiglib', '.git').check(dir=1)
    assert zid.join('subdir', 'Kconfiglib', 'kconfiglib.py').check(file=1)
    assert zid.join('net-tools').check(dir=1)
    assert zid.join('net-tools', '.git').check(dir=1)
    assert zid.join('net-tools', 'qemu-script.sh').check(file=1)
    assert zid.join('tagged_repo', 'test.txt').check(file=1)


def test_init_local_already_initialized_failure(west_init_tmpdir):
    # Test that 'west init -l' on an initialized tmpdir errors out
    with pytest.raises(subprocess.CalledProcessError):
        cmd(f'init -l "{west_init_tmpdir}"')


def test_init_local_missing_west_yml_failure(repos_tmpdir):
    # Test that 'west init -l' on repo without a 'west.yml' fails

    # Do a local clone of manifest repo
    zephyr_install_dir = repos_tmpdir.join('workspace', 'zephyr')
    clone(str(repos_tmpdir.join('repos', 'zephyr')),
          str(zephyr_install_dir))
    os.remove(str(zephyr_install_dir.join('west.yml')))

    with pytest.raises(subprocess.CalledProcessError):
        cmd(f'init -l "{zephyr_install_dir}"')


def test_init_local_with_manifest_filename(repos_tmpdir):
    # Test 'west init --mf -l' on a local repo

    manifest = repos_tmpdir / 'repos' / 'zephyr'
    workspace = repos_tmpdir / 'workspace'
    zephyr_install_dir = workspace / 'zephyr'

    # Do a local clone of manifest repo
    clone(str(manifest), str(zephyr_install_dir))
    os.rename(str(zephyr_install_dir / 'west.yml'),
              str(zephyr_install_dir / 'project.yml'))

    # fails because west.yml is missing
    with pytest.raises(subprocess.CalledProcessError):
        cmd(['init', '-l', zephyr_install_dir])

    # create a manifest with a syntax error so we can test if it's being parsed
    with open(zephyr_install_dir / 'west.yml', 'w') as f:
        f.write('[')

    cwd = os.getcwd()
    cmd(['init', '-l', zephyr_install_dir])

    # init with a local manifest doesn't parse the file, so let's access it
    workspace.chdir()
    with pytest.raises(subprocess.CalledProcessError):
        cmd('list')

    os.chdir(cwd)
    shutil.move(workspace / '.west', workspace / '.west-syntaxerror')

    # success
    cmd(['init', '--mf', 'project.yml', '-l', zephyr_install_dir])
    workspace.chdir()
    config.read_config()
    cmd('update')


def test_init_local_with_empty_path(repos_tmpdir):
    # Test "west init -l ." + "west update".
    # Regression test for:
    # https://github.com/zephyrproject-rtos/west/issues/435

    local_manifest_dir = repos_tmpdir / 'workspace' / 'zephyr'
    clone(str(repos_tmpdir / 'repos' / 'zephyr'), str(local_manifest_dir))
    os.chdir(local_manifest_dir)
    cmd('init -l .')
    cmd('update')
    assert (repos_tmpdir / 'workspace' / 'subdir' / 'Kconfiglib').check(dir=1)


def test_update_with_groups_enabled(west_init_tmpdir):
    # Test "west update" with increasing numbers of groups enabled.

    remotes = west_init_tmpdir / '..' / 'repos'

    with open(west_init_tmpdir / 'zephyr' / 'west.yml', 'w') as f:
        # The purpose of the 'disabled' group is to ensure that a
        # project's groups' "enabled" bits are ORed together, not
        # ANDed together, when deciding if the project is active.
        f.write(f'''
        manifest:
          defaults:
            remote: test-local
          remotes:
            - name: test-local
              url-base: {remotes}
          projects:
            - name: Kconfiglib
              revision: zephyr
              path: subdir/Kconfiglib
              groups:
              - enabled
              - disabled
            - name: tagged_repo
              revision: v1.0
              groups:
              - enable-on-cmd-line
              - disabled
            - name: net-tools
              groups:
              - enable-in-config-file
              - disabled
          group-filter: [-enable-on-cmd-line,-enable-in-config-file,-disabled]
          self:
            path: zephyr
        ''')

    cmd('update')
    assert (west_init_tmpdir / 'subdir' / 'Kconfiglib').check(dir=1)
    assert (west_init_tmpdir / 'tagged_repo').check(dir=0)
    assert (west_init_tmpdir / 'net-tools').check(dir=0)

    cmd('update --group-filter +enable-on-cmd-line')
    assert (west_init_tmpdir / 'tagged_repo').check(dir=1)
    assert (west_init_tmpdir / 'net-tools').check(dir=0)

    cmd('config manifest.group-filter +enable-in-config-file')
    cmd('update')
    assert (west_init_tmpdir / 'net-tools').check(dir=1)


def test_update_with_groups_disabled(west_init_tmpdir):
    # Test "west update" with decreasing numbers of groups disabled.

    remotes = west_init_tmpdir / '..' / 'repos'

    with open(west_init_tmpdir / 'zephyr' / 'west.yml', 'w') as f:
        f.write(f'''
        manifest:
          defaults:
            remote: test-local
          remotes:
            - name: test-local
              url-base: {remotes}
          projects:
            - name: Kconfiglib
              revision: zephyr
              path: subdir/Kconfiglib
              groups:
              - disabled
            - name: tagged_repo
              revision: v1.0
              groups:
              - disabled-on-cmd-line
            - name: net-tools
              groups:
              - disabled-in-config-file
          group-filter: [-disabled]
          self:
            path: zephyr
        ''')

    cmd('config manifest.group-filter -- -disabled-in-config-file')
    cmd('update --group-filter=-disabled-on-cmd-line')
    assert (west_init_tmpdir / 'subdir' / 'Kconfiglib').check(dir=0)
    assert (west_init_tmpdir / 'tagged_repo').check(dir=0)
    assert (west_init_tmpdir / 'net-tools').check(dir=0)

    cmd('config -d manifest.group-filter')
    cmd('update --group-filter=-disabled-on-cmd-line')
    assert (west_init_tmpdir / 'subdir' / 'Kconfiglib').check(dir=0)
    assert (west_init_tmpdir / 'tagged_repo').check(dir=0)
    assert (west_init_tmpdir / 'net-tools').check(dir=1)

    cmd('update')
    assert (west_init_tmpdir / 'subdir' / 'Kconfiglib').check(dir=0)
    assert (west_init_tmpdir / 'tagged_repo').check(dir=1)

    # Enabling overrides disabling.
    cmd('update --group-filter +disabled')
    assert (west_init_tmpdir / 'subdir' / 'Kconfiglib').check(dir=1)

def test_update_with_groups_explicit(west_init_tmpdir):
    # Even inactive projects must be updated if the user specifically
    # requests it.

    remotes = west_init_tmpdir / '..' / 'repos'

    with open(west_init_tmpdir / 'zephyr' / 'west.yml', 'w') as f:
        f.write(f'''
        manifest:
          defaults:
            remote: test-local
          remotes:
            - name: test-local
              url-base: {remotes}
          projects:
            - name: Kconfiglib
              revision: zephyr
              path: subdir/Kconfiglib
              groups:
              - disabled
          group-filter: [-disabled]
          self:
            path: zephyr
        ''')

    cmd('update')
    assert (west_init_tmpdir / 'subdir' / 'Kconfiglib').check(dir=0)

    cmd('update Kconfiglib')
    assert (west_init_tmpdir / 'subdir' / 'Kconfiglib').check(dir=1)


def test_init_with_manifest_filename(repos_tmpdir):
    # Test 'west init --mf' on a normal repo

    west_tmpdir = repos_tmpdir / 'workspace'
    manifest = repos_tmpdir / 'repos' / 'zephyr'

    with open(manifest / 'west.yml', 'r') as f:
        manifest_data = f.read()

    # also creates a west.yml with a syntax error to verify west doesn't even
    # try to load the file
    add_commit(str(manifest), 'rename manifest',
               files={'west.yml': '[', 'project.yml': manifest_data})

    # syntax error
    with pytest.raises(subprocess.CalledProcessError):
        cmd(['init', '-m', manifest, west_tmpdir])
    shutil.move(west_tmpdir, repos_tmpdir / 'workspace-syntaxerror')

    # success
    cmd(['init', '-m', manifest, '--mf', 'project.yml', west_tmpdir])
    west_tmpdir.chdir()
    config.read_config()
    cmd('update')

def test_init_with_manifest_in_subdir(repos_tmpdir):
    # Test west init with a manifest repository that is intended to
    # live in a nested subdirectory of the workspace topdir.

    manifest = repos_tmpdir / 'repos' / 'zephyr'
    add_commit(manifest, 'move manifest repo to subdirectory',
               files={'west.yml':
                      textwrap.dedent('''
                      manifest:
                        projects: []
                        self:
                          path: nested/subdirectory
                      ''')})

    workspace = repos_tmpdir / 'workspace'
    cmd(['init', '-m', manifest, workspace])
    assert (workspace / 'nested' / 'subdirectory').check(dir=1)

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
    # This removes tagged_repo, but we're not using it, so that's fine.
    add_commit(remote_zephyr, 'test added extension command',
               files={'west.yml': textwrap.dedent(f'''\
                      west:
                        url: file://{remote_west}
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
                      ''')})

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
    west_tmpdir = repos_tmpdir / 'workspace'
    zephyr = repos_tmpdir / 'repos' / 'zephyr'
    cmd(['init', '-m', zephyr, west_tmpdir])
    west_tmpdir.chdir()
    config.read_config()
    cmd('update')

    # The newline shenanigans are for Windows.
    help_text = '\n'.join(cmd('-h').splitlines())
    expected = '\n'.join([
        'extension commands from project Kconfiglib (path: subdir/Kconfiglib):',  # noqa: E501
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

    # This removes tagged_repo, but we're not using it, so that's fine.
    add_commit(remote_zephyr, 'test added extension command',
               files={'west.yml': textwrap.dedent(f'''\
                      west:
                        url: file://{remote_west}
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
                      ''')})

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
    west_tmpdir = repos_tmpdir / 'workspace'
    zephyr = repos_tmpdir / 'repos' / 'zephyr'
    cmd(['init', '-m', zephyr, west_tmpdir])
    west_tmpdir.chdir()
    config.read_config()
    cmd('update')

    actual = cmd('test-extension', stderr=subprocess.STDOUT).splitlines()
    expected = [
        'WARNING: ignoring project net-tools extension command "test-extension"; command "test-extension" is already defined as extension command',  # noqa: E501
        'Testing kconfig test command',
    ]

    assert actual == expected

def test_topdir_none(tmpdir):
    # Running west topdir outside of any workspace ought to fail.

    tmpdir.chdir()
    with pytest.raises(subprocess.CalledProcessError):
        cmd('topdir')

def test_topdir_in_workspace(west_init_tmpdir):
    # Running west topdir anywhere inside of a workspace ought to
    # work, and return the same thing.

    expected = PurePath(str(west_init_tmpdir)).as_posix()

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

def default_updater(remotes):
    add_commit(remotes.net_tools, 'another net-tools commit')
    add_commit(remotes.kconfiglib, 'another kconfiglib commit')
    add_commit(remotes.tagged_repo, 'another tagged_repo commit')
    cmd('update')

def update_helper(west_tmpdir, updater=default_updater):
    # Helper command for causing a change in two remote repositories,
    # then running a project command on the west workspace.
    #
    # Adds a commit to both of the kconfiglib and net-tools projects
    # remotes, then call updater(update_remotes),
    # which defaults to a function that adds commits in each
    # repository's remote and runs 'west update'.
    #
    # Captures the remote and local repository paths, as well as
    # manifest-rev and HEAD before and after, returning the results in
    # an UpdateResults tuple.

    nt_remote = str(west_tmpdir.join('..', 'repos', 'net-tools'))
    nt_local = str(west_tmpdir.join('net-tools'))
    kl_remote = str(west_tmpdir.join('..', 'repos', 'Kconfiglib'))
    kl_local = str(west_tmpdir.join('subdir', 'Kconfiglib'))
    tr_remote = str(west_tmpdir.join('..', 'repos', 'tagged_repo'))
    tr_local = str(west_tmpdir.join('tagged_repo'))

    def output_or_none(*args, **kwargs):
        try:
            ret = check_output(*args, **kwargs)
        except (FileNotFoundError, NotADirectoryError,
                subprocess.CalledProcessError):
            ret = None
        return ret

    nt_mr_0 = output_or_none([GIT, 'rev-parse', 'manifest-rev'], cwd=nt_local)
    kl_mr_0 = output_or_none([GIT, 'rev-parse', 'manifest-rev'], cwd=kl_local)
    tr_mr_0 = output_or_none([GIT, 'rev-parse', 'manifest-rev'], cwd=tr_local)
    nt_head_0 = output_or_none([GIT, 'rev-parse', 'HEAD'], cwd=nt_local)
    kl_head_0 = output_or_none([GIT, 'rev-parse', 'HEAD'], cwd=kl_local)
    tr_head_0 = output_or_none([GIT, 'rev-parse', 'HEAD'], cwd=tr_local)

    updater(UpdateRemotes(nt_remote, kl_remote, tr_remote))

    nt_mr_1 = output_or_none([GIT, 'rev-parse', 'manifest-rev'], cwd=nt_local)
    kl_mr_1 = output_or_none([GIT, 'rev-parse', 'manifest-rev'], cwd=kl_local)
    tr_mr_1 = output_or_none([GIT, 'rev-parse', 'manifest-rev'], cwd=tr_local)
    nt_head_1 = output_or_none([GIT, 'rev-parse', 'HEAD'], cwd=nt_local)
    kl_head_1 = output_or_none([GIT, 'rev-parse', 'HEAD'], cwd=kl_local)
    tr_head_1 = output_or_none([GIT, 'rev-parse', 'HEAD'], cwd=tr_local)

    return UpdateResults(nt_remote, nt_local,
                         kl_remote, kl_local,
                         tr_remote, tr_local,
                         nt_mr_0, nt_mr_1,
                         kl_mr_0, kl_mr_1,
                         tr_mr_0, tr_mr_1,
                         nt_head_0, nt_head_1,
                         kl_head_0, kl_head_1,
                         tr_head_0, tr_head_1)

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

    west_yml_content = textwrap.dedent(f'''\
                      west:
                        url: file://{rwest}
                      manifest:
                        defaults:
                          remote: test-local

                        remotes:
                          - name: test-local
                            url-base: file://{rrepo}

                        projects:
                          - name: net-tools
                            revision: {revision}
                        self:
                          path: zephyr
                      ''')
    add_commit(str(wct.join('zephyr')), 'test update manifest',
               files={'west.yml': west_yml_content})

    cmd('update')

    revision = rev_parse(alt_net_tools, 'HEAD')

    west_yml_content = textwrap.dedent(f'''\
                      west:
                        url: file://{rwest}
                      manifest:
                        defaults:
                          remote: test-local

                        remotes:
                          - name: test-local
                            url-base: file://{rrepo}
                          - name: test-alternate
                            url-base: file://{alt_repo}

                        projects:
                          - name: net-tools
                            remote: test-alternate
                            revision: {revision}
                        self:
                          path: zephyr
                      ''')

    add_commit(str(wct.join('zephyr')), 'test update manifest conflict',
               files={'west.yml': west_yml_content})

    cmd('update')

def test_import_project_release(repos_tmpdir):
    # Tests for a workspace that's based off of importing from a
    # project at a fixed release, with no downstream project forks.

    remotes = repos_tmpdir / 'repos'
    zephyr = remotes / 'zephyr'
    add_tag(zephyr, 'test-tag')

    # For this test, we create a remote manifest repository. This
    # makes sure we can clone a manifest repository which contains
    # imports without issue (and we don't need to clone the imported
    # projects.)
    #
    # On subsequent tests, we won't bother with this step. We will
    # just put the manifest repository directly into the workspace and
    # use west init -l. This also provides coverage for the -l option
    # in the presence of imports.
    manifest_remote = remotes / 'mp'
    create_repo(manifest_remote)
    add_commit(manifest_remote, 'manifest repo commit',
               files={'west.yml':
                      f'''
                      manifest:
                        projects:
                        - name: zephyr
                          url: {zephyr}
                          revision: test-tag
                          import: true
                      '''})

    # Create the workspace and verify we can't load the manifest yet
    # (because some imported data is missing).
    ws = repos_tmpdir / 'ws'
    cmd(['init', '-m', manifest_remote, ws])
    with pytest.raises(ManifestImportFailed):
        # We can't load this yet, because we haven't cloned zephyr.
        Manifest.from_topdir(topdir=ws)

    # Run west update and make sure we can load the manifest now.
    cmd('update', cwd=ws)

    actual = Manifest.from_topdir(topdir=ws).projects
    expected = [ManifestProject(path='mp', topdir=ws),
                Project('zephyr', zephyr,
                        revision='test-tag', topdir=ws),
                Project('Kconfiglib', remotes / 'Kconfiglib',
                        revision='zephyr', path='subdir/Kconfiglib',
                        topdir=ws),
                Project('tagged_repo', remotes / 'tagged_repo',
                        revision='v1.0', topdir=ws),
                Project('net-tools', remotes / 'net-tools',
                        clone_depth=1, topdir=ws,
                        west_commands='scripts/west-commands.yml')]
    for a, e in zip(actual, expected):
        check_proj_consistency(a, e)

    # Add a commit in the remote zephyr repository and make sure it
    # doesn't affect our local workspace, since we've locked it to a
    # tag.
    zephyr_ws = ws / 'zephyr'
    head_before = rev_parse(zephyr_ws, 'HEAD')
    add_commit(zephyr, 'this better not show up',
               files={'should-not-clone': ''})

    cmd('update', cwd=ws)

    assert head_before == rev_parse(zephyr_ws, 'HEAD')
    actual = Manifest.from_topdir(topdir=ws).projects
    for a, e in zip(actual, expected):
        check_proj_consistency(a, e)
    assert (zephyr_ws / 'should-not-clone').check(file=0)

def test_import_project_release_fork(repos_tmpdir):
    # Like test_import_project_release(), but with a project fork,
    # and using west init -l.

    remotes = repos_tmpdir / 'repos'

    zephyr = remotes / 'zephyr'
    add_tag(zephyr, 'zephyr-tag')

    fork = remotes / 'my-kconfiglib-fork'
    create_repo(fork)
    add_commit(fork, 'fork kconfiglib')
    add_tag(fork, 'fork-tag')

    ws = repos_tmpdir / 'ws'
    create_workspace(ws)
    manifest_repo = ws / 'mp'
    create_repo(manifest_repo)
    add_commit(manifest_repo, 'manifest repo commit',
               files={'west.yml':
                      f'''
                      manifest:
                        projects:
                        - name: zephyr
                          url: {zephyr}
                          revision: zephyr-tag
                          import: true
                        - name: Kconfiglib
                          url: {fork}
                          revision: fork-tag
                      '''})

    cmd(['init', '-l', manifest_repo])
    with pytest.raises(ManifestImportFailed):
        Manifest.from_topdir(topdir=ws)

    cmd('update', cwd=ws)

    actual = Manifest.from_topdir(topdir=ws).projects
    expected = [ManifestProject(path='mp', topdir=ws),
                Project('zephyr', zephyr,
                        revision='zephyr-tag', topdir=ws),
                Project('Kconfiglib', fork,
                        revision='fork-tag', path='Kconfiglib',
                        topdir=ws),
                Project('tagged_repo', remotes / 'tagged_repo',
                        revision='v1.0', topdir=ws),
                Project('net-tools', remotes / 'net-tools',
                        clone_depth=1, topdir=ws,
                        west_commands='scripts/west-commands.yml')]
    for a, e in zip(actual, expected):
        check_proj_consistency(a, e)

    zephyr_ws = ws / 'zephyr'
    head_before = rev_parse(zephyr_ws, 'HEAD')
    add_commit(zephyr, 'this better not show up',
               files={'should-not-clone': ''})

    cmd('update', cwd=ws)

    assert head_before == rev_parse(zephyr_ws, 'HEAD')
    actual = Manifest.from_topdir(topdir=ws).projects
    for a, e in zip(actual, expected):
        check_proj_consistency(a, e)
    assert (zephyr_ws / 'should-not-clone').check(file=0)

def test_import_project_release_dir(tmpdir):
    # Tests for a workspace that imports a directory from a project
    # at a fixed release.

    remotes = tmpdir / 'remotes'
    empty_project = remotes / 'empty_project'
    create_repo(empty_project)
    add_commit(empty_project, 'empty-project empty commit')
    imported = remotes / 'imported'
    create_repo(imported)
    add_commit(imported, 'add directory of imports',
               files={'test.d/1.yml':
                      f'''\
                      manifest:
                        projects:
                        - name: west.d_1.yml-p1
                          url: {empty_project}
                        - name: west.d_1.yml-p2
                          url: {empty_project}
                      ''',
                      'test.d/2.yml':
                      f'''\
                      manifest:
                        projects:
                        - name: west.d_2.yml-p1
                          url: {empty_project}
                      '''})
    add_tag(imported, 'import-tag')

    ws = tmpdir / 'ws'
    create_workspace(ws)
    manifest_repo = ws / 'mp'
    add_commit(manifest_repo, 'manifest repo commit',
               files={'west.yml':
                      f'''
                      manifest:
                        projects:
                        - name: imported
                          url: {imported}
                          revision: import-tag
                          import: test.d
                      '''})

    cmd(['init', '-l', manifest_repo])
    with pytest.raises(ManifestImportFailed):
        Manifest.from_topdir(topdir=ws)

    cmd('update', cwd=ws)
    actual = Manifest.from_topdir(topdir=ws).projects
    expected = [ManifestProject(path='mp', topdir=ws),
                Project('imported', imported,
                        revision='import-tag', topdir=ws),
                Project('west.d_1.yml-p1', empty_project, topdir=ws),
                Project('west.d_1.yml-p2', empty_project, topdir=ws),
                Project('west.d_2.yml-p1', empty_project, topdir=ws)]
    for a, e in zip(actual, expected):
        check_proj_consistency(a, e)

def test_import_project_rolling(repos_tmpdir):
    # Like test_import_project_release, but with a rolling downstream
    # that pulls master. We also use west init -l.

    remotes = repos_tmpdir / 'repos'
    zephyr = remotes / 'zephyr'

    ws = repos_tmpdir / 'ws'
    create_workspace(ws)
    manifest_repo = ws / 'mp'
    create_repo(manifest_repo)
    add_commit(manifest_repo, 'manifest repo commit',
               # zephyr revision is implicitly master:
               files={'west.yml':
                      f'''
                      manifest:
                        projects:
                        - name: zephyr
                          url: {zephyr}
                          import: true
                      '''})

    cmd(['init', '-l', manifest_repo])
    with pytest.raises(ManifestImportFailed):
        Manifest.from_topdir(topdir=ws)

    cmd('update', cwd=ws)

    actual = Manifest.from_topdir(topdir=ws).projects
    expected = [ManifestProject(path='mp', topdir=ws),
                Project('zephyr', zephyr,
                        revision='master', topdir=ws),
                Project('Kconfiglib', remotes / 'Kconfiglib',
                        revision='zephyr', path='subdir/Kconfiglib',
                        topdir=ws),
                Project('tagged_repo', remotes / 'tagged_repo',
                        revision='v1.0', topdir=ws),
                Project('net-tools', remotes / 'net-tools',
                        clone_depth=1, topdir=ws,
                        west_commands='scripts/west-commands.yml')]
    for a, e in zip(actual, expected):
        check_proj_consistency(a, e)

    # Add a commit in the remote zephyr repository and make sure it
    # *does* affect our local workspace, since we're rolling with its
    # master branch.
    zephyr_ws = ws / 'zephyr'
    head_before = rev_parse(zephyr_ws, 'HEAD')
    add_commit(zephyr, 'this better show up',
               files={'should-clone': ''})

    cmd('update', cwd=ws)

    assert head_before != rev_parse(zephyr_ws, 'HEAD')
    assert (zephyr_ws / 'should-clone').check(file=1)
