# TODO

Priority-ordered backlog for the Python-to-Rust port. Each entry names the
gap and, where useful, points at the file:line that wants attention. The
prose-style entries (manifest.version validation, JSON Schema publish)
capture full legacy/current/to-do context; the rest are one-liners with
a pointer to where the work lives.

## High — correctness gaps

### `manifest.version` is parsed but not validated

**Legacy behavior.** Versions outside the supported window were rejected at
parse time — too-high → `ManifestVersionError`, too-low (< 0.6.99) →
`MalformedManifest`. Covered in `tests-legacy/test_manifest.py:96` and
`:149`.

**Current behavior.** `ManifestSection.version` is stored without validation
(`crates/west-core/src/manifest.rs:343,908,1425`); `ManifestVersionError` in
`src/west/manifest.py:131` is a stub kept only so legacy `from west.manifest
import ManifestVersionError` doesn't break.

**To do.** Add a check in `Resolver::process_root` (or `into_manifest`) that
parses the version string and rejects anything outside the supported window.
Introduce a dedicated `ManifestError::UnsupportedVersion` variant, map it to
`ManifestVersionError` in `manifest_error_to_py` (`crates/west-cli/src/python/manifest.rs:843`),
and leave the low-version case mapping to `MalformedManifest`.

### `expect()` on user-reachable paths in `manifest.rs`

`manifest.rs:232` and `:275` both `expect("to_value emits manifest.projects
as array")` — couples west-cli to west-core's serialisation contract; a
future refactor of `to_value` panics here instead of erroring cleanly.
Convert to a `ManifestCmdError::InternalShape(&'static str)` variant + a
`projects_array_mut(value)` helper and propagate via `?`. `exec.rs:22`'s
`expect("required = true")` is one file away from clap and lower-risk —
improve the message or convert too, contributor's choice.

## Medium — UX / DX

### Restore `--format` key documentation on `west list` (and add `--json`)

Keys exist in `project_format.rs:45-80`; auto-generate the `long_about`
from the match arms so docs stay in sync. ~50 LOC.

### "Did you mean?" for unknown projects + unknown commands

`manifest.rs:331` (`UnknownProject` error) and `help.rs:23` (unknown
command). One `strsim` dep, ~15 LOC at each callsite.

### `west update --dry-run` / `west init --dry-run`

Print decisions without side effects. `update`'s auto-cache smart-skip
already classifies via `Vcs::rev_type`; expose it.

### `west config --list-search-paths` / `--list-paths`

v1 (upstream `48b3b0f`) added `west config --list-search-paths` (every
config file west searches, in lookup order) and `--list-paths` (only
the configs that actually exist, in load order). v2's `west config` CLI
exposes neither, even though the `Configuration` binding already backs
both — `get_search_paths()` and `get_existing_paths()` in
`crates/west-cli/src/python/config.rs`. Wire them up as flags on `west
config list` (or a small dedicated path-listing mode).

### Publish a JSON Schema for the manifest format

**Legacy state.** west v1 shipped a `pykwalify`-style YAML schema as
part of the python package (`manifest-schema.yml`). Editors, CI checks,
and downstream tools could validate `west.yml` files against it
without depending on west itself.

**Current state.** The Rust port encodes the manifest format only in
the `ManifestFile` / `ManifestSection` / `ImportSchema` / `ImportMap`
structs, plus the hand-rolled `Deserialize` impls. There is no
external artifact downstream consumers can read. Editors, the Zephyr
docs site, and third-party tooling that wants to validate manifests
have nothing to point at.

**To do.** Wire `schemars` (or hand-author, the schema is small enough)
to emit a JSON Schema from the manifest structs and publish it as
`west-manifest.schema.json` alongside the wheel / sdist. Pin the
schema with a regression test that diffs the generated output against
a committed snapshot — drift between the structs and the schema would
otherwise be invisible until a downstream user complained. Suggested
follow-ups once the schema exists:

- Reference the schema URL from a `# yaml-language-server: $schema=...`
  hint in west.yml examples, so VS Code (and any editor using the
  YAML language server) gives autocompletion and inline validation out
  of the box.
- Mention the schema location in `doc/`.
- The runtime parsing path stays serde + derive + the small custom
  Deserialize impls — JSON Schema isn't a replacement for those, just
  a published *description* of what they accept.

## Medium — internal hygiene / refactors

### `Project::from_core` deep-clones on every `Manifest::projects` access

`python/manifest.rs:302-322`. For workspaces with 100+ projects, each
`manifest.projects` call deep-clones every `Project` including
`userdata: serde_json::Value` recursion (the comment claims "shallow
clone" but it isn't, given `Value` depth). Switch to `Arc<core::Project>`-
backed storage in the python wrapper.

### `is_active` duplication between `Manifest` / `LoadedManifest` python bindings

`python/manifest.rs:573 + 805`. Both reconstruct `core::Project` from the
python `Project`'s unpacked fields and the comment says "only `groups`
matters." Either store `core::Project` directly in the python `Project`
(as the source of truth) or expose an `is_active_by_groups(&[String]) ->
bool` shortcut on `LoadedManifest`.

### `from_yaml_str_with_imports` non-zero default `import_flags`

`python/manifest.rs:511` defaults to `FLAG_FORCE_PROJECTS`. A caller
forgetting the flag implicitly opts into FORCE_PROJECTS. Footgun;
require an explicit value or make the default `0`.

### Add clippy + cargo fmt CI gates

No workspace `[lints]` table today; 18 clippy warnings (mostly nits) sit
unchecked. Add `[lints.clippy] = { deny = "warnings" }` + a `fmt --check`
job. Current starting count is so low that adding the gate now won't
bite.

### Audit `pub` vs `pub(crate)` across `commands/*`

`pub:pub(crate):pub(super)` ratio across the workspace is 198:30:17. The
198 leans hot; many `commands/*` items don't need to be globally public.
A sweep with `cargo public-api` would likely tighten ~20-30 items.

### `ColorMode::Auto` on the `Vcs` trait is dead surface

`vcs/mod.rs:565-575` documents `Auto` as a footgun — clients capture
into pipes, where `Auto` would force `Never` against intent. The CLI
layer already resolves `Auto → Always/Never` before calling the trait.
Either remove the variant or split trait-side `ColorMode` from CLI-side
`ColorArg`.

### `Vcs::ls_tree_at_ref` lacks file-vs-dir distinction

`vcs/mod.rs:220-225` returns `Vec<String>` with no `kind` discriminator;
callers can't tell directories from files without a follow-up call.
Return `Option<Vec<TreeEntry>>` where `TreeEntry { name, kind }`.

### Hoist `MAX_DEFAULT_JOBS = 8` + `default_jobs()`

Duplicated 5× across `diff` / `status` / `forall` / `grep` / `compare` /
`update`. Move to `commands/jobs.rs` or `workspace.rs`.

### Hoist `Settings::from_config` boilerplate

Copy-pasted across `diff` / `status` / `forall` / `grep` / `compare`
with identical `{cmd}.jobs` / `output.raw` / `output.quiet` triple.
Generic `fn settings_for(prefix: &str, ...) -> Settings`.

### Convert `WorkspaceError::{Config,Manifest,Vcs}(String)` to typed `#[source]` chains

`commands/workspace.rs`'s `WorkspaceError` erases the source typed error
into a `String`, breaking `e.source()` walking for `-vvv` callers.
`init.rs:635-646` is the gold-standard `#[from]`-driven pattern in this
tree; emulate it.

### Split `update/mod.rs::run()` (~260 lines)

Into setup + dispatch helpers. The function has grown unwieldy through
incremental additions.

### Stale `--manifest-path` doc comment in `init.rs`

The module header (`crates/west-cli/src/commands/init.rs:13-16`) says
`--manifest-path` is remote-mode only and that `-l` + `--manifest-path`
is rejected. That's wrong: the runtime (`resolve_local_layout`) supports
it in both modes with a "positional and `manifest.path` must agree" rule
— matching v1 and matching the arg's own docstring (lines 80-87). Fix
the header comment; no behavior change. (Surfaced during the
MIGRATION.md-vs-`zephyr/main` review — the doc entry that claimed this
was a v2-only restriction has been corrected.)

### `west manifest --resolve` drops west-commands `base_dir`

`Manifest::to_value()` emits west-commands as path-only strings
(`wc.path`), losing the `WestCommandsRef::base_dir` field that
`--resolve` / `--freeze` would need to round-trip correctly when the
source manifest had imported-subdir west-commands. Re-loading the
resolved file and running extensions against it would put the affected
entries back into the pre-fix state. Either (a) inline the base into
the path at emit time AND rewrite the referenced west-commands file
contents so its `file:` entries already include the base (intrusive),
or (b) extend the schema with an optional `base:` field per entry.
Defer until someone actually uses `west manifest --resolve` in this
configuration.

### Fix `{description}` / `{clone_depth}` fallback to empty string

`project_format.rs:52,85` returns literal `"None"` when the field is
absent — a python-ism (`x or "None"`) that breaks shell composition.
Empty string or `N/A` aligns with `{url}` / `{revision}` behavior.

### Continue `tests-legacy/` migration

High-volume files (`test_alias`, `test_commands`, `test_config`,
`test_extension_commands`, `test_manifest`, `test_project`,
`test_project_caching`) are all in `tests/`; what remains is the long
tail of small-and-niche cases. Each migration replaces the legacy
`_cmd`/`cmd`/`cmd_raises` shape with subprocess invocations of the rust
binary.

## Low — niche or known-defer

### CI wheel matrix

`.github/workflows/wheels.yml` with cibuildwheel + maturin-action
producing manylinux x86_64/aarch64, macOS universal2, win_amd64 wheels.
End users on PyPI get prebuilt artifacts (today's build relies on a
local rust toolchain via the PEP 517 wrapper).

### `west update --stats` — per-project timing breakdown.

### `west update --no-update` — skip writing manifest-rev.

### `west init --rename-delay` — Windows-NTFS workaround.

### Drop legacy aliases now that v2 has settled

`--manifest-path-from-yaml` (`list.rs:58`), `--gf` (`update/mod.rs:75`),
the `init` positional `directory` (`init.rs:43`). Note: `--mr` and
`--manifest-rev` on `init --revision` are deliberately kept for v1
muscle memory; the original "drop them" recommendation is overridden
by the more recent "keep v1 invocations working" decision.

### Drop `let _is_tty = …` dead reads in `forall.rs:486` and `grep.rs:566`.

### Rustdoc gap (~52% of pub items have rustdoc)

Mostly internal-feeling pubs (binding glue, etc.); worth closing
before 2.0. Mechanical.

### `value_to_py(Value::String(s))` does `s.clone()` — use `s.as_str()`

`python/data.rs:103`. Per-string nit; matters at scale.

### `SubmoduleScope::Specific(&[])` no-op contract should be enforced via a default method

`vcs/mod.rs:520-522` documents the contract as "per-client must early-
return"; a default `update_submodules` arm checking
`scope.is_empty()` would centralise it.

### `Vcs::update_submodules` positional args — wrap into `SubmoduleSpec`

Mixes data + behavior + hint positionally (`scope`, `strategy`,
`reference`). Spec-struct pattern matches the rest of the trait
(`CloneSpec`, `FetchSpec`, `DiffSpec`, `StatusSpec`).

### Document "no `canonicalize`, no symlink follow" invariant at `topdir.rs:1`

Security-relevant choice currently invisible. One-line doc.

### `u64` doesn't-fit-`i64` stringifies — could surface as Python int

`python/data.rs:97-100`. Python has arbitrary-precision int —
`PyLong::new_from_str` would round-trip cleanly. Edge case, technically
lossy today.

### Re-audit `from_py_object` attribute on `Submodule` / `GroupFilterEntry` / `Project` / `ManifestRepo`

Drop the attribute where the type is never converted *back* from Python
(parallels what was already done for `ProjectFilter` via
`skip_from_py_object`).

## Docs to write

`MIGRATION.md` gaps surfaced by the breaking-changes audit. Each is a
bullet that needs drafting + landing in `MIGRATION.md`:

- System / global config file paths moved (`westconfig` →
  `west/config.toml`; dotfile → XDG-always).
- New `~/.config/west/conf.d/*.toml` drop-in layer between global and
  local.
- `west status` requires `--` before pass-through args
  (`last = true` in `status.rs:88` vs v1's
  `accepts_unknown_args = True`).
- `west update --stats` dropped.
- `west init --rename-delay` dropped.
- EPIPE: v1 exited 0, v2 exits 141 (SIGPIPE convention) via
  `bin/west.rs`'s `SIG_DFL` reset.
- Format engine swap (python `str.format` → `strfmt`) — subtle
  conversion-specifier and width-spec divergences.
- New env vars: `WEST_PYTHON`, `VIRTUAL_ENV` (consulted for
  extension dispatch), `WEST_TOPDIR` (now exported to extension
  subprocesses).
