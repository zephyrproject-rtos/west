# Copyright 2026 The Zephyr Project Contributors.
#
# SPDX-License-Identifier: Apache-2.0

'''Pytest covering the `_west_native` PyO3 binding via the
`src/west/util.py` re-export.

Lives under `crates/west-cli/tests/python/` rather than the repo's
top-level `tests/` directory because the latter's `conftest.py`
imports `west.app` (carry-over from removing the python CLI driver
not yet untangled). Keeping the binding tests self-contained here
means they don't need that conftest fixed first.

Run with:
    PYTHONPATH=src pytest crates/west-cli/tests/python/

The binding is a hard dependency of `west.util` — if it isn't
installed (run `maturin develop -m crates/west-cli/Cargo.toml
--features pyo3`), this file errors at import, not at the
individual test level.
'''

import contextlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Make `import west` resolve to src/west/ for in-tree runs. pyproject.toml
# already sets `pythonpath = "src"` for pytest invoked at the repo root,
# but we don't rely on that here so the test runs from anywhere.
_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from west import _west_native  # noqa: E402
from west.util import WestNotFound, west_topdir  # noqa: E402


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
