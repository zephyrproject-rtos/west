# Copyright 2026 The Zephyr Project Contributors.
#
# SPDX-License-Identifier: Apache-2.0

'''Pytest covering the PyO3 `Configuration` binding via the
`src/west/configuration.py` re-export.

Tests override `WEST_CONFIG_SYSTEM` and `WEST_CONFIG_GLOBAL` to
tempfiles so user/system config on the developer's machine is
never touched.
'''

import contextlib
import os
import tempfile

import pytest

from west import _west_native
from west.configuration import ConfigFile, Configuration, MalformedConfig


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


# --- append ---------------------------------------------------------------


def test_append_to_existing_list_grows_by_one():
    # Sets a list, then appends a scalar — list grows by exactly one
    # element. Mirrors the CLI's `west config set -a` behaviour.
    with _workspace_with_isolated_config() as ws:
        c = Configuration(ws)
        c.set("manifest.project-filter", ["+foo", "-bar"])
        c.append("manifest.project-filter", "+baz")
        assert c.get_list_str("manifest.project-filter") == ["+foo", "-bar", "+baz"]


def test_append_python_list_value_extends_not_nests():
    # A python list/tuple input EXTENDS the target list — each element
    # joins one by one (mirrors list.extend), NOT list.append's
    # nesting semantic. To nest, wrap in another list.
    with _workspace_with_isolated_config() as ws:
        c = Configuration(ws)
        c.set("manifest.project-filter", ["+foo"])
        c.append("manifest.project-filter", ["+a", "+b"])
        assert c.get_list_str("manifest.project-filter") == ["+foo", "+a", "+b"]


def test_append_to_absent_creates_new_list():
    # Key absent at the target scope → append seeds a fresh list.
    with _workspace_with_isolated_config() as ws:
        c = Configuration(ws)
        c.append("manifest.project-filter", "+only")
        assert c.get_list_str("manifest.project-filter") == ["+only"]


def test_append_to_scalar_raises_value_error():
    # append refuses scalar-valued keys; user is told the current
    # type and what to do instead. The on-disk value is unchanged.
    with _workspace_with_isolated_config() as ws:
        c = Configuration(ws)
        c.set("k.v", "plain", ConfigFile.LOCAL)
        with pytest.raises(ValueError, match="list-valued"):
            c.append("k.v", "more")
        assert c.get("k.v") == "plain"


def test_append_honours_scope_does_not_peek_at_other_layers():
    # The β-semantic: append reads from the same layer it writes to.
    # Global has the key; appending at --local seeds local with just
    # the new element, leaving global untouched. No silent layer
    # shadowing.
    with _workspace_with_isolated_config() as ws:
        c = Configuration(ws)
        c.set("manifest.project-filter", ["+global"], ConfigFile.GLOBAL)
        c.append("manifest.project-filter", "+local", ConfigFile.LOCAL)
        assert c.get_list_str(
            "manifest.project-filter", configfile=ConfigFile.LOCAL
        ) == ["+local"]
        assert c.get_list_str(
            "manifest.project-filter", configfile=ConfigFile.GLOBAL
        ) == ["+global"]


def test_append_with_configfile_all_is_rejected():
    # `ConfigFile.ALL` makes no sense for append — same shape `set`
    # rejects via ValueError.
    with _workspace_with_isolated_config() as ws:
        c = Configuration(ws)
        with pytest.raises(ValueError):
            c.append("k.v", "x", ConfigFile.ALL)


def test_malformed_toml_raises_malformed_config():
    with _workspace_with_isolated_config() as ws:
        # Plant a broken local config before constructing.
        local_dir = os.path.join(ws, ".west")
        with open(os.path.join(local_dir, "config.toml"), "w") as f:
            f.write("[unclosed\nno = good\n")
        with pytest.raises(MalformedConfig):
            Configuration(ws)
