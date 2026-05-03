use std::path::PathBuf;
use std::process::ExitCode;

use clap::{Args, Subcommand};

use west_core::config::Configuration;
use west_core::config_paths::{ResolvedConfig, resolve};

pub mod get;
pub mod list;
pub mod set;
pub mod unset;

#[derive(Args, Debug)]
pub struct ConfigArgs {
    #[command(subcommand)]
    pub action: Action,
}

#[derive(Subcommand, Debug)]
pub enum Action {
    /// Get a configuration value.
    Get(get::GetArgs),
    /// Set a configuration value.
    Set(set::SetArgs),
    /// Remove a configuration value.
    Unset(unset::UnsetArgs),
    /// List all configuration values.
    List(list::ListArgs),
}

pub fn run(args: ConfigArgs) -> ExitCode {
    match args.action {
        Action::Get(a) => get::run(a),
        Action::Set(a) => set::run(a),
        Action::Unset(a) => unset::run(a),
        Action::List(a) => list::run(a),
    }
}

#[derive(Args, Debug, Default)]
#[group(id = "scope", multiple = false)]
pub struct ScopeArgs {
    /// Use the system-wide config file.
    #[arg(long, group = "scope")]
    pub system: bool,
    /// Use the per-user (global) config file.
    #[arg(long, group = "scope")]
    pub global: bool,
    /// Use the workspace-local config file (requires a workspace).
    #[arg(long, group = "scope")]
    pub local: bool,
    /// Use the named config file directly.
    #[arg(long, value_name = "PATH", group = "scope")]
    pub file: Option<PathBuf>,
}

impl ScopeArgs {
    pub fn is_set(&self) -> bool {
        self.system || self.global || self.local || self.file.is_some()
    }
}

pub struct LoadedConfig {
    pub resolved: ResolvedConfig,
    pub config: Configuration,
}

/// Discover the workspace topdir (best-effort), resolve the conventional layer
/// stack, and load it into a `Configuration`. `extra` paths (e.g. from `--file`)
/// are appended at the highest precedence position if not already present.
pub fn load(extra: &[PathBuf]) -> Result<LoadedConfig, String> {
    let cwd = std::env::current_dir()
        .map_err(|e| format!("cannot get current directory: {e}"))?;
    let topdir = west_core::topdir::topdir(&cwd).ok();
    let resolved = resolve(topdir.as_deref());
    let mut paths = resolved.layer_paths();
    for e in extra {
        if !paths.contains(e) {
            paths.push(e.clone());
        }
    }
    let config = Configuration::load(paths).map_err(|e| format!("{e}"))?;
    Ok(LoadedConfig { resolved, config })
}

/// Resolve a `ScopeArgs` to a single layer path. Returns `Ok(None)` when no
/// scope flag was supplied (caller decides default).
pub fn scope_to_path(
    s: &ScopeArgs,
    r: &ResolvedConfig,
) -> Result<Option<PathBuf>, String> {
    if let Some(p) = &s.file {
        return Ok(Some(p.clone()));
    }
    if s.system {
        return r
            .system
            .clone()
            .map(Some)
            .ok_or_else(|| "--system: no system config file is configured".to_owned());
    }
    if s.global {
        return r
            .global
            .clone()
            .map(Some)
            .ok_or_else(|| "--global: no global config file is configured".to_owned());
    }
    if s.local {
        return r.local.clone().map(Some).ok_or_else(|| {
            "--local: not in a workspace; use --file or run inside one".to_owned()
        });
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rc() -> ResolvedConfig {
        ResolvedConfig {
            system: Some(PathBuf::from("/sys.toml")),
            global: Some(PathBuf::from("/glob.toml")),
            global_confd: vec![],
            local: Some(PathBuf::from("/loc.toml")),
        }
    }

    #[test]
    fn scope_to_path_resolves_each_flag() {
        let r = rc();
        let s = ScopeArgs {
            system: true,
            ..Default::default()
        };
        assert_eq!(
            scope_to_path(&s, &r).unwrap(),
            Some(PathBuf::from("/sys.toml"))
        );
        let s = ScopeArgs {
            global: true,
            ..Default::default()
        };
        assert_eq!(
            scope_to_path(&s, &r).unwrap(),
            Some(PathBuf::from("/glob.toml"))
        );
        let s = ScopeArgs {
            local: true,
            ..Default::default()
        };
        assert_eq!(
            scope_to_path(&s, &r).unwrap(),
            Some(PathBuf::from("/loc.toml"))
        );
        let s = ScopeArgs {
            file: Some(PathBuf::from("/x.toml")),
            ..Default::default()
        };
        assert_eq!(
            scope_to_path(&s, &r).unwrap(),
            Some(PathBuf::from("/x.toml"))
        );
    }

    #[test]
    fn scope_to_path_no_flag_returns_none() {
        let r = rc();
        let s = ScopeArgs::default();
        assert_eq!(scope_to_path(&s, &r).unwrap(), None);
    }

    #[test]
    fn scope_to_path_local_without_workspace_errors() {
        let mut r = rc();
        r.local = None;
        let s = ScopeArgs {
            local: true,
            ..Default::default()
        };
        let err = scope_to_path(&s, &r).unwrap_err();
        assert!(err.contains("--local"));
        assert!(err.contains("workspace"));
    }
}
