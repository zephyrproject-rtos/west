# Copyright (c) 2026, Draeger Safety AG & Co. KGaA
#
# SPDX-License-Identifier: Apache-2.0

import os
from pathlib import Path

import pytest

from conftest import tmp_west_topdir, update_env
from west.util import WestEnvFileError, WestNotFound, west_topdir


def test_absolute_zephyr_base(tmp_path):
    # A .westenv file with an absolute ZEPHYR_BASE path resolves the workspace.
    workspace = tmp_path / 'workspace'
    project = tmp_path / 'project'
    project.mkdir()

    with tmp_west_topdir(workspace):
        (project / '.westenv').write_text(f'ZEPHYR_BASE={workspace}\n')
        result = west_topdir(str(project))

    assert Path(result).resolve() == workspace.resolve()


def test_relative_zephyr_base(tmp_path):
    # A .westenv file with a relative ZEPHYR_BASE is resolved against the file's directory.
    workspace = tmp_path / 'workspace'
    project = tmp_path / 'project'
    project.mkdir()

    with tmp_west_topdir(workspace):
        rel = os.path.relpath(workspace, project)
        (project / '.westenv').write_text(f'ZEPHYR_BASE={rel}\n')
        result = west_topdir(str(project))

    assert Path(result).resolve() == workspace.resolve()


def test_no_zephyr_base_key_falls_through_to_parent(tmp_path):
    # A .westenv with no ZEPHYR_BASE key is ignored; the search continues up the tree.
    subdir = tmp_path / 'subdir'
    subdir.mkdir()
    (subdir / '.westenv').write_text('# no ZEPHYR_BASE\n')

    with tmp_west_topdir(tmp_path):
        result = west_topdir(str(subdir))

    assert Path(result).resolve() == tmp_path.resolve()


def test_invalid_key_raises_westenv_error(tmp_path):
    # A .westenv file containing an unrecognized key raises WestEnvFileError.
    project = tmp_path / 'project'
    project.mkdir()
    (project / '.westenv').write_text('UNKNOWN_KEY=value\n')

    with pytest.raises(WestEnvFileError):
        west_topdir(str(project))


def test_zephyr_base_env_fallback(tmp_path):
    # When no .west directory is found anywhere, and no .westenv file exists, the ZEPHYR_BASE env var is tried
    # as a fallback.
    workspace = tmp_path / 'workspace'
    project = tmp_path / 'project'
    project.mkdir()

    with tmp_west_topdir(workspace), update_env({'ZEPHYR_BASE': str(workspace)}):
        result = west_topdir(str(project))

    assert Path(result).resolve() == workspace.resolve()


def test_no_workspace_raises_west_not_found(tmp_path):
    # With no .west, no .westenv, and no ZEPHYR_BASE env, WestNotFound is raised.
    project = tmp_path / 'project'
    project.mkdir()

    with update_env({'ZEPHYR_BASE': None}):
        with pytest.raises(WestNotFound):
            west_topdir(str(project))
