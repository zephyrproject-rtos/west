//! Resolve west's conventional config file paths into a layer stack.
//!
//! `Configuration` itself is level-agnostic. This module is the *only* place
//! that encodes "where west keeps config" (system / global / conf.d / local).

use std::env;
use std::path::{Path, PathBuf};

use crate::WEST_DIR;

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ResolvedConfig {
    pub system: Option<PathBuf>,
    pub global: Option<PathBuf>,
    pub global_confd: Vec<PathBuf>,
    pub local: Option<PathBuf>,
}

impl ResolvedConfig {
    /// Layer paths in low-to-high precedence order, ready for
    /// [`crate::config::Configuration::load`].
    pub fn layer_paths(&self) -> Vec<PathBuf> {
        let mut out = Vec::new();
        if let Some(p) = &self.system {
            out.push(p.clone());
        }
        if let Some(p) = &self.global {
            out.push(p.clone());
        }
        out.extend(self.global_confd.iter().cloned());
        if let Some(p) = &self.local {
            out.push(p.clone());
        }
        out
    }
}

pub fn resolve(topdir: Option<&Path>) -> ResolvedConfig {
    let global_base = xdg_config_home().map(|d| d.join("west"));

    ResolvedConfig {
        system: env_path("WEST_CONFIG_SYSTEM").or_else(system_default),
        global: env_path("WEST_CONFIG_GLOBAL")
            .or_else(|| global_base.as_ref().map(|d| d.join("config.toml"))),
        global_confd: global_base
            .as_ref()
            .map(|d| confd_entries(&d.join("conf.d")))
            .unwrap_or_default(),
        local: env_path("WEST_CONFIG_LOCAL")
            .or_else(|| topdir.map(|t| t.join(WEST_DIR).join("config.toml"))),
    }
}

fn env_path(var: &str) -> Option<PathBuf> {
    env::var_os(var)
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
}

/// Cross-platform XDG-style config home: `$XDG_CONFIG_HOME` if set and
/// non-empty, otherwise `$HOME/.config`. Applies uniformly on Linux, macOS,
/// and Windows — we deliberately ignore the Apple `Application Support` and
/// Windows `%APPDATA%` conventions in favour of a single `~/.config/west`
/// layout.
fn xdg_config_home() -> Option<PathBuf> {
    if let Some(p) = env_path("XDG_CONFIG_HOME") {
        return Some(p);
    }
    dirs::home_dir().map(|h| h.join(".config"))
}

#[cfg(any(
    target_os = "linux",
    target_os = "freebsd",
    target_os = "netbsd",
    target_os = "openbsd",
    target_os = "dragonfly",
))]
fn system_default() -> Option<PathBuf> {
    Some(PathBuf::from("/etc/west/config.toml"))
}

#[cfg(target_os = "macos")]
fn system_default() -> Option<PathBuf> {
    Some(PathBuf::from("/usr/local/etc/west/config.toml"))
}

#[cfg(target_os = "windows")]
fn system_default() -> Option<PathBuf> {
    env::var_os("PROGRAMDATA").map(|p| PathBuf::from(p).join("west").join("config.toml"))
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
fn system_default() -> Option<PathBuf> {
    None
}

fn confd_entries(dir: &Path) -> Vec<PathBuf> {
    let entries = match std::fs::read_dir(dir) {
        Ok(e) => e,
        Err(_) => return Vec::new(),
    };
    let mut paths: Vec<PathBuf> = entries
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.is_file() && p.extension().is_some_and(|ext| ext == "toml"))
        .collect();
    paths.sort();
    paths
}

#[cfg(test)]
mod tests {
    use super::*;
    use serial_test::serial;
    use std::fs;
    use tempfile::TempDir;

    fn clear_env() {
        for var in [
            "WEST_CONFIG_SYSTEM",
            "WEST_CONFIG_GLOBAL",
            "WEST_CONFIG_LOCAL",
            "XDG_CONFIG_HOME",
        ] {
            // SAFETY: tests are serialized via `#[serial]`.
            unsafe { env::remove_var(var) };
        }
    }

    #[test]
    #[serial]
    fn env_var_overrides_per_level() {
        clear_env();
        let tmp = TempDir::new().unwrap();
        let s = tmp.path().join("sys.toml");
        let g = tmp.path().join("glob.toml");
        let l = tmp.path().join("loc.toml");

        // SAFETY: tests are serialized via `#[serial]`.
        unsafe {
            env::set_var("WEST_CONFIG_SYSTEM", &s);
            env::set_var("WEST_CONFIG_GLOBAL", &g);
            env::set_var("WEST_CONFIG_LOCAL", &l);
        }
        let r = resolve(None);
        clear_env();

        assert_eq!(r.system.as_deref(), Some(s.as_path()));
        assert_eq!(r.global.as_deref(), Some(g.as_path()));
        assert_eq!(r.local.as_deref(), Some(l.as_path()));
    }

    #[test]
    #[serial]
    fn local_default_uses_topdir() {
        clear_env();
        let tmp = TempDir::new().unwrap();
        let r = resolve(Some(tmp.path()));
        assert_eq!(r.local, Some(tmp.path().join(WEST_DIR).join("config.toml")));
    }

    #[test]
    #[serial]
    fn local_none_when_topdir_none_and_env_unset() {
        clear_env();
        let r = resolve(None);
        assert!(r.local.is_none());
    }

    #[test]
    fn confd_lists_sorted_toml_files() {
        let tmp = TempDir::new().unwrap();
        fs::write(tmp.path().join("20-second.toml"), "").unwrap();
        fs::write(tmp.path().join("10-first.toml"), "").unwrap();
        fs::write(tmp.path().join("README.md"), "").unwrap();

        let entries = confd_entries(tmp.path());
        assert_eq!(
            entries,
            vec![
                tmp.path().join("10-first.toml"),
                tmp.path().join("20-second.toml"),
            ]
        );
    }

    #[test]
    fn confd_missing_dir_yields_empty_list() {
        let tmp = TempDir::new().unwrap();
        assert!(confd_entries(&tmp.path().join("does-not-exist")).is_empty());
    }

    #[test]
    fn layer_paths_order_is_system_global_confd_local() {
        let r = ResolvedConfig {
            system: Some(PathBuf::from("/sys.toml")),
            global: Some(PathBuf::from("/glob.toml")),
            global_confd: vec![
                PathBuf::from("/conf.d/10.toml"),
                PathBuf::from("/conf.d/20.toml"),
            ],
            local: Some(PathBuf::from("/loc.toml")),
        };
        assert_eq!(
            r.layer_paths(),
            vec![
                PathBuf::from("/sys.toml"),
                PathBuf::from("/glob.toml"),
                PathBuf::from("/conf.d/10.toml"),
                PathBuf::from("/conf.d/20.toml"),
                PathBuf::from("/loc.toml"),
            ]
        );
    }

    #[test]
    fn layer_paths_skips_missing_optional_components() {
        let r = ResolvedConfig::default();
        assert!(r.layer_paths().is_empty());
    }

    #[test]
    #[cfg(any(
        target_os = "linux",
        target_os = "freebsd",
        target_os = "netbsd",
        target_os = "openbsd",
        target_os = "dragonfly",
    ))]
    fn system_default_unix() {
        assert_eq!(
            system_default(),
            Some(PathBuf::from("/etc/west/config.toml"))
        );
    }

    #[test]
    #[cfg(target_os = "macos")]
    fn system_default_macos() {
        assert_eq!(
            system_default(),
            Some(PathBuf::from("/usr/local/etc/west/config.toml"))
        );
    }

    #[test]
    #[serial]
    fn xdg_config_home_uses_env_when_set() {
        clear_env();
        let tmp = TempDir::new().unwrap();
        // SAFETY: tests are serialized via `#[serial]`.
        unsafe { env::set_var("XDG_CONFIG_HOME", tmp.path()) };
        let got = xdg_config_home();
        clear_env();
        assert_eq!(got.as_deref(), Some(tmp.path()));
    }

    #[test]
    #[serial]
    fn xdg_config_home_falls_back_to_home_when_empty() {
        clear_env();
        // SAFETY: tests are serialized via `#[serial]`.
        unsafe { env::set_var("XDG_CONFIG_HOME", "") };
        let got = xdg_config_home();
        clear_env();
        let expected = dirs::home_dir().map(|h| h.join(".config"));
        assert_eq!(got, expected);
    }

    #[test]
    #[serial]
    fn xdg_config_home_falls_back_to_home_when_unset() {
        clear_env();
        let got = xdg_config_home();
        let expected = dirs::home_dir().map(|h| h.join(".config"));
        assert_eq!(got, expected);
    }

    #[test]
    #[serial]
    fn resolve_global_default_uses_xdg() {
        clear_env();
        let tmp = TempDir::new().unwrap();
        // SAFETY: tests are serialized via `#[serial]`.
        unsafe { env::set_var("XDG_CONFIG_HOME", tmp.path()) };
        let r = resolve(None);
        clear_env();
        assert_eq!(
            r.global,
            Some(tmp.path().join("west").join("config.toml"))
        );
    }
}
