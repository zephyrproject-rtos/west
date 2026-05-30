//! `west config migrate` — convert a v1 INI config to v2 TOML.
//!
//! v1 west stored config in INI format (python's `configparser`) at
//! three platform-specific locations. See `_location()` in v1.5.0's
//! `src/west/configuration.py` for the canonical reference; reproduced
//! here as a table:
//!
//! | scope  | linux / bsd          | macos                       | windows                       |
//! |--------|----------------------|-----------------------------|-------------------------------|
//! | system | `/etc/westconfig`    | `/usr/local/etc/westconfig` | `%PROGRAMDATA%\west\config`   |
//! | global | `$XDG_CONFIG_HOME/west/config` if `XDG_CONFIG_HOME` set, else `~/.westconfig` | `~/.westconfig` | `~/.westconfig` |
//! | local  | `<topdir>/.west/config` | (same)                   | (same)                        |
//!
//! v2 writes TOML at the conventional paths resolved by
//! [`west_core::config_paths`].
//!
//! Key shape carries over by construction: configparser's
//! `parse_key` is `dotted.split('.', 1)`, so the section is always
//! the *first* path component and the key holds the remainder. INI
//! `[section]\nrest.of.key = value` reassembles to the dotted
//! `section.rest.of.key` v2 reads natively. No renames needed.
//!
//! The work is type coercion: v1 stored everything as strings; v2's
//! typed getters refuse string-shaped booleans / integers / lists.
//! Known-typed keys live in a static table below; unknown keys fall
//! back to string with a warning. Coercion failures (e.g.,
//! `update.rebase = sometimes`) also fall back to string with a
//! warning — the migration always completes; users fix specific
//! keys afterwards with `west config set`.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use clap::Args;
use ini::Ini;
use toml_edit::{DocumentMut, Item, Table, Value};

use west_core::config::ConfigValue;
use west_core::config_paths::ResolvedConfig;

use super::{LoadedConfig, ScopeArgs, scope_to_path};
use crate::exit;

#[derive(Args, Debug)]
pub struct MigrateArgs {
    #[command(flatten)]
    pub scope: ScopeArgs,

    /// Override the v1 source path. Without this the source is
    /// derived from the chosen scope using v1.5.0's `_location()`
    /// rules.
    #[arg(long, value_name = "PATH")]
    pub from: Option<PathBuf>,

    /// Parse + coerce + print the resulting TOML to stdout; write
    /// nothing to disk.
    #[arg(long)]
    pub dry_run: bool,

    /// Overwrite the v2 target if it already exists.
    #[arg(long)]
    pub force: bool,

    /// Skip the type-inference heuristic for keys not in v2's known
    /// registry — keep their v1 values as verbatim TOML strings
    /// instead of promoting clean `true`/`false` → bool and pure
    /// integers → int. Off by default (inference is on); pass this
    /// when you'd rather audit each user-defined key by hand.
    #[arg(long = "no-infer-types")]
    pub no_infer_types: bool,
}

pub fn run(args: MigrateArgs, loaded: &LoadedConfig) -> ExitCode {
    let scopes = match resolve_scopes(&args, &loaded.resolved) {
        Ok(s) => s,
        Err(e) => {
            log::error!("{e}");
            return exit::usage();
        }
    };
    if scopes.is_empty() {
        log::warn!("no v1 config files found at any conventional scope");
        return exit::SUCCESS;
    }
    // Default-on inference; `--no-infer-types` flips off.
    let infer = !args.no_infer_types;
    let mut overall_ok = true;
    for (label, v1, v2) in scopes {
        if !migrate_one(&label, &v1, &v2, args.dry_run, args.force, infer) {
            overall_ok = false;
        }
    }
    if overall_ok {
        exit::SUCCESS
    } else {
        exit::FAILURE
    }
}

/// Resolve `(label, v1_path, v2_path)` triples to migrate. The label
/// is for diagnostic prefixing ("system: ...", "local: ...").
fn resolve_scopes(
    args: &MigrateArgs,
    resolved: &ResolvedConfig,
) -> Result<Vec<(String, PathBuf, PathBuf)>, String> {
    if let Some(v1) = &args.from {
        let v2 = scope_to_path(&args.scope, resolved)?.ok_or_else(|| {
            "--from requires --system / --global / --local / --file to set the target".to_owned()
        })?;
        return Ok(vec![("custom".to_owned(), v1.clone(), v2)]);
    }
    if args.scope.is_set() {
        let v2 = scope_to_path(&args.scope, resolved)?
            .expect("scope_to_path returns Some when ScopeArgs::is_set");
        let v1 = scope_to_v1_path(&args.scope).ok_or_else(|| {
            "no conventional v1 path for the chosen scope on this platform".to_owned()
        })?;
        return Ok(vec![(scope_label(&args.scope), v1, v2)]);
    }
    // No scope flag: walk all three conventional scopes, keep the ones whose
    // v1 file actually exists.
    let mut out = Vec::new();
    for (label, v1_opt, v2_opt) in [
        ("system", v1_system_path(), resolved.system.clone()),
        ("global", v1_global_path(), resolved.global.clone()),
        (
            "local",
            local_v1_from_v2(resolved.local.as_deref()),
            resolved.local.clone(),
        ),
    ] {
        let (Some(v1), Some(v2)) = (v1_opt, v2_opt) else {
            continue;
        };
        if v1.exists() {
            out.push((label.to_owned(), v1, v2));
        }
    }
    Ok(out)
}

fn scope_label(s: &ScopeArgs) -> String {
    if s.system {
        "system"
    } else if s.global {
        "global"
    } else if s.local {
        "local"
    } else if s.file.is_some() {
        "file"
    } else {
        "?"
    }
    .to_owned()
}

fn scope_to_v1_path(s: &ScopeArgs) -> Option<PathBuf> {
    if s.system {
        v1_system_path()
    } else if s.global {
        v1_global_path()
    } else if s.local {
        let cwd = std::env::current_dir().ok()?;
        let top = west_core::topdir::topdir(&cwd).ok()?;
        Some(top.join(".west").join("config"))
    } else {
        // `--file` is the v2 target; the v1 source is the same path
        // minus its `.toml` extension. With `--from` they decouple.
        s.file.as_ref().map(|f| f.with_extension(""))
    }
}

fn local_v1_from_v2(v2_local: Option<&Path>) -> Option<PathBuf> {
    // v2 local: <topdir>/.west/config.toml → v1: <topdir>/.west/config.
    // The local scope is the one case where "strip .toml" is the right rule
    // (system/global have entirely different layouts).
    v2_local.map(|p| p.with_extension(""))
}

// Per-platform v1 defaults. v1.5.0's `_location()` also consulted
// `WEST_CONFIG_{SYSTEM,GLOBAL,LOCAL}` env vars ahead of these defaults, but
// we deliberately don't — those env vars carry v2 meaning in a running v2
// install (they point at v2 TOML files), and honouring them would conflict.
// Users with custom v1 layouts pass `--from PATH` instead.

#[cfg(target_os = "linux")]
fn env_path(var: &str) -> Option<PathBuf> {
    std::env::var_os(var)
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
}

#[cfg(any(
    target_os = "linux",
    target_os = "freebsd",
    target_os = "netbsd",
    target_os = "openbsd",
    target_os = "dragonfly",
))]
fn v1_system_path() -> Option<PathBuf> {
    Some(PathBuf::from("/etc/westconfig"))
}

#[cfg(target_os = "macos")]
fn v1_system_path() -> Option<PathBuf> {
    Some(PathBuf::from("/usr/local/etc/westconfig"))
}

#[cfg(target_os = "windows")]
fn v1_system_path() -> Option<PathBuf> {
    std::env::var_os("PROGRAMDATA").map(|p| PathBuf::from(p).join("west").join("config"))
}

#[cfg(not(any(
    target_os = "linux",
    target_os = "macos",
    target_os = "windows",
    target_os = "freebsd",
    target_os = "netbsd",
    target_os = "openbsd",
    target_os = "dragonfly",
)))]
fn v1_system_path() -> Option<PathBuf> {
    None
}

#[cfg(target_os = "linux")]
fn v1_global_path() -> Option<PathBuf> {
    // v1.5.0 returned the XDG path unconditionally when `XDG_CONFIG_HOME`
    // was set, even if no file existed there. For migration we deviate: a
    // user who set XDG late and never moved their `~/.westconfig` should
    // still get their global config migrated. Try XDG first; if the file
    // isn't there, fall back to `~/.westconfig`. (Users who genuinely want
    // the XDG path migrated when the file is missing won't care — there's
    // nothing to migrate either way.)
    if let Some(xdg) = env_path("XDG_CONFIG_HOME") {
        let xdg_path = xdg.join("west").join("config");
        if xdg_path.exists() {
            return Some(xdg_path);
        }
    }
    dirs::home_dir().map(|h| h.join(".westconfig"))
}

#[cfg(not(target_os = "linux"))]
fn v1_global_path() -> Option<PathBuf> {
    dirs::home_dir().map(|h| h.join(".westconfig"))
}

// --- per-pair migration ------------------------------------------------------

fn migrate_one(label: &str, v1: &Path, v2: &Path, dry_run: bool, force: bool, infer: bool) -> bool {
    if !v1.exists() {
        log::error!("{label}: v1 source not found at {}", v1.display());
        return false;
    }
    if v2.exists() && !force && !dry_run {
        log::warn!(
            "{label}: v2 target {} already exists; pass --force to overwrite",
            v2.display()
        );
        return false;
    }
    log::info!("{label}: migrating {}", v1.display());
    let notes = match parse_v1(v1, infer) {
        Ok(n) => n,
        Err(e) => {
            log::error!("{label}: {e}");
            return false;
        }
    };
    emit_traces(label, &notes);

    // Collect the (key, value) pairs for TOML emission. Stable
    // alphabetical order independent of source-file order, for
    // reproducible v2 output.
    let mut pairs: Vec<(String, ConfigValue)> = notes
        .iter()
        .map(|n| (n.key.clone(), n.value.clone()))
        .collect();
    pairs.sort_by(|a, b| a.0.cmp(&b.0));

    let body = build_v2(&pairs, v1);
    if dry_run {
        print!("# --- {label}: would write to {} ---\n{body}", v2.display());
        log::info!(
            "{label}: {}",
            summary_clause(&notes, "would write", v2)
        );
        return true;
    }
    if let Some(parent) = v2.parent()
        && !parent.as_os_str().is_empty()
        && let Err(e) = fs::create_dir_all(parent)
    {
        log::error!("{label}: create parent dir {}: {e}", parent.display());
        return false;
    }
    if let Err(e) = write_atomic(v2, &body) {
        log::error!("{label}: write {}: {e}", v2.display());
        return false;
    }
    log::info!("{label}: {}", summary_clause(&notes, "wrote", v2));
    true
}

/// One info-level log line per migrated key. The format aims at
/// scannable left-aligned `key = value (type[, qualifier])` lines on
/// stderr; the eventual TOML body goes to stdout (under `--dry-run`)
/// or to disk and is separate from this trace.
fn emit_traces(label: &str, notes: &[Note]) {
    for n in notes {
        let value_repr = render_value(&n.value);
        let ty = type_name(&n.value);
        match &n.kind {
            NoteKind::Kept => log::info!("{label}: {key} = {value_repr} ({ty})", key = n.key),
            NoteKind::Inferred => log::info!(
                "{label}: {key} = {value_repr} ({ty}, inferred)",
                key = n.key,
            ),
            NoteKind::Unknown => log::info!(
                "{label}: {key} = {value_repr} ({ty}, unknown key, kept verbatim)",
                key = n.key,
            ),
            NoteKind::Renamed { from } => log::info!(
                "{label}: {from} → {key} = {value_repr} ({ty}, renamed)",
                key = n.key,
            ),
            NoteKind::CoercionFailed { expected } => log::warn!(
                "{label}: {key} = {value_repr} ({ty}, expected {expected}; preserved as string)",
                key = n.key,
            ),
        }
    }
}

/// "wrote 5 keys to /path (3 string, 1 int, 1 bool); 1 rename; 2 warnings"
/// — the verb is parameterised so `--dry-run` can say "would write".
fn summary_clause(notes: &[Note], verb: &str, v2: &Path) -> String {
    let mut by_type: std::collections::BTreeMap<&'static str, usize> = Default::default();
    let mut renames = 0usize;
    let mut warnings = 0usize;
    for n in notes {
        *by_type.entry(type_name(&n.value)).or_insert(0) += 1;
        if matches!(n.kind, NoteKind::Renamed { .. }) {
            renames += 1;
        }
        if matches!(n.kind, NoteKind::CoercionFailed { .. }) {
            warnings += 1;
        }
    }
    let count = notes.len();
    let plural = if count == 1 { "" } else { "s" };

    let mut out = format!(
        "{verb} {count} key{plural} to {} (",
        v2.display(),
    );
    let parts: Vec<String> = by_type
        .iter()
        .map(|(ty, n)| format!("{n} {ty}{}", if *n == 1 { "" } else { "s" }))
        .collect();
    out.push_str(&parts.join(", "));
    out.push(')');
    if renames > 0 {
        out.push_str(&format!(
            "; {renames} rename{}",
            if renames == 1 { "" } else { "s" }
        ));
    }
    if warnings > 0 {
        out.push_str(&format!(
            "; {warnings} warning{}",
            if warnings == 1 { "" } else { "s" }
        ));
    }
    out
}

fn type_name(v: &ConfigValue) -> &'static str {
    match v {
        ConfigValue::String(_) => "string",
        ConfigValue::Bool(_) => "bool",
        ConfigValue::Integer(_) => "int",
        ConfigValue::Float(_) => "float",
        ConfigValue::List(_) => "list",
    }
}

/// Compact TOML-ish rendering for log output. Strings are quoted so
/// it's clear the value is textual; small lists are spelled out
/// element-by-element; large lists collapse to `[N elements]` so a
/// 100-project filter doesn't dump a screenful onto the log.
fn render_value(v: &ConfigValue) -> String {
    match v {
        ConfigValue::String(s) => format!("{s:?}"),
        ConfigValue::Bool(b) => format!("{b}"),
        ConfigValue::Integer(i) => format!("{i}"),
        ConfigValue::Float(f) => format!("{f}"),
        ConfigValue::List(items) => {
            if items.len() <= 3 {
                let parts: Vec<String> = items.iter().map(render_value).collect();
                format!("[{}]", parts.join(", "))
            } else {
                format!("[{} elements]", items.len())
            }
        }
    }
}

// --- v1 parse ----------------------------------------------------------------

/// One per (target v2 key) emitted by `parse_v1`. A split rename
/// (one v1 key → two v2 keys) produces two Notes; everything else
/// produces one. `kind` carries the per-key migration story so the
/// caller can format each translation uniformly without re-deriving
/// state from the value.
struct Note {
    key: String,
    value: ConfigValue,
    kind: NoteKind,
}

enum NoteKind {
    /// Known v2 key, value coerced cleanly to its registered type.
    Kept,
    /// Unknown v2 key, inference picked a non-string type.
    Inferred,
    /// Unknown v2 key, kept verbatim as string (inference off OR
    /// didn't match any heuristic).
    Unknown,
    /// Rename rule fired. `from` is the v1 source key.
    Renamed { from: String },
    /// Known-typed v2 key but the v1 value couldn't coerce; value
    /// preserved as a string. User-actionable — surfaces as a warning.
    CoercionFailed { expected: &'static str },
}

fn parse_v1(path: &Path, infer: bool) -> Result<Vec<Note>, String> {
    let conf = Ini::load_from_file(path).map_err(|e| format!("parse {}: {e}", path.display()))?;
    let mut notes: Vec<Note> = Vec::new();
    for (section, props) in conf.iter() {
        match section {
            None => {
                // rust-ini's "general" section holds keys appearing before
                // any [section] header. configparser raises
                // MissingSectionHeaderError for that shape, so a v1 file
                // shouldn't have any — treat as malformed.
                if props.iter().next().is_some() {
                    return Err(format!(
                        "{}: keys appear outside any [section] header",
                        path.display()
                    ));
                }
            }
            Some(s) if s.eq_ignore_ascii_case("DEFAULT") => {
                // configparser's DEFAULT section leaks its keys into every
                // other section via interpolation. Reject rather than
                // silently mis-materialize.
                if props.iter().next().is_some() {
                    return Err(format!(
                        "{}: [DEFAULT] section is not supported by `west config migrate`",
                        path.display()
                    ));
                }
            }
            Some(section_name) => {
                for (key, value) in props.iter() {
                    let dotted = format!("{section_name}.{key}");
                    let rewrite = rewrite_v1_key(&dotted);
                    let renamed_from = match &rewrite {
                        Rewrite::Same => None,
                        Rewrite::Renamed(_) => Some(dotted.clone()),
                    };
                    for target_key in rewrite.targets(&dotted) {
                        let (cv, outcome) = coerce(target_key, value, infer);
                        let kind = match (renamed_from.as_ref(), outcome) {
                            (Some(from), _) => NoteKind::Renamed { from: from.clone() },
                            (None, CoerceOutcome::Kept) => NoteKind::Kept,
                            (None, CoerceOutcome::Inferred) => NoteKind::Inferred,
                            (None, CoerceOutcome::Unknown) => NoteKind::Unknown,
                            (None, CoerceOutcome::CoercionFailed { expected }) => {
                                NoteKind::CoercionFailed { expected }
                            }
                        };
                        notes.push(Note {
                            key: target_key.to_owned(),
                            value: cv,
                            kind,
                        });
                    }
                }
            }
        }
    }
    Ok(notes)
}

// --- v1 → v2 key rewrites ----------------------------------------------------
//
// A handful of v1 keys moved namespace or split into multiple v2 keys.
// Apply the rewrite before coercion so the v2 key drives the type lookup
// (which is what landed in v2's `get_bool` / `get_i64` / `get_list_str`
// callsites).

enum Rewrite {
    /// Key carries over unchanged (the common case).
    Same,
    /// One v1 key maps to one or more v2 keys with the same value
    /// applied to each. Two-element form covers the
    /// `update.sync-submodules` → `tool.git.submodules.{sync,recurse}`
    /// split.
    Renamed(Vec<&'static str>),
}

impl Rewrite {
    /// v2 key(s) this rewrite produces for `original`. Returns the
    /// original key untouched for the `Same` case so the parse loop
    /// can branch on the rewrite once and then iterate uniformly.
    fn targets<'a>(&'a self, original: &'a str) -> Vec<&'a str> {
        match self {
            Rewrite::Same => vec![original],
            Rewrite::Renamed(ks) => ks.to_vec(),
        }
    }
}

/// v2 key(s) this v1 key migrates to. Most v1 keys carry over
/// unchanged; the listed ones moved namespace in v2 or were split
/// into multiple knobs.
fn rewrite_v1_key(v1_key: &str) -> Rewrite {
    match v1_key {
        // String enum (`always` | `smart`), same value shape, just moved
        // under the `tool.<client>.*` namespace introduced in v2.
        "update.fetch" => Rewrite::Renamed(vec!["tool.git.fetch.strategy"]),
        // v1's single boolean controlled both `git submodule sync` and
        // `git submodule update --init --recursive`. v2 separates those
        // into two knobs; both should track the v1 value.
        "update.sync-submodules" => Rewrite::Renamed(vec![
            "tool.git.submodules.sync",
            "tool.git.submodules.recurse",
        ]),
        _ => Rewrite::Same,
    }
}

// --- coercion ----------------------------------------------------------------

#[derive(Copy, Clone)]
enum Typ {
    Bool,
    Int,
    ListStr,
    /// Known-string v2 key (or `alias.*` namespace). Preserve the
    /// value verbatim with no warning — the migration is lossless
    /// for these.
    String,
}

/// Expected type for known v2 keys. Sourced by grepping `get_bool` /
/// `get_i64` / `get_list_str` / `get_str` callsites across `west-cli`
/// + `west-core`. Anything not listed here gets a string + an
/// "unknown key" warning.
fn known_type(key: &str) -> Option<Typ> {
    // `alias.<name>` is a user-defined namespace (see
    // `commands/help.rs::strip_prefix("alias.")`); values are
    // always strings. Migrate silently — there's no v1 → v2 loss to
    // flag.
    if key.starts_with("alias.") {
        return Some(Typ::String);
    }
    Some(match key {
        "commands.allow_extensions"
        | "compare.ignore-branches"
        | "output.raw"
        | "output.quiet"
        | "update.rebase"
        | "update.keep-descendants"
        | "update.narrow"
        | "tool.git.fetch.tags"
        | "tool.git.fetch.narrow"
        | "tool.git.fetch.force"
        | "tool.git.submodules.recurse"
        | "tool.git.submodules.sync" => Typ::Bool,

        "update.jobs" | "forall.jobs" | "diff.jobs" | "grep.jobs" | "tool.git.fetch.depth" => {
            Typ::Int
        }

        "manifest.group-filter"
        | "manifest.project-filter"
        | "update.group-filter"
        | "tool.git.fetch.extra-args"
        | "tool.git.clone.extra-args"
        | "tool.git.submodules.init-config"
        | "grep.git-grep-args"
        | "grep.ripgrep-args"
        | "grep.grep-args" => Typ::ListStr,

        // String-typed v2 keys west itself reads.
        "color.ui" | "grep.color" | "grep.tool" | "manifest.file" | "manifest.path"
        | "update.auto-cache" | "update.name-cache" | "update.path-cache" => Typ::String,

        _ => return None,
    })
}

/// Per-key outcome of `coerce`. Drives the trace-line kind for each
/// migrated pair. (For rename-rule keys, the caller overrides to
/// `NoteKind::Renamed` regardless of this outcome — it carries the
/// inner story but the trace presents the rename framing.)
enum CoerceOutcome {
    /// Known-typed key, value matched the type cleanly.
    Kept,
    /// Unknown key, inference picked a non-string type.
    Inferred,
    /// Unknown key, kept verbatim as a string.
    Unknown,
    /// Known-typed key but the v1 value didn't fit; value preserved
    /// as a string and the user should investigate.
    CoercionFailed { expected: &'static str },
}

/// Coerce a v1 string value to its v2 type. Drives by `known_type`
/// for west's registered keys; falls back to (inferred / verbatim)
/// string for everything else, depending on the caller's `infer`
/// choice.
fn coerce(key: &str, raw: &str, infer: bool) -> (ConfigValue, CoerceOutcome) {
    match known_type(key) {
        Some(Typ::Bool) => match parse_bool(raw) {
            Some(b) => (ConfigValue::Bool(b), CoerceOutcome::Kept),
            None => (
                ConfigValue::String(raw.to_owned()),
                CoerceOutcome::CoercionFailed { expected: "boolean" },
            ),
        },
        Some(Typ::Int) => match raw.trim().parse::<i64>() {
            Ok(n) => (ConfigValue::Integer(n), CoerceOutcome::Kept),
            Err(_) => (
                ConfigValue::String(raw.to_owned()),
                CoerceOutcome::CoercionFailed { expected: "integer" },
            ),
        },
        Some(Typ::ListStr) => {
            let items: Vec<ConfigValue> = raw
                .split(',')
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(|s| ConfigValue::String(s.to_owned()))
                .collect();
            (ConfigValue::List(items), CoerceOutcome::Kept)
        }
        Some(Typ::String) => (ConfigValue::String(raw.to_owned()), CoerceOutcome::Kept),
        None if infer => match infer_unknown(raw) {
            Some(v) => (v, CoerceOutcome::Inferred),
            None => (ConfigValue::String(raw.to_owned()), CoerceOutcome::Unknown),
        },
        None => (ConfigValue::String(raw.to_owned()), CoerceOutcome::Unknown),
    }
}

/// Conservative type-inference heuristic for unknown keys. v1 INI
/// was string-typed by construction, so the goal is: only promote to
/// a non-string type when the v1 value can't plausibly have been
/// intended as a string with that shape. Returns `None` for "stays
/// as a string" so the caller distinguishes `Inferred` from `Unknown`.
///
/// Rules:
///
/// - `true`/`false`/`yes`/`no`/`on`/`off` (case-insensitive) → bool.
///   These are exactly the tokens configparser's `getboolean()`
///   recognises, so a v1 INI user writing `enabled = yes` was
///   declaring a bool by the idiom of the format they were using.
///   `0` and `1` deliberately stay out of the bool set — they're
///   ambiguous with counts (`max-retries = 1` shouldn't quietly
///   become `true`).
/// - Pure integer matching `^-?(0|[1-9]\d*)$` that fits in `i64` →
///   int. Excludes leading zeros (`0123` is more often an
///   ID/zip/version-prefix than a number) and any decimal/sign/comma.
///   `0` / `1` land here (int 0 / int 1) by definition.
/// - Everything else → `None` (keep as string).
fn infer_unknown(raw: &str) -> Option<ConfigValue> {
    let trimmed = raw.trim();
    match trimmed.to_ascii_lowercase().as_str() {
        "true" | "yes" | "on" => return Some(ConfigValue::Bool(true)),
        "false" | "no" | "off" => return Some(ConfigValue::Bool(false)),
        _ => {}
    }
    if looks_like_int(trimmed)
        && let Ok(n) = trimmed.parse::<i64>()
    {
        return Some(ConfigValue::Integer(n));
    }
    None
}

fn looks_like_int(s: &str) -> bool {
    let body = s.strip_prefix('-').unwrap_or(s);
    if body.is_empty() {
        return false;
    }
    if body == "0" {
        return true;
    }
    // Reject leading zero on multi-char numbers — `0123` should stay
    // a string (zip, ID, version prefix), not become `123`.
    let mut chars = body.chars();
    match chars.next() {
        Some(c) if c.is_ascii_digit() && c != '0' => {}
        _ => return false,
    }
    chars.all(|c| c.is_ascii_digit())
}

/// git-config-style boolean parsing — same accepted set as v1
/// `configparser.getboolean()` plus git's `on`/`off`.
fn parse_bool(s: &str) -> Option<bool> {
    match s.trim().to_ascii_lowercase().as_str() {
        "true" | "yes" | "on" | "1" => Some(true),
        "false" | "no" | "off" | "0" => Some(false),
        _ => None,
    }
}

// --- v2 build ----------------------------------------------------------------

/// Build the v2 TOML document body (header + table tree) as a string.
fn build_v2(pairs: &[(String, ConfigValue)], source: &Path) -> String {
    let mut doc = DocumentMut::new();
    for (key, value) in pairs {
        insert_dotted(doc.as_table_mut(), key, value.clone());
    }
    let header = format!(
        "# Generated by `west config migrate` (west {})\n# Source: {}\n\n",
        env!("CARGO_PKG_VERSION"),
        source.display(),
    );
    format!("{header}{doc}")
}

/// Walk + create intermediate sub-tables; assign the leaf. Mirrors
/// `ensure_table_mut` from west-core, kept private here so this
/// module stays self-contained and deletable when v1 dies.
fn insert_dotted(root: &mut Table, key: &str, value: ConfigValue) {
    let parts: Vec<&str> = key.split('.').collect();
    let (leaf, prefix) = parts.split_last().expect("dotted key non-empty");
    let mut current: &mut Table = root;
    for part in prefix {
        let item = current
            .entry(part)
            .or_insert_with(|| Item::Table(Table::new()));
        current = item.as_table_mut().expect("we just inserted a table");
    }
    current[*leaf] = Item::Value(config_value_to_toml(value));
}

fn config_value_to_toml(v: ConfigValue) -> Value {
    match v {
        ConfigValue::String(s) => Value::from(s),
        ConfigValue::Bool(b) => Value::from(b),
        ConfigValue::Integer(i) => Value::from(i),
        ConfigValue::Float(f) => Value::from(f),
        ConfigValue::List(items) => {
            let arr: toml_edit::Array = items.into_iter().map(config_value_to_toml).collect();
            Value::Array(arr)
        }
    }
}

/// Same temp-file + rename pattern as `west_core::config::write_atomic`,
/// kept private here to avoid widening west-core's public surface for a
/// migration-only path.
fn write_atomic(path: &Path, contents: &str) -> std::io::Result<()> {
    let tmp = path.with_extension("toml.migrate.tmp");
    fs::write(&tmp, contents)?;
    fs::rename(&tmp, path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_bool_accepts_v1_aliases() {
        for s in ["true", "TRUE", "yes", "Yes", "on", "1"] {
            assert_eq!(parse_bool(s), Some(true), "input {s:?}");
        }
        for s in ["false", "no", "OFF", "0"] {
            assert_eq!(parse_bool(s), Some(false), "input {s:?}");
        }
        assert_eq!(parse_bool("maybe"), None);
        assert_eq!(parse_bool(""), None);
    }

    #[test]
    fn coerce_bool_known_key() {
        let (v, o) = coerce("update.rebase", "yes", true);
        assert!(matches!(v, ConfigValue::Bool(true)));
        assert!(matches!(o, CoerceOutcome::Kept));
    }

    #[test]
    fn coerce_bool_falls_back_with_warning() {
        let (v, o) = coerce("update.rebase", "sometimes", true);
        assert!(matches!(v, ConfigValue::String(ref s) if s == "sometimes"));
        assert!(matches!(
            o,
            CoerceOutcome::CoercionFailed { expected: "boolean" }
        ));
    }

    #[test]
    fn coerce_int() {
        let (v, o) = coerce("update.jobs", "8", true);
        assert!(matches!(v, ConfigValue::Integer(8)));
        assert!(matches!(o, CoerceOutcome::Kept));
    }

    #[test]
    fn coerce_list_comma_split_trims() {
        let (v, o) = coerce("manifest.group-filter", " +a, -b , +c", true);
        assert!(matches!(o, CoerceOutcome::Kept));
        let items = match v {
            ConfigValue::List(l) => l,
            _ => panic!("expected list"),
        };
        assert_eq!(items.len(), 3);
        assert!(matches!(items[0], ConfigValue::String(ref s) if s == "+a"));
        assert!(matches!(items[1], ConfigValue::String(ref s) if s == "-b"));
        assert!(matches!(items[2], ConfigValue::String(ref s) if s == "+c"));
    }

    #[test]
    fn rewrite_update_fetch_renames_to_strategy() {
        match rewrite_v1_key("update.fetch") {
            Rewrite::Renamed(ks) => assert_eq!(ks, vec!["tool.git.fetch.strategy"]),
            Rewrite::Same => panic!("expected rename"),
        }
    }

    #[test]
    fn rewrite_sync_submodules_splits_into_two() {
        match rewrite_v1_key("update.sync-submodules") {
            Rewrite::Renamed(ks) => assert_eq!(
                ks,
                vec!["tool.git.submodules.sync", "tool.git.submodules.recurse"]
            ),
            Rewrite::Same => panic!("expected split"),
        }
    }

    #[test]
    fn rewrite_unrelated_key_passes_through() {
        assert!(matches!(rewrite_v1_key("manifest.path"), Rewrite::Same));
        // Verify .targets() returns the original for the Same case.
        assert_eq!(
            Rewrite::Same.targets("manifest.path"),
            vec!["manifest.path"]
        );
    }

    #[test]
    fn unknown_key_with_text_value_kept_as_string() {
        // Non-numeric, non-boolean value on an unknown key stays as a
        // string regardless of `infer` — the heuristic doesn't fire,
        // so the outcome is `Unknown` (verbatim string).
        let (v, o) = coerce("custom.local-only", "hello", true);
        assert!(matches!(v, ConfigValue::String(ref s) if s == "hello"));
        assert!(matches!(o, CoerceOutcome::Unknown));
    }

    #[test]
    fn alias_keys_migrate_silently() {
        // `alias.<name>` is a user-defined namespace whose values are
        // always strings — the known_type table has a dedicated arm
        // so the outcome is `Kept`, not `Unknown`.
        let (v, o) = coerce("alias.run", "build && flash", true);
        assert!(matches!(v, ConfigValue::String(ref s) if s == "build && flash"));
        assert!(matches!(o, CoerceOutcome::Kept));
    }

    #[test]
    fn known_string_keys_migrate_silently() {
        for key in ["manifest.path", "update.auto-cache"] {
            let (v, o) = coerce(key, "anything", true);
            assert!(matches!(v, ConfigValue::String(ref s) if s == "anything"));
            assert!(matches!(o, CoerceOutcome::Kept));
        }
    }

    #[test]
    fn inference_promotes_bool_idioms() {
        // configparser's `getboolean()` recognises true/yes/on (+
        // false/no/off) — a v1 INI user writing those was declaring
        // a bool by the idiom of the format. Inference accepts the
        // same set.
        for (raw, want) in [
            ("true", true),
            ("TRUE", true),
            ("yes", true),
            ("YES", true),
            ("on", true),
            ("false", false),
            ("False", false),
            ("no", false),
            ("off", false),
            ("OFF", false),
        ] {
            let (v, o) = coerce("custom.flag", raw, true);
            assert!(
                matches!(v, ConfigValue::Bool(b) if b == want),
                "{raw:?} → {v:?}"
            );
            assert!(matches!(o, CoerceOutcome::Inferred));
        }
    }

    #[test]
    fn inference_keeps_zero_and_one_as_int_not_bool() {
        // `0` / `1` are deliberately NOT inferred as bool — they're
        // too often counts (`max-retries = 1`, `errors = 0`). They
        // land as int via the integer branch instead.
        for (raw, want) in [("0", 0i64), ("1", 1)] {
            let (v, o) = coerce("custom.count", raw, true);
            assert!(
                matches!(v, ConfigValue::Integer(i) if i == want),
                "{raw:?} → {v:?}"
            );
            assert!(matches!(o, CoerceOutcome::Inferred));
        }
    }

    #[test]
    fn inference_promotes_pure_integer() {
        for (raw, want) in [("42", 42), ("0", 0), ("-7", -7), ("9999", 9999)] {
            let (v, o) = coerce("custom.count", raw, true);
            assert!(
                matches!(v, ConfigValue::Integer(i) if i == want),
                "{raw:?} → {v:?}"
            );
            assert!(matches!(o, CoerceOutcome::Inferred));
        }
    }

    #[test]
    fn inference_skips_leading_zero_integers() {
        // `0123` is much more often a zip / ID / version-prefix than
        // an integer; leave it as a string.
        let (v, o) = coerce("custom.id", "0123", true);
        assert!(matches!(v, ConfigValue::String(ref s) if s == "0123"));
        assert!(matches!(o, CoerceOutcome::Unknown));
    }

    #[test]
    fn no_infer_keeps_unknowns_as_string() {
        // `--no-infer-types` short-circuits the heuristic; even a clean
        // `true` / `42` becomes a verbatim string.
        let (v, o) = coerce("custom.flag", "true", false);
        assert!(matches!(v, ConfigValue::String(ref s) if s == "true"));
        assert!(matches!(o, CoerceOutcome::Unknown));

        let (v, o) = coerce("custom.count", "42", false);
        assert!(matches!(v, ConfigValue::String(ref s) if s == "42"));
        assert!(matches!(o, CoerceOutcome::Unknown));
    }

    #[test]
    fn build_v2_emits_header_and_nested_tables() {
        let pairs = vec![
            (
                "manifest.path".to_owned(),
                ConfigValue::String("west.yml".to_owned()),
            ),
            ("update.jobs".to_owned(), ConfigValue::Integer(8)),
            ("tool.git.fetch.tags".to_owned(), ConfigValue::Bool(true)),
        ];
        let s = build_v2(&pairs, Path::new("/tmp/oldconfig"));
        assert!(s.starts_with("# Generated by `west config migrate` (west "));
        assert!(s.contains("# Source: /tmp/oldconfig"));
        // toml_edit emits nested tables as [section] headers.
        assert!(s.contains("[manifest]"));
        assert!(s.contains("path = \"west.yml\""));
        assert!(s.contains("[update]"));
        assert!(s.contains("jobs = 8"));
        assert!(s.contains("[tool.git.fetch]"));
        assert!(s.contains("tags = true"));
    }

    #[test]
    fn local_v1_from_v2_strips_toml_extension() {
        let v2 = PathBuf::from("/work/.west/config.toml");
        assert_eq!(
            local_v1_from_v2(Some(&v2)),
            Some(PathBuf::from("/work/.west/config"))
        );
    }

    #[test]
    fn local_v1_from_v2_none_when_no_workspace() {
        assert_eq!(local_v1_from_v2(None), None);
    }
}
