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
}

pub fn run(args: MigrateArgs, loaded: &LoadedConfig) -> ExitCode {
    let scopes = match resolve_scopes(&args, &loaded.resolved) {
        Ok(s) => s,
        Err(e) => {
            log::error!("{e}");
            return ExitCode::from(2);
        }
    };
    if scopes.is_empty() {
        log::warn!("no v1 config files found at any conventional scope");
        return ExitCode::SUCCESS;
    }
    let mut overall_ok = true;
    for (label, v1, v2) in scopes {
        if !migrate_one(&label, &v1, &v2, args.dry_run, args.force) {
            overall_ok = false;
        }
    }
    if overall_ok {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
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
        let v1 = scope_to_v1_path(&args.scope)
            .ok_or_else(|| "no conventional v1 path for the chosen scope on this platform".to_owned())?;
        return Ok(vec![(scope_label(&args.scope), v1, v2)]);
    }
    // No scope flag: walk all three conventional scopes, keep the ones whose
    // v1 file actually exists.
    let mut out = Vec::new();
    for (label, v1_opt, v2_opt) in [
        ("system", v1_system_path(), resolved.system.clone()),
        ("global", v1_global_path(), resolved.global.clone()),
        ("local", local_v1_from_v2(resolved.local.as_deref()), resolved.local.clone()),
    ] {
        let (Some(v1), Some(v2)) = (v1_opt, v2_opt) else { continue };
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

fn migrate_one(label: &str, v1: &Path, v2: &Path, dry_run: bool, force: bool) -> bool {
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
    let (pairs, warnings) = match parse_v1(v1) {
        Ok(p) => p,
        Err(e) => {
            log::error!("{label}: {e}");
            return false;
        }
    };
    for w in &warnings {
        log::warn!("{label}: {w}");
    }
    let body = build_v2(&pairs, v1);
    if dry_run {
        print!("# --- {label}: would write to {} ---\n{body}", v2.display());
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
    log::info!(
        "{label}: migrated {} → {} ({} key{})",
        v1.display(),
        v2.display(),
        pairs.len(),
        if pairs.len() == 1 { "" } else { "s" },
    );
    true
}

// --- v1 parse ----------------------------------------------------------------

#[allow(clippy::type_complexity)]
fn parse_v1(path: &Path) -> Result<(Vec<(String, ConfigValue)>, Vec<String>), String> {
    let conf = Ini::load_from_file(path).map_err(|e| format!("parse {}: {e}", path.display()))?;
    let mut pairs: Vec<(String, ConfigValue)> = Vec::new();
    let mut warnings: Vec<String> = Vec::new();
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
                    if let Rewrite::Renamed(new_keys) = &rewrite {
                        // Successful translation, not a failure — flag it so
                        // the user knows their v1 key shape moved namespace.
                        warnings.push(format!(
                            "{dotted} renamed to {} (v1 key removed in v2)",
                            new_keys.join(" + ")
                        ));
                    }
                    for target_key in rewrite.targets(&dotted) {
                        let (cv, warn) = coerce(target_key, value);
                        pairs.push((target_key.to_owned(), cv));
                        if let Some(w) = warn {
                            warnings.push(w);
                        }
                    }
                }
            }
        }
    }
    // Stable ordering by dotted key — independent of v1 INI section order,
    // gives reproducible v2 output across migration runs.
    pairs.sort_by(|a, b| a.0.cmp(&b.0));
    Ok((pairs, warnings))
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
        "update.sync-submodules" => {
            Rewrite::Renamed(vec!["tool.git.submodules.sync", "tool.git.submodules.recurse"])
        }
        _ => Rewrite::Same,
    }
}

// --- coercion ----------------------------------------------------------------

#[derive(Copy, Clone)]
enum Typ {
    Bool,
    Int,
    ListStr,
}

/// Expected type for known v2 keys. Sourced by grepping `get_bool` /
/// `get_i64` / `get_list_str` callsites across `west-cli` + `west-core`.
/// Anything not listed here gets a string + an "unknown key" warning.
fn known_type(key: &str) -> Option<Typ> {
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

        "update.jobs"
        | "forall.jobs"
        | "diff.jobs"
        | "grep.jobs"
        | "tool.git.fetch.depth" => Typ::Int,

        "manifest.group-filter"
        | "manifest.project-filter"
        | "update.group-filter"
        | "tool.git.fetch.extra-args"
        | "tool.git.clone.extra-args"
        | "tool.git.submodules.init-config"
        | "grep.git-grep-args"
        | "grep.ripgrep-args"
        | "grep.grep-args" => Typ::ListStr,

        _ => return None,
    })
}

/// Coerce a v1 string value to its v2 type, falling back to string +
/// warning if the type doesn't fit (or the key isn't in the v2
/// registry).
fn coerce(key: &str, raw: &str) -> (ConfigValue, Option<String>) {
    match known_type(key) {
        Some(Typ::Bool) => match parse_bool(raw) {
            Some(b) => (ConfigValue::Bool(b), None),
            None => (
                ConfigValue::String(raw.to_owned()),
                Some(format!(
                    "{key}: expected boolean, got {raw:?}; preserved as string"
                )),
            ),
        },
        Some(Typ::Int) => match raw.trim().parse::<i64>() {
            Ok(n) => (ConfigValue::Integer(n), None),
            Err(_) => (
                ConfigValue::String(raw.to_owned()),
                Some(format!(
                    "{key}: expected integer, got {raw:?}; preserved as string"
                )),
            ),
        },
        Some(Typ::ListStr) => {
            let items: Vec<ConfigValue> = raw
                .split(',')
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(|s| ConfigValue::String(s.to_owned()))
                .collect();
            (ConfigValue::List(items), None)
        }
        None => (
            ConfigValue::String(raw.to_owned()),
            Some(format!("{key}: not recognised in v2; preserved as string")),
        ),
    }
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
        let (v, w) = coerce("update.rebase", "yes");
        assert!(matches!(v, ConfigValue::Bool(true)));
        assert!(w.is_none());
    }

    #[test]
    fn coerce_bool_falls_back_with_warning() {
        let (v, w) = coerce("update.rebase", "sometimes");
        assert!(matches!(v, ConfigValue::String(ref s) if s == "sometimes"));
        let w = w.unwrap();
        assert!(w.contains("expected boolean"));
    }

    #[test]
    fn coerce_int() {
        let (v, _) = coerce("update.jobs", "8");
        assert!(matches!(v, ConfigValue::Integer(8)));
    }

    #[test]
    fn coerce_list_comma_split_trims() {
        let (v, w) = coerce("manifest.group-filter", " +a, -b , +c");
        assert!(w.is_none());
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
        assert_eq!(Rewrite::Same.targets("manifest.path"), vec!["manifest.path"]);
    }

    #[test]
    fn unknown_key_preserved_with_warning() {
        let (v, w) = coerce("custom.local-only", "hello");
        assert!(matches!(v, ConfigValue::String(ref s) if s == "hello"));
        assert!(w.unwrap().contains("not recognised"));
    }

    #[test]
    fn build_v2_emits_header_and_nested_tables() {
        let pairs = vec![
            (
                "manifest.path".to_owned(),
                ConfigValue::String("west.yml".to_owned()),
            ),
            ("update.jobs".to_owned(), ConfigValue::Integer(8)),
            (
                "tool.git.fetch.tags".to_owned(),
                ConfigValue::Bool(true),
            ),
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
