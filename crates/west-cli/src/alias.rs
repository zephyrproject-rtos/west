//! Alias resolution for `west`.
//!
//! Reads `alias.<name>` from the loaded `Configuration` and rewrites argv to
//! the expansion target. Recursive aliases are supported; a visited-name set
//! prevents infinite loops.
//!
//! Aliases must resolve to another command name as their first token: no
//! leading `-`. Top-level flags (`-C`, `-v`/`-q`, `--config`, `--config-file`)
//! are user-supplied only and not propagated through aliases.

use std::collections::HashSet;
use std::ffi::OsString;

use clap::Parser;

use west_core::config::{ConfigError, ConfigValue, Configuration};

use crate::Cli;
use crate::commands::Command;

#[derive(Debug, thiserror::Error)]
pub enum AliasError {
    /// `alias.<name>` resolves to an empty argv.
    #[error("alias {0:?} is empty")]
    Empty(String),
    /// String form was un-parseable by shlex.
    #[error("alias {0:?}: shell-style split failed (mismatched quotes?)")]
    Shlex(String),
    /// Array form contains a non-string element.
    #[error("alias {name:?}: list element of type {kind} is not a string")]
    NotAString { name: String, kind: &'static str },
    /// `alias.<name>` is set but not a string-or-array-of-strings.
    #[error("alias {name:?}: expected string or list of strings, got {kind}")]
    BadType { name: String, kind: &'static str },
    /// First token of the expansion starts with `-` (looks like a flag).
    /// Aliases must resolve to another command name as their first token.
    #[error("alias {name:?} must begin with a command name (got {token:?})")]
    FirstTokenIsFlag { name: String, token: String },
    /// Underlying config lookup failed.
    #[error(transparent)]
    Config(#[from] ConfigError),
    /// argv didn't contain the subcommand token at expansion time
    /// (defensive; shouldn't happen in practice).
    #[error("alias {0:?}: subcommand token vanished from argv")]
    SubcommandTokenMissing(String),
}

/// Parse `alias.<name>` into an argv. Returns `Ok(None)` if the alias is
/// unset. Validates that the first token doesn't start with `-`.
pub fn lookup(cfg: &Configuration, name: &str) -> Result<Option<Vec<String>>, AliasError> {
    let key = format!("alias.{name}");
    let argv = match cfg.get(&key)? {
        None => return Ok(None),
        Some(ConfigValue::String(s)) => {
            shlex::split(&s).ok_or_else(|| AliasError::Shlex(name.to_owned()))?
        }
        Some(ConfigValue::List(items)) => {
            let mut out = Vec::with_capacity(items.len());
            for item in items {
                match item {
                    ConfigValue::String(s) => out.push(s),
                    other => {
                        return Err(AliasError::NotAString {
                            name: name.to_owned(),
                            kind: variant_name(&other),
                        });
                    }
                }
            }
            out
        }
        Some(other) => {
            return Err(AliasError::BadType {
                name: name.to_owned(),
                kind: variant_name(&other),
            });
        }
    };
    if argv.is_empty() {
        return Err(AliasError::Empty(name.to_owned()));
    }
    if argv[0].starts_with('-') {
        return Err(AliasError::FirstTokenIsFlag {
            name: name.to_owned(),
            token: argv[0].clone(),
        });
    }
    Ok(Some(argv))
}

/// Resolution loop. Re-parses `argv` with clap on each iteration; if the
/// resulting subcommand name has an alias and hasn't been expanded yet,
/// splice the alias into argv and re-parse. Stops when the name has no
/// alias OR has already been expanded once.
pub fn resolve_loop(mut argv: Vec<OsString>, cfg: &Configuration) -> Result<Cli, AliasError> {
    let mut visited: HashSet<String> = HashSet::new();
    loop {
        let cli = Cli::parse_from(&argv);
        let name = subcommand_token(&cli.command);
        if visited.contains(&name) {
            return Ok(cli);
        }
        let alias_argv = match lookup(cfg, &name)? {
            Some(a) => a,
            None => return Ok(cli),
        };
        log::trace!("expanding alias {name:?} → {alias_argv:?}");
        visited.insert(name.clone());

        // Replace the FIRST occurrence of `name` in argv (after argv[0]).
        let pos = argv
            .iter()
            .skip(1)
            .position(|a| a.to_str() == Some(name.as_str()))
            .map(|p| p + 1)
            .ok_or_else(|| AliasError::SubcommandTokenMissing(name.clone()))?;
        let new: Vec<OsString> = alias_argv.iter().map(OsString::from).collect();
        argv.splice(pos..=pos, new);
    }
}

fn subcommand_token(cmd: &Command) -> String {
    match cmd {
        Command::Config(_) => "config".to_owned(),
        Command::Diff(_) => "diff".to_owned(),
        Command::Exec(_) => "exec".to_owned(),
        Command::Forall(_) => "forall".to_owned(),
        Command::Help(_) => "help".to_owned(),
        Command::Init(_) => "init".to_owned(),
        Command::List(_) => "list".to_owned(),
        Command::Manifest(_) => "manifest".to_owned(),
        Command::Topdir => "topdir".to_owned(),
        Command::Update(_) => "update".to_owned(),
        Command::External(args) => args
            .first()
            .map(|a| a.to_string_lossy().into_owned())
            .unwrap_or_default(),
    }
}

fn variant_name(v: &ConfigValue) -> &'static str {
    match v {
        ConfigValue::String(_) => "string",
        ConfigValue::Bool(_) => "bool",
        ConfigValue::Integer(_) => "integer",
        ConfigValue::Float(_) => "float",
        ConfigValue::List(_) => "list",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn cfg_with(toml: &str) -> (TempDir, Configuration) {
        let tmp = TempDir::new().unwrap();
        let p = tmp.path().join("c.toml");
        std::fs::write(&p, toml).unwrap();
        let cfg = Configuration::load([p]).unwrap();
        (tmp, cfg)
    }

    #[test]
    fn lookup_string_form_uses_shlex() {
        let (_t, c) = cfg_with(
            r#"[alias]
x = 'log -f "{name}"'
"#,
        );
        let argv = lookup(&c, "x").unwrap().unwrap();
        assert_eq!(argv, vec!["log", "-f", "{name}"]);
    }

    #[test]
    fn lookup_array_form_taken_verbatim() {
        let (_t, c) = cfg_with(
            r#"[alias]
x = ["log", "-f", "{name}"]
"#,
        );
        let argv = lookup(&c, "x").unwrap().unwrap();
        assert_eq!(argv, vec!["log", "-f", "{name}"]);
    }

    #[test]
    fn lookup_missing_returns_none() {
        let (_t, c) = cfg_with("");
        assert!(lookup(&c, "absent").unwrap().is_none());
    }

    #[test]
    fn lookup_empty_string_errors() {
        let (_t, c) = cfg_with(
            r#"[alias]
x = ""
"#,
        );
        assert!(matches!(lookup(&c, "x"), Err(AliasError::Empty(_))));
    }

    #[test]
    fn lookup_empty_array_errors() {
        let (_t, c) = cfg_with(
            r#"[alias]
x = []
"#,
        );
        assert!(matches!(lookup(&c, "x"), Err(AliasError::Empty(_))));
    }

    #[test]
    fn lookup_array_with_non_string_errors() {
        let (_t, c) = cfg_with(
            r#"[alias]
x = ["log", 1]
"#,
        );
        assert!(matches!(
            lookup(&c, "x"),
            Err(AliasError::NotAString { .. })
        ));
    }

    #[test]
    fn lookup_bad_type_errors() {
        let (_t, c) = cfg_with(
            r#"[alias]
x = 42
"#,
        );
        assert!(matches!(lookup(&c, "x"), Err(AliasError::BadType { .. })));
    }

    #[test]
    fn lookup_first_token_is_flag_errors_string_form() {
        let (_t, c) = cfg_with(
            r#"[alias]
x = "-vvv config"
"#,
        );
        assert!(matches!(
            lookup(&c, "x"),
            Err(AliasError::FirstTokenIsFlag { .. })
        ));
    }

    #[test]
    fn lookup_first_token_is_flag_errors_array_form() {
        let (_t, c) = cfg_with(
            r#"[alias]
x = ["-vvv", "config"]
"#,
        );
        assert!(matches!(
            lookup(&c, "x"),
            Err(AliasError::FirstTokenIsFlag { .. })
        ));
    }

    #[test]
    fn resolve_single_expansion() {
        let (_t, c) = cfg_with(
            r#"[alias]
mytopdir = "topdir"
"#,
        );
        let argv = vec![OsString::from("west"), OsString::from("mytopdir")];
        let cli = resolve_loop(argv, &c).unwrap();
        assert!(matches!(cli.command, Command::Topdir));
    }

    #[test]
    fn resolve_recursive_aliases() {
        let (_t, c) = cfg_with(
            r#"[alias]
a = "b"
b = "topdir"
"#,
        );
        let argv = vec![OsString::from("west"), OsString::from("a")];
        let cli = resolve_loop(argv, &c).unwrap();
        assert!(matches!(cli.command, Command::Topdir));
    }

    #[test]
    fn resolve_overrides_same_name_without_loop() {
        // Mirrors the `alias.flash = "flash --no-rebuild"` use case from the
        // plan. The expansion's first token equals the alias name; the
        // visited set prevents a second expansion, so the loop terminates
        // and dispatch lands on the (eventual) extension command.
        let (_t, c) = cfg_with(
            r#"[alias]
flash = "flash --no-rebuild"
"#,
        );
        let argv = vec![
            OsString::from("west"),
            OsString::from("flash"),
            OsString::from("x"),
        ];
        let cli = resolve_loop(argv, &c).unwrap();
        match cli.command {
            Command::External(args) => {
                assert_eq!(args[0], "flash");
                assert!(args.iter().any(|a| a == "--no-rebuild"));
                assert!(args.iter().any(|a| a == "x"));
            }
            other => panic!("expected External, got {other:?}"),
        }
    }

    #[test]
    fn resolve_mutual_cycle_terminates() {
        let (_t, c) = cfg_with(
            r#"[alias]
a = "b"
b = "a"
"#,
        );
        let argv = vec![OsString::from("west"), OsString::from("a")];
        // Should terminate (not loop forever) and end up as External("a", ...)
        // or External("b", ...) — either is fine; both are unknown.
        let cli = resolve_loop(argv, &c).unwrap();
        assert!(matches!(cli.command, Command::External(_)));
    }

    #[test]
    fn resolve_no_alias_no_change() {
        let (_t, c) = cfg_with("");
        let argv = vec![OsString::from("west"), OsString::from("topdir")];
        let cli = resolve_loop(argv, &c).unwrap();
        assert!(matches!(cli.command, Command::Topdir));
    }
}
