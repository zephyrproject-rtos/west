# Copyright 2026 The Zephyr Project Contributors.
#
# SPDX-License-Identifier: Apache-2.0

'''Pytest covering the PyO3 `Manifest` binding (data-layer cut).

Layout matches the other binding tests in this directory: self-
contained, no dependency on the repo's top-level `tests/conftest.py`.

Run with:
    PYTHONPATH=src pytest crates/west-cli/tests/python/

This file covers the binding surface only. The python wrapper layer
that re-exports these as `west.manifest.{Manifest, Project, ...}`
(plus workspace concerns like `from_topdir` and git helpers) is a
separate commit.
'''

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from west._west_native import (  # noqa: E402
    GroupFilterEntry,
    Manifest,
    ManifestImportFailed,
    ManifestRepo,
    MalformedManifest,
    Project,
    Submodule,
    parse_cli_group_filter,
)


# A small but representative manifest used by most tests.
SIMPLE_YAML = """\
manifest:
  group-filter: [-noisy]
  projects:
    - name: alpha
      url: https://example.com/a
      revision: main
      groups: [optional]
    - name: beta
      url: https://example.com/b
      groups: [noisy]
      submodules: true
"""


def test_from_yaml_str_parses_simple_manifest():
    m = Manifest.from_yaml_str(SIMPLE_YAML)
    assert isinstance(m, Manifest)
    assert m.version is None
    assert len(m.projects) == 2


def test_project_fields_round_trip():
    m = Manifest.from_yaml_str(SIMPLE_YAML)
    a, b = m.projects
    assert a.name == "alpha"
    assert a.url == "https://example.com/a"
    assert a.revision == "main"
    assert a.groups == ["optional"]
    assert a.path == "alpha"  # defaults to name
    assert a.submodules is False  # no `submodules:` in YAML
    assert b.name == "beta"
    assert b.submodules is True  # `submodules: true` → All


def test_self_block_is_exposed():
    m = Manifest.from_yaml_str(SIMPLE_YAML)
    repo = m.self_
    assert isinstance(repo, ManifestRepo)
    # `self.path` defaults to "manifest" when not set explicitly.
    assert repo.path == "manifest"
    assert repo.west_commands == []


def test_group_filter_is_parsed():
    m = Manifest.from_yaml_str(SIMPLE_YAML)
    gf = m.group_filter
    assert len(gf) == 1
    assert gf[0].group == "noisy"
    assert gf[0].disabled is True


def test_project_lookup_by_name():
    m = Manifest.from_yaml_str(SIMPLE_YAML)
    assert m.project("alpha").name == "alpha"
    assert m.project("does-not-exist") is None


def test_resolve_projects_by_selectors():
    m = Manifest.from_yaml_str(SIMPLE_YAML)
    picked = m.resolve_projects(["beta"])
    assert [p.name for p in picked] == ["beta"]


def test_resolve_projects_unknown_raises_keyerror():
    m = Manifest.from_yaml_str(SIMPLE_YAML)
    with pytest.raises(KeyError):
        m.resolve_projects(["nope"])


def test_is_active_respects_manifest_group_filter():
    m = Manifest.from_yaml_str(SIMPLE_YAML)
    alpha = m.project("alpha")
    beta = m.project("beta")
    # group-filter: [-noisy] disables anything in `noisy`; `alpha` is
    # `optional` (no default opt-out), so active by default.
    assert m.is_active(alpha) is True
    assert m.is_active(beta) is False


def test_is_active_with_extra_cli_filter():
    m = Manifest.from_yaml_str(SIMPLE_YAML)
    beta = m.project("beta")
    cli = parse_cli_group_filter(["+noisy"])  # re-enable noisy
    assert m.is_active(beta, cli) is True


def test_parse_cli_group_filter_round_trip():
    parsed = parse_cli_group_filter(["+optional", "-noisy"])
    assert len(parsed) == 2
    assert parsed[0].group == "optional"
    assert parsed[0].disabled is False
    assert parsed[1].group == "noisy"
    assert parsed[1].disabled is True


def test_parse_cli_group_filter_invalid_raises():
    with pytest.raises(MalformedManifest):
        parse_cli_group_filter(["~bogus"])  # not +/-


def test_malformed_yaml_raises():
    with pytest.raises(MalformedManifest):
        Manifest.from_yaml_str("manifest:\n  projects:\n    - this is junk\n")


def test_submodules_specific_list_form(tmp_path):
    yaml = """\
manifest:
  projects:
    - name: a
      url: https://x
      submodules:
        - path: sub1
        - path: sub2
          name: named-sub
"""
    m = Manifest.from_yaml_str(yaml)
    subs = m.project("a").submodules
    assert isinstance(subs, list)
    assert len(subs) == 2
    assert isinstance(subs[0], Submodule)
    assert subs[0].path == "sub1"
    assert subs[0].name is None
    assert subs[1].path == "sub2"
    assert subs[1].name == "named-sub"


def test_from_path_dispatches_on_extension(tmp_path):
    p = tmp_path / "west.yml"
    p.write_text(SIMPLE_YAML)
    m = Manifest.from_path(p)
    assert len(m.projects) == 2


def test_from_path_unknown_extension_raises(tmp_path):
    p = tmp_path / "west.bogus"
    p.write_text(SIMPLE_YAML)
    with pytest.raises(MalformedManifest):
        Manifest.from_path(p)


def test_imports_at_top_level_raise_import_failed():
    # ImportSourceFailed / ImportNotSupported variants map to
    # ManifestImportFailed. A `manifest.import:` block without a
    # resolver should hit one of those paths via from_path.
    yaml = """\
manifest:
  self:
    import: somewhere.yml
  projects: []
"""
    # `from_yaml_str` doesn't carry an ImportSource, so imports there
    # are reported as ImportNotSupported (or MalformedManifest if the
    # YAML is fundamentally rejected).
    with pytest.raises((ManifestImportFailed, MalformedManifest)):
        Manifest.from_yaml_str(yaml)
