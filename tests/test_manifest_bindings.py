# Copyright 2026 The Zephyr Project Contributors.
#
# SPDX-License-Identifier: Apache-2.0

'''Pytest covering the PyO3 `Manifest` binding (data-layer cut).'''

import pytest
from west._west_native import (
    MalformedManifest,
    Manifest,
    ManifestImportFailed,
    ManifestRepo,
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


# --- PyImportSource::project_root regression -----------------------------
#
# Nested `self.import:` directives inside a project-imported manifest body
# must anchor against the imported project's working tree, not the outer
# manifest repo root. Hits the example-application + zephyr workspace
# layout where zephyr's body declares `self.import: submanifests`.


def test_per_project_import_anchors_self_import_in_project_root(tmp_path):
    # Workspace layout:
    #   <tmp>/outer/west.yml                   — the root manifest
    #   <tmp>/inner/west.yml                   — body returned by callback
    #   <tmp>/inner/submanifests/a.yml         — real file on disk
    outer = tmp_path / "outer"
    inner = tmp_path / "inner"
    submanifests = inner / "submanifests"
    submanifests.mkdir(parents=True)
    outer.mkdir()

    (outer / "west.yml").write_text(
        """\
manifest:
  projects:
    - name: inner
      url: https://example.com/inner
      import: true
"""
    )
    inner_body = """\
manifest:
  self:
    import: submanifests
  projects: []
"""
    (submanifests / "a.yml").write_text(
        """\
manifest:
  projects:
    - name: nested
      url: https://example.com/nested
"""
    )

    def importer(name, project_path, relative_file):
        # The resolver asks for inner's body. We don't care which file
        # name it requests; there's only one importable project here.
        assert name == "inner"
        return inner_body

    m = Manifest.from_path_with_imports(
        str(tmp_path),  # topdir — the bug fix wires this through
        str(outer / "west.yml"),
        str(outer),
        importer,
    )
    names = [p.name for p in m.projects]
    # `inner` from the outer manifest; `nested` from the directory walk
    # against <tmp>/inner/submanifests/. If project_root had returned None
    # (the pre-fix bug), the walk would have looked at <tmp>/outer/submanifests/
    # and the call would have raised MalformedManifest.
    assert "nested" in names
    assert "inner" in names


def test_in_memory_with_imports_keeps_no_anchor():
    # `from_yaml_str_with_imports` has no workspace anchor, so
    # PyImportSource.topdir stays None. The in-memory FORCE_PROJECTS
    # policy maps to PROJECTS_ONLY in the resolver, which deliberately
    # *skips* self/top-level imports (no filesystem to anchor against).
    # This pins "topdir=None doesn't perturb the in-memory path": the
    # nested self.import gets silently dropped (correct) rather than
    # erroring against a confused outer root.
    root_body = """\
manifest:
  projects:
    - name: inner
      url: https://example.com/inner
      import: true
"""
    inner_body = """\
manifest:
  self:
    import: submanifests
  projects:
    - name: from-inner
      url: https://example.com/from-inner
"""

    def importer(name, project_path, relative_file):
        return inner_body

    # FORCE_PROJECTS = 2 (see src/west/manifest.py ImportFlag).
    m = Manifest.from_yaml_str_with_imports(root_body, importer, import_flags=2)
    names = [p.name for p in m.projects]
    # `inner` from the outer; `from-inner` from the body; `nested` (which
    # would come from the dropped self.import) is absent.
    assert "inner" in names
    assert "from-inner" in names


# --- has_imports observation flag ----------------------------------------
#
# v1 set `Manifest.has_imports = True` whenever the source YAML carried
# any `import:` directive — self, top-level, or per-project. The flag
# records observation, not resolution, so it survives parses that ignore
# the directive (e.g. `ImportFlag.IGNORE`, which maps to the resolver's
# `IGNORE_ALL` policy).

# ImportFlag.IGNORE = 1 (see src/west/manifest.py).
_IGNORE = 1


def test_has_imports_false_when_absent():
    m = Manifest.from_yaml_str(SIMPLE_YAML)
    assert m.has_imports is False


def test_has_imports_true_for_self_import_under_ignore():
    yaml = """\
manifest:
  self:
    import: sub.yml
  projects: []
"""
    m = Manifest.from_yaml_str(yaml, import_flags=_IGNORE)
    assert m.has_imports is True


def test_has_imports_true_for_per_project_import_under_ignore():
    yaml = """\
manifest:
  projects:
    - name: p
      url: https://example.com/p
      import: true
"""
    m = Manifest.from_yaml_str(yaml, import_flags=_IGNORE)
    assert m.has_imports is True


def test_has_imports_false_for_per_project_import_bool_false():
    yaml = """\
manifest:
  projects:
    - name: p
      url: https://example.com/p
      import: false
"""
    m = Manifest.from_yaml_str(yaml)
    assert m.has_imports is False
