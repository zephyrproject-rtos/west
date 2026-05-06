//! `west init` — bootstrap a new workspace from a manifest URL or register
//! an existing local manifest directory.
//!
//! See the plan / `init --help` for the full UX. Key design points:
//!
//! - The positional `[DIR]` is **always the workspace** (decoupled from the
//!   manifest directory).
//! - `--manifest-path` and `--manifest-file` are sugar for
//!   `--config manifest.path=...` / `--config manifest.file=...`. They're
//!   spliced into the loaded config's inline overrides at the top of `run`,
//!   then read back via `Configuration::get_str`.
//! - Workspace eligibility uses `west_core::topdir::topdir`, so we reject
//!   any candidate that's already inside an existing workspace at any depth.
//! - Local mode does **not** require the manifest path to be a VCS working
//!   copy — only that the manifest YAML file exists there.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use clap::Args;

use west_core::config::{ConfigValue, Configuration};
use west_core::manifest::Manifest;
use west_core::vcs;

use super::config::LoadedConfig;

#[derive(Args, Debug)]
pub struct InitArgs {
    /// Workspace directory (default: current working directory).
    pub directory: Option<PathBuf>,

    /// Initialize from an existing local manifest directory (no clone).
    /// Requires `--manifest-path` (or `--config manifest.path=...`).
    #[arg(long, short = 'l')]
    pub local: bool,

    /// Manifest URL to clone (bootstrap mode; required).
    #[arg(long, short = 'u', conflicts_with = "local")]
    pub url: Option<String>,

    /// Revision (branch or tag) to check out (bootstrap mode).
    /// `--mr` is accepted as an alias for muscle-memory carry-over from
    /// Python west.
    #[arg(long = "revision", visible_alias = "mr")]
    pub revision: Option<String>,

    /// Manifest directory relative to the workspace.
    /// Equivalent to `--config manifest.path=PATH`.
    #[arg(long = "manifest-path", visible_alias = "mp")]
    pub manifest_path: Option<PathBuf>,

    /// Manifest YAML filename within the manifest directory.
    /// Equivalent to `--config manifest.file=FILE`. Default: `west.yml`.
    #[arg(long = "manifest-file", visible_alias = "mf")]
    pub manifest_file: Option<PathBuf>,
}

const DEFAULT_MANIFEST_FILE: &str = "west.yml";

pub fn run(args: InitArgs, loaded: &mut LoadedConfig) -> ExitCode {
    // Splice dedicated flags into inline config overrides so the rest of the
    // command reads from a single source. Dedicated flags run after the
    // top-level `--config`, so they win on conflict.
    if let Some(p) = args.manifest_path.as_deref() {
        if let Err(e) = loaded.config.set_inline(
            "manifest.path",
            ConfigValue::String(p.to_string_lossy().into_owned()),
        ) {
            eprintln!("west: {e}");
            return ExitCode::from(2);
        }
    }
    if let Some(f) = args.manifest_file.as_deref() {
        if let Err(e) = loaded.config.set_inline(
            "manifest.file",
            ConfigValue::String(f.to_string_lossy().into_owned()),
        ) {
            eprintln!("west: {e}");
            return ExitCode::from(2);
        }
    }

    // Workspace dir.
    let workspace = match resolve_workspace_dir(args.directory.as_deref()) {
        Ok(p) => p,
        Err(msg) => {
            eprintln!("west: {msg}");
            return ExitCode::FAILURE;
        }
    };

    let result = if args.local {
        local(&workspace, &loaded.config)
    } else {
        match args.url.as_deref() {
            Some(url) => bootstrap(&workspace, url, args.revision.as_deref(), &loaded.config),
            None => Err(InitError::Generic(
                "specify --url to clone a manifest, or --local to register an existing manifest directory".into(),
            )),
        }
    };

    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(InitError::AlreadyInitialized(p)) => {
            eprintln!(
                "west: directory {} is already inside a west workspace ({})",
                p.display(),
                p.display(),
            );
            ExitCode::FAILURE
        }
        Err(e) => {
            eprintln!("west: {e}");
            ExitCode::FAILURE
        }
    }
}

// =====================================================================
// bootstrap mode
// =====================================================================

fn bootstrap(
    workspace: &Path,
    url: &str,
    revision: Option<&str>,
    config: &Configuration,
) -> Result<(), InitError> {
    fs::create_dir_all(workspace).map_err(|e| InitError::Io {
        path: workspace.to_owned(),
        source: e,
    })?;
    eligibility_check(workspace)?;

    // Resolve config-driven options up front.
    let user_manifest_path = config_manifest_path(config)?;
    let manifest_file_name = config_manifest_file(config)?;

    // Create .west/ now so we have somewhere safe to put the bootstrap tempdir.
    // If anything fails before we finalize, we remove `.west/` again.
    let west_dir = workspace.join(".west");
    fs::create_dir(&west_dir).map_err(|e| InitError::Io {
        path: west_dir.clone(),
        source: e,
    })?;

    // Use a deterministic tempdir inside `.west/`. We clean it up explicitly
    // on every exit path. (No `tempfile` dep here; manual cleanup is enough
    // since panics are not a normal init failure mode.)
    let tmp_dir = west_dir.join(format!(".bootstrap-tmp.{}", std::process::id()));
    fs::create_dir(&tmp_dir).map_err(|e| InitError::Io {
        path: tmp_dir.clone(),
        source: e,
    })?;

    let body = || -> Result<PathBuf, InitError> {
        let vcs = vcs::from_config(config).map_err(InitError::Vcs)?;
        vcs.clone(url, &tmp_dir, revision, None)
            .map_err(InitError::Vcs)?;

        // Resolve manifest.path:
        //   - explicit: use it; warn if it disagrees with the YAML's self.path.
        //   - implicit: use the YAML's self.path; fall back to URL basename.
        let manifest_yaml = tmp_dir.join(&manifest_file_name);
        if !manifest_yaml.exists() {
            return Err(InitError::Generic(format!(
                "manifest file {} not found in cloned repo",
                manifest_yaml.display()
            )));
        }

        // Use the lenient probe: at bootstrap we only need `self.path`. The
        // strict loader rejects `import:` directives (e.g. zephyr's example
        // application), but those don't matter until later commands consume
        // the manifest — we shouldn't block init on them.
        let yaml_self_path =
            Manifest::peek_self_path(&manifest_yaml).map_err(InitError::Manifest)?;

        let manifest_path = match user_manifest_path.clone() {
            Some(user) => {
                if let Some(yaml) = &yaml_self_path {
                    if yaml != &user && yaml != Path::new("manifest") {
                        eprintln!(
                            "west: warning: --manifest-path={} differs from the manifest's self.path ({}); the workspace layout will not match the manifest's documented layout",
                            user.display(),
                            yaml.display(),
                        );
                    }
                }
                user
            }
            None => match yaml_self_path {
                Some(p) if p != Path::new("manifest") => p,
                _ => PathBuf::from(url_basename(url)),
            },
        };

        // Move tempdir → <workspace>/<manifest.path>.
        let dest = workspace.join(&manifest_path);
        if dest.exists() {
            return Err(InitError::Generic(format!(
                "target directory {} already exists",
                dest.display()
            )));
        }
        if let Some(parent) = dest.parent() {
            fs::create_dir_all(parent).map_err(|e| InitError::Io {
                path: parent.to_owned(),
                source: e,
            })?;
        }
        fs::rename(&tmp_dir, &dest).map_err(|e| InitError::Io {
            path: dest.clone(),
            source: e,
        })?;

        Ok(manifest_path)
    };

    let manifest_path = match body() {
        Ok(p) => p,
        Err(e) => {
            // Clean up the tempdir and the freshly-created `.west/` so the
            // user can retry without manually undoing init's partial work.
            let _ = fs::remove_dir_all(&tmp_dir);
            let _ = fs::remove_dir_all(&west_dir);
            return Err(e);
        }
    };

    write_workspace_config(workspace, &manifest_path, &manifest_file_name)?;
    Ok(())
}

// =====================================================================
// local mode
// =====================================================================

fn local(workspace: &Path, config: &Configuration) -> Result<(), InitError> {
    if !workspace.exists() {
        return Err(InitError::Generic(format!(
            "workspace {} does not exist",
            workspace.display()
        )));
    }
    eligibility_check(workspace)?;

    let manifest_path = config_manifest_path(config)?.ok_or_else(|| {
        InitError::Generic(
            "--local requires --manifest-path (or --config manifest.path=...)".into(),
        )
    })?;
    let manifest_file_name = config_manifest_file(config)?;

    let manifest_yaml = workspace.join(&manifest_path).join(&manifest_file_name);
    if !manifest_yaml.exists() {
        return Err(InitError::Generic(format!(
            "manifest file {} not found",
            manifest_yaml.display()
        )));
    }

    write_workspace_config(workspace, &manifest_path, &manifest_file_name)?;
    Ok(())
}

// =====================================================================
// helpers
// =====================================================================

fn resolve_workspace_dir(positional: Option<&Path>) -> Result<PathBuf, String> {
    let p = match positional {
        Some(p) => p.to_path_buf(),
        None => {
            std::env::current_dir().map_err(|e| format!("cannot get current directory: {e}"))?
        }
    };
    Ok(p)
}

/// Errors out if `workspace` is in or under an existing west workspace.
fn eligibility_check(workspace: &Path) -> Result<(), InitError> {
    if let Ok(found) = west_core::topdir::topdir(workspace) {
        return Err(InitError::AlreadyInitialized(found));
    }
    Ok(())
}

fn config_manifest_path(config: &Configuration) -> Result<Option<PathBuf>, InitError> {
    config
        .get_str("manifest.path")
        .map_err(InitError::Config)
        .map(|opt| opt.map(PathBuf::from))
}

fn config_manifest_file(config: &Configuration) -> Result<PathBuf, InitError> {
    Ok(config
        .get_str("manifest.file")
        .map_err(InitError::Config)?
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(DEFAULT_MANIFEST_FILE)))
}

fn write_workspace_config(
    workspace: &Path,
    manifest_path: &Path,
    manifest_file: &Path,
) -> Result<(), InitError> {
    let west_dir = workspace.join(".west");
    fs::create_dir_all(&west_dir).map_err(|e| InitError::Io {
        path: west_dir.clone(),
        source: e,
    })?;
    let local_path = west_dir.join("config.toml");
    let mut config = Configuration::load([local_path.clone()]).map_err(InitError::Config)?;
    config
        .set(
            "manifest.path",
            ConfigValue::String(manifest_path.to_string_lossy().into_owned()),
            &local_path,
        )
        .map_err(InitError::Config)?;
    config
        .set(
            "manifest.file",
            ConfigValue::String(manifest_file.to_string_lossy().into_owned()),
            &local_path,
        )
        .map_err(InitError::Config)?;
    Ok(())
}

/// Strip a trailing `.git` and any path / scheme noise to get a sensible
/// fallback manifest dir name from a URL.
fn url_basename(url: &str) -> String {
    // Take the last path segment.
    let last = url
        .rsplit(['/', '\\', ':'])
        .find(|s| !s.is_empty())
        .unwrap_or(url);
    let trimmed = last.strip_suffix(".git").unwrap_or(last);
    if trimmed.is_empty() {
        "manifest".to_owned()
    } else {
        trimmed.to_owned()
    }
}

// =====================================================================
// errors
// =====================================================================

#[derive(Debug)]
enum InitError {
    AlreadyInitialized(PathBuf),
    Generic(String),
    Io {
        path: PathBuf,
        source: std::io::Error,
    },
    Config(west_core::config::ConfigError),
    Manifest(west_core::manifest::ManifestError),
    Vcs(west_core::vcs::VcsError),
}

impl std::fmt::Display for InitError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            InitError::AlreadyInitialized(p) => write!(
                f,
                "directory is already inside a west workspace ({})",
                p.display()
            ),
            InitError::Generic(s) => f.write_str(s),
            InitError::Io { path, source } => write!(f, "io error on {}: {source}", path.display()),
            InitError::Config(e) => std::fmt::Display::fmt(e, f),
            InitError::Manifest(e) => std::fmt::Display::fmt(e, f),
            InitError::Vcs(e) => std::fmt::Display::fmt(e, f),
        }
    }
}

impl std::error::Error for InitError {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn url_basename_strips_git_suffix_and_path() {
        assert_eq!(url_basename("https://x/y/repo.git"), "repo");
        assert_eq!(url_basename("https://x/y/repo"), "repo");
        assert_eq!(url_basename("/abs/path/manifest.git"), "manifest");
        assert_eq!(url_basename("git@host:user/proj.git"), "proj");
        assert_eq!(url_basename("trailing/"), "trailing");
        assert_eq!(url_basename(""), "manifest");
    }
}
