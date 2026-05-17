# Copyright 2026 The Zephyr Project Contributors.
#
# SPDX-License-Identifier: Apache-2.0

'''Pytest covering the PyO3 `Configuration` binding via the
`src/west/configuration.py` re-export.

Same layout pattern as `test_util_bindings.py`: self-contained
fixtures, no dependency on the repo's broken top-level
`tests/conftest.py`. Tests override `WEST_CONFIG_SYSTEM` and
`WEST_CONFIG_GLOBAL` to tempfiles so user/system config on the
developer's machine is never touched.

Run with:
    PYTHONPATH=src pytest crates/west-py/tests/

The `_west_native` binding is a hard dependency of `west.configuration`;
if it isn't installed (run `maturin develop -m crates/west-py/Cargo.toml`
or `cargo build -p west-py` + copy the dylib), this file errors at
import time, not at the individual test level.
'''

import contextlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Make `import west` resolve to src/west/ for in-tree runs.
_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import _west_native  # noqa: E402
from west.configuration import Configuration, ConfigFile, MalformedConfig  # noqa: E402


@contextlib.contextmanager
def _workspace_with_isolated_config():
    '''Yield a topdir whose system/global/local config files all live
    under a tempdir, so the developer's real `~/.config/west/...` is
    never touched.'''
    with tempfile.TemporaryDirectory() as ws:
        os.makedirs(os.path.join(ws, ".west"))
        prev = {
            "WEST_CONFIG_SYSTEM": os.environ.get("WEST_CONFIG_SYSTEM"),
            "WEST_CONFIG_GLOBAL": os.environ.get("WEST_CONFIG_GLOBAL"),
            "WEST_CONFIG_LOCAL": os.environ.get("WEST_CONFIG_LOCAL"),
        }
        os.environ["WEST_CONFIG_SYSTEM"] = os.path.join(ws, "sys.toml")
        os.environ["WEST_CONFIG_GLOBAL"] = os.path.join(ws, "glob.toml")
        os.environ.pop("WEST_CONFIG_LOCAL", None)
        try:
            yield ws
        finally:
            for k, v in prev.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def test_util_re_exports_are_the_binding():
    '''Pins down the no-fallback contract: `west.configuration` IS the
    binding's symbols, not a wrapper.'''
    assert Configuration is _west_native.Configuration
    assert ConfigFile is _west_native.ConfigFile
    assert MalformedConfig is _west_native.MalformedConfig


def test_config_file_int_values_match_legacy():
    '''Legacy `ConfigFile` was an `Enum(value)` with stable ints. The
    binding mirrors them so any caller comparing against the bare
    integer still works.'''
    assert int(ConfigFile.ALL) == 1
    assert int(ConfigFile.SYSTEM) == 2
    assert int(ConfigFile.GLOBAL) == 3
    assert int(ConfigFile.LOCAL) == 4


def test_get_returns_default_when_missing():
    with _workspace_with_isolated_config() as ws:
        c = Configuration(ws)
        assert c.get("nonexistent.key") is None
        assert c.get("nonexistent.key", "fallback") == "fallback"


def test_set_then_get_roundtrips_in_local_layer():
    with _workspace_with_isolated_config() as ws:
        c = Configuration(ws)
        c.set("manifest.path", "zephyr")  # default configfile = LOCAL
        assert c.get("manifest.path") == "zephyr"
        # And re-loading from disk surfaces the same value.
        c2 = Configuration(ws)
        assert c2.get("manifest.path") == "zephyr"


def test_set_then_get_in_global_layer():
    with _workspace_with_isolated_config() as ws:
        c = Configuration(ws)
        c.set("user.name", "alice", ConfigFile.GLOBAL)
        # Default search (ALL) finds it.
        assert c.get("user.name") == "alice"
        # Scoped lookups: present in GLOBAL, absent in LOCAL.
        assert c.get("user.name", configfile=ConfigFile.GLOBAL) == "alice"
        assert c.get("user.name", configfile=ConfigFile.LOCAL) is None


def test_local_layer_overrides_global_under_all_scope():
    with _workspace_with_isolated_config() as ws:
        c = Configuration(ws)
        c.set("a.b", "global-value", ConfigFile.GLOBAL)
        c.set("a.b", "local-value", ConfigFile.LOCAL)
        # ALL walks highest first → local wins.
        assert c.get("a.b") == "local-value"
        assert c.get("a.b", configfile=ConfigFile.GLOBAL) == "global-value"


def test_set_with_configfile_all_is_rejected():
    with _workspace_with_isolated_config() as ws:
        c = Configuration(ws)
        with pytest.raises(ValueError):
            c.set("a.b", "x", ConfigFile.ALL)


def test_getboolean_truthy_and_default():
    with _workspace_with_isolated_config() as ws:
        c = Configuration(ws)
        c.set("update.narrow", True)
        assert c.getboolean("update.narrow") is True
        assert c.getboolean("update.missing") is False  # default
        assert c.getboolean("update.missing", True) is True


def test_getint_and_getfloat_typed():
    with _workspace_with_isolated_config() as ws:
        c = Configuration(ws)
        c.set("a.i", 42)
        c.set("a.f", 1.5)
        assert c.getint("a.i") == 42
        assert c.getfloat("a.f") == 1.5
        assert c.getint("a.missing") is None
        assert c.getfloat("a.missing") is None


def test_set_accepts_list_value():
    with _workspace_with_isolated_config() as ws:
        c = Configuration(ws)
        c.set("manifest.project-filter", ["+foo", "-bar"])
        items = dict(c.items())
        assert items.get("manifest.project-filter") == ["+foo", "-bar"]


def test_delete_topmost_when_configfile_none():
    with _workspace_with_isolated_config() as ws:
        c = Configuration(ws)
        c.set("a.b", "global", ConfigFile.GLOBAL)
        c.set("a.b", "local", ConfigFile.LOCAL)
        c.delete("a.b")  # configfile=None → topmost
        assert c.get("a.b") == "global"  # local was the topmost
        c.delete("a.b")
        assert c.get("a.b") is None


def test_delete_missing_raises_keyerror():
    with _workspace_with_isolated_config() as ws:
        c = Configuration(ws)
        with pytest.raises(KeyError):
            c.delete("never.set")


def test_items_merges_layers_with_local_winning():
    with _workspace_with_isolated_config() as ws:
        c = Configuration(ws)
        c.set("g.only", "g-only", ConfigFile.GLOBAL)
        c.set("a.b", "g", ConfigFile.GLOBAL)
        c.set("a.b", "l", ConfigFile.LOCAL)
        c.set("l.only", "l-only", ConfigFile.LOCAL)
        merged = dict(c.items())
        assert merged["g.only"] == "g-only"
        assert merged["a.b"] == "l"  # local wins
        assert merged["l.only"] == "l-only"


def test_get_existing_paths_only_lists_written_files():
    with _workspace_with_isolated_config() as ws:
        c = Configuration(ws)
        assert c.get_existing_paths() == []
        c.set("a.b", "x", ConfigFile.LOCAL)
        existing = c.get_existing_paths()
        assert len(existing) == 1
        assert existing[0].name == "config.toml"


def test_malformed_toml_raises_malformed_config():
    with _workspace_with_isolated_config() as ws:
        # Plant a broken local config before constructing.
        local_dir = os.path.join(ws, ".west")
        with open(os.path.join(local_dir, "config.toml"), "w") as f:
            f.write("[unclosed\nno = good\n")
        with pytest.raises(MalformedConfig):
            Configuration(ws)
