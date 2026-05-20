# Copyright 2026 The Zephyr Project Contributors.
#
# SPDX-License-Identifier: Apache-2.0

'''Pytest covering the `_west_native` PyO3 binding via the
`src/west/util.py` re-export.
'''

import contextlib
import os
import tempfile

import pytest

from west import _west_native
from west.util import WestNotFound, west_topdir


@contextlib.contextmanager
def _tmp_workspace():
    '''Yield a path that has a `.west/` directory in it.'''
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, ".west"))
        yield d


def test_returns_workspace_for_topdir_start():
    with _tmp_workspace() as ws:
        assert west_topdir(ws) == ws


def test_walks_up_from_nested_start():
    with _tmp_workspace() as ws:
        nested = os.path.join(ws, "a", "b", "c")
        os.makedirs(nested)
        assert west_topdir(nested) == ws


def test_raises_outside_workspace():
    with tempfile.TemporaryDirectory() as nowhere:
        with pytest.raises(WestNotFound):
            west_topdir(nowhere)


def test_west_not_found_is_runtime_error_subclass():
    # Back-compat: `except RuntimeError` callers must still work.
    assert issubclass(WestNotFound, RuntimeError)


def test_util_re_exports_are_the_binding():
    '''`west.util` re-exports IS the binding's symbols, not a wrapper
    or copy. Pins down the no-fallback contract.'''
    assert WestNotFound is _west_native.WestNotFound
    assert west_topdir is _west_native.west_topdir
