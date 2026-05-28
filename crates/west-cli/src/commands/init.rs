//! `west init` — bootstrap a new workspace from a manifest URL or register
//! an existing local manifest directory.
//!
//! Argument shape (matches v1, with one explicit-workspace knob added):
//!
//! - The positional `[directory]` is **mode-dependent**:
//!   - Under `-l/--local`: the existing manifest directory (default cwd).
//!     Topdir derives as `manifest_directory.parent()` unless `-t` is set.
//!   - Without `-l`: the workspace target (default cwd). Soft-deprecated
//!     in favour of `-t/--topdir`.
//! - `-t/--topdir <WORKSPACE_DIR>` is the explicit workspace knob; works
//!   in both modes. When given, it wins.
//! - `--manifest-path <SUBPATH>` is **remote-mode only** — the subpath of
//!   the workspace into which the cloned manifest repository lands.
//!   In local mode the positional carries this information; combining
//!   `-l` with `--manifest-path` is rejected.
//! - `--manifest-file <FILE>` is filename-only and applies in both modes.
//!
//! Workspace eligibility uses `west_core::topdir::topdir`, so we reject
//! any candidate that's already inside an existing workspace at any depth.
//! Local mode does **not** require the manifest directory to be a VCS
//! working copy — only that the manifest YAML file exists there.

use std::fs;
use std::io::IsTerminal;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use clap::Args;

use west_core::config::{ConfigValue, Configuration};
use west_core::manifest::Manifest;
use west_core::vcs::{self, RevSpec};

use super::config::LoadedConfig;
use crate::exit;

#[derive(Args, Debug)]
pub struct InitArgs {
    /// Mode-dependent positional:
    ///   - with `-l`: the existing manifest directory (default cwd).
    ///   - without `-l`: the workspace target (default cwd). Soft-deprecated
    ///     in favour of `-t/--topdir`.
    pub directory: Option<PathBuf>,

    /// Initialize from an existing local manifest directory (no clone).
    /// The positional `directory` (default cwd) is the manifest dir;
    /// topdir defaults to its parent unless `-t` is given.
    #[arg(long, short = 'l')]
    pub local: bool,

    /// Manifest URL to clone (bootstrap mode; required when not `-l`).
    /// `-m` / `--manifest-url` are accepted as v1-compatible aliases.
    #[arg(
        long,
        short = 'u',
        visible_alias = "manifest-url",
        visible_short_alias = 'm',
        conflicts_with = "local"
    )]
    pub url: Option<String>,

    /// Revision (branch or tag) to check out (bootstrap mode).
    /// `--mr` is accepted as a short alias.
    #[arg(long = "revision", visible_alias = "mr", conflicts_with = "local")]
    pub revision: Option<String>,

    /// Explicit workspace directory (the parent of the `.west/` to be
    /// created). When set, takes precedence over any directory
    /// derivation. Applies in both modes.
    #[arg(long = "topdir", short = 't')]
    pub topdir: Option<PathBuf>,

    /// Subpath within the workspace where the manifest repository lives
    /// (sets `manifest.path`). Relative to the workspace root (`-t`,
    /// the positional directory, or cwd, in that order). With `-l`:
    /// an alternative to specifying the manifest directory positionally;
    /// if both are given they must resolve to the same location. With
    /// `-u/--url`: defaults to the manifest URL's basename.
    #[arg(long = "manifest-path", visible_alias = "mp")]
    pub manifest_path: Option<PathBuf>,

    /// Manifest YAML filename within the manifest directory.
    /// Equivalent to `--config manifest.file=FILE`. Default: `west.yml`.
    #[arg(long = "manifest-file", visible_alias = "mf")]
    pub manifest_file: Option<PathBuf>,

    /// Extra option to pass through to `git clone` when bootstrapping
    /// the manifest repo (e.g. `-o=--depth=1`). Repeatable. Appends to
    /// `tool.git.clone.extra-args`. Cannot be combined with `-l` (which
    /// doesn't clone).
    #[arg(short = 'o', long = "clone-opt", value_name = "OPT", conflicts_with = "local", action = clap::ArgAction::Append)]
    pub clone_opt: Vec<String>,
}

const DEFAULT_MANIFEST_FILE: &str = "west.yml";

pub fn run(args: InitArgs, loaded: &mut LoadedConfig) -> ExitCode {
    // Splice the dedicated flags into the inline-overrides config layer
    // so the rest of the command reads from a single source. Dedicated
    // flags run after top-level `--config`, so they win on conflict — but
    // top-level `--config manifest.path=…` / `manifest.file=…` still
    // applies when the dedicated flag isn't given.
    if let Some(p) = args.manifest_path.as_deref()
        && let Err(e) = super::config::splice_inline(
            &mut loaded.config,
            "manifest.path",
            ConfigValue::String(p.to_string_lossy().into_owned()),
        )
    {
        log::error!("{e}");
        return exit::usage();
    }
    if let Some(f) = args.manifest_file.as_deref()
        && let Err(e) = super::config::splice_inline(
            &mut loaded.config,
            "manifest.file",
            ConfigValue::String(f.to_string_lossy().into_owned()),
        )
    {
        log::error!("{e}");
        return exit::usage();
    }
    if !args.clone_opt.is_empty() {
        // Append to whatever `tool.git.clone.extra-args` already holds,
        // so the git client (built from this config) picks up the
        // passthrough when it clones the manifest repo.
        let mut combined: Vec<ConfigValue> = match loaded.config.get("tool.git.clone.extra-args") {
            Ok(None) => Vec::new(),
            Ok(Some(ConfigValue::List(items))) => items,
            Ok(Some(other)) => {
                log::error!("tool.git.clone.extra-args must be a list, got {other:?}");
                return exit::usage();
            }
            Err(e) => {
                log::error!("{e}");
                return exit::usage();
            }
        };
        combined.extend(args.clone_opt.iter().cloned().map(ConfigValue::String));
        if let Err(e) = super::config::splice_inline(
            &mut loaded.config,
            "tool.git.clone.extra-args",
            ConfigValue::List(combined),
        ) {
            log::error!("{e}");
            return exit::usage();
        }
    }

    let result = if args.local {
        local(&args, &loaded.config)
    } else {
        match args.url.as_deref() {
            Some(url) => bootstrap(&args, url, &loaded.config),
            None => {
                // No mode chosen. Re-running `init` inside an existing
                // workspace is the common case here, so surface the
                // "already initialized" guard first (matching v1, and
                // every other invocation); only otherwise tell the user
                // to pick a mode.
                resolve_topdir_remote(&args)
                    .and_then(|ws| eligibility_check(&ws))
                    .and_then(|()| {
                        Err(InitError::Generic(
                            "specify --url to clone a manifest, or --local to register an existing manifest directory".into(),
                        ))
                    })
            }
        }
    };

    match result {
        Ok(()) => exit::SUCCESS,
        Err(InitError::AlreadyInitialized(p)) => {
            log::error!("already initialized in {}", p.display());
            exit::FAILURE
        }
        Err(e) => {
            log::error!("{e}");
            exit::FAILURE
        }
    }
}

// =====================================================================
// bootstrap mode
// =====================================================================

fn bootstrap(args: &InitArgs, url: &str, config: &Configuration) -> Result<(), InitError> {
    // Topdir resolution (remote mode):
    //   1. `-t/--topdir` if given.
    //   2. positional `directory` (legacy / soft-deprecated form).
    //   3. cwd.
    // Combining `-t` and the positional is rejected — both attempt to set
    // the same thing, and the user means one or the other.
    if args.topdir.is_some() && args.directory.is_some() {
        return Err(InitError::Generic(
            "cannot combine -t/--topdir with the positional directory; \
             use one or the other"
                .into(),
        ));
    }
    let workspace = resolve_topdir_remote(args)?;
    let revision = args.revision.as_deref();

    fs::create_dir_all(&workspace).map_err(|e| InitError::Io {
        path: workspace.clone(),
        source: e,
    })?;
    eligibility_check(&workspace)?;
    let workspace = workspace.as_path();

    // Manifest-path comes from config (the dedicated `--manifest-path`
    // flag got spliced in `run`, so this picks up either the flag or a
    // top-level `--config manifest.path=…`). Fallback below is YAML
    // self.path or URL basename.
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
        // On a TTY (and unless the user asked for raw output), drive a
        // single indicatif progress bar. Otherwise hand stdio straight
        // to the underlying tool — matches the captured-bytes-or-native
        // policy `update` uses.
        let raw = config
            .get_bool("output.raw")
            .map_err(InitError::Config)?
            .unwrap_or(false);
        if !raw && std::io::stderr().is_terminal() {
            // Attach to the process-wide MultiProgress so any log
            // records (routed through the same instance) suspend the
            // spinner and print above it instead of tearing the frame.
            let pb = crate::progress::multi().add(indicatif::ProgressBar::new_spinner());
            // Show the URL's basename (e.g. `example-application`) rather
            // than a truncated full URL — same logic init uses elsewhere
            // when falling back from the manifest's `self.path`.
            let prefix =
                crate::progress::truncate_prefix(&url_basename(url), crate::progress::PREFIX_WIDTH);
            pb.set_prefix(prefix.clone());
            pb.set_style(crate::progress::spinner_style());
            pb.set_message("cloning…");
            pb.enable_steady_tick(crate::progress::TICK_INTERVAL);
            let mut sink = crate::progress::IndicatifSink::new(pb.clone(), None);
            let mut out = vcs::Output::Stream(&mut sink);
            let spec = vcs::CloneSpec {
                url,
                dest: &tmp_dir,
                revision,
                origin: None,
                kind: vcs::CloneKind::Working,
            };
            let res = vcs.clone(&spec, &mut out);
            // Replace the spinner with the same `✓ <sha> <subject>` /
            // `✗ <msg>` lines `west update` uses on completion, so a
            // bootstrap clone has the same visual shape as a per-project
            // update. The bar is cleared first; the formatted line is
            // emitted via `eprintln` (single bar = no MultiProgress to
            // route through).
            pb.finish_and_clear();
            match &res {
                Ok(()) => match vcs.commit_summary(&tmp_dir, RevSpec::Head) {
                    Ok(summary) => {
                        eprintln!("{}", crate::progress::render_done_line(&prefix, &summary));
                    }
                    Err(_) => {
                        // SHA lookup failed despite a successful clone
                        // (very unlikely — would mean the working tree
                        // has no HEAD). Fall back to a minimal success
                        // line so the user still sees the clone
                        // finished.
                        eprintln!(
                            "{}",
                            crate::progress::render_done_line(
                                &prefix,
                                &vcs::CommitSummary {
                                    short_sha: String::new(),
                                    subject: "cloned".into(),
                                },
                            )
                        );
                    }
                },
                Err(e) => {
                    eprintln!(
                        "{}",
                        crate::progress::render_failed_line(&prefix, &e.to_string())
                    );
                }
            }
            res.map_err(InitError::Vcs)?;
        } else {
            let mut out = vcs::Output::Native;
            let spec = vcs::CloneSpec {
                url,
                dest: &tmp_dir,
                revision,
                origin: None,
                kind: vcs::CloneKind::Working,
            };
            vcs.clone(&spec, &mut out).map_err(InitError::Vcs)?;
        }

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
                        log::warn!(
                            "--manifest-path={} differs from the manifest's self.path ({}); the workspace layout will not match the manifest's documented layout",
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

fn local(args: &InitArgs, config: &Configuration) -> Result<(), InitError> {
    let manifest_file_name = config_manifest_file(config)?;
    let (topdir, manifest_dir) = resolve_local_layout(args, config)?;

    let manifest_yaml = manifest_dir.join(&manifest_file_name);
    if !manifest_yaml.is_file() {
        return Err(InitError::Generic(format!(
            "manifest file {} not found",
            manifest_yaml.display()
        )));
    }

    // Eligibility check after location resolution so we report against the
    // workspace the user actually chose.
    eligibility_check(&topdir)?;

    let manifest_path_rel = manifest_dir.strip_prefix(&topdir).map_err(|_| {
        InitError::Generic(format!(
            "manifest directory {} is not inside workspace {}",
            manifest_dir.display(),
            topdir.display(),
        ))
    })?;

    write_workspace_config(&topdir, manifest_path_rel, &manifest_file_name)?;
    Ok(())
}

/// Resolve `(topdir, manifest_dir)` for local mode. Both come back as
/// absolute paths.
///
/// Inputs (any combination of):
///   - `args.directory` (positional) — manifest dir as a filesystem path
///     (absolute or cwd-relative).
///   - `manifest.path` (from the dedicated `--manifest-path` flag spliced
///     in `run`, or from a top-level `--config manifest.path=…`) — manifest
///     dir as a workspace-relative subpath.
///   - `args.topdir` — explicit workspace root.
///
/// Rules:
///   - Manifest dir = positional > topdir-joined(`manifest.path`)
///     > cwd-joined(`manifest.path`) > cwd.
///   - Topdir = explicit `-t` > manifest_dir.parent.
///   - If both positional and `manifest.path` are given, they must
///     resolve to the same absolute manifest dir (else error).
fn resolve_local_layout(
    args: &InitArgs,
    config: &Configuration,
) -> Result<(PathBuf, PathBuf), InitError> {
    let cwd = std::env::current_dir()
        .map_err(|e| InitError::Generic(format!("cannot get current directory: {e}")))?;

    let topdir_explicit: Option<PathBuf> = args.topdir.as_deref().map(|t| {
        if t.is_absolute() {
            t.to_path_buf()
        } else {
            cwd.join(t)
        }
    });

    let manifest_dir_from_pos: Option<PathBuf> = args.directory.as_deref().map(|d| {
        if d.is_absolute() {
            d.to_path_buf()
        } else {
            cwd.join(d)
        }
    });

    // `manifest.path` (either dedicated flag or top-level --config) is
    // workspace-relative. When `-t` is given we root it at the explicit
    // topdir; otherwise we root at cwd (the same place `-l <DIR>` would
    // have rooted a relative positional).
    let manifest_dir_from_mp: Option<PathBuf> = config_manifest_path(config)?.map(|mp| {
        let base = topdir_explicit.as_deref().unwrap_or(&cwd);
        base.join(mp)
    });

    // If both supplied, require they resolve to the same absolute path.
    if let (Some(p), Some(m)) = (
        manifest_dir_from_pos.as_deref(),
        manifest_dir_from_mp.as_deref(),
    ) && canonicalize_for_compare(p) != canonicalize_for_compare(m)
    {
        return Err(InitError::Generic(format!(
            "-l positional ({}) and manifest.path ({}) disagree about the manifest location",
            p.display(),
            m.display(),
        )));
    }

    let manifest_dir = manifest_dir_from_pos
        .or(manifest_dir_from_mp)
        .unwrap_or_else(|| cwd.clone());

    // Canonicalize manifest_dir if it exists on disk; otherwise leave as-is
    // and let the "manifest file not found" check downstream report a clean
    // error.
    let manifest_dir = canonicalize_or_keep(&manifest_dir);

    let topdir = match topdir_explicit {
        Some(t) => canonicalize_or_keep(&t),
        None => manifest_dir
            .parent()
            .ok_or_else(|| {
                InitError::Generic(format!(
                    "manifest directory {} has no parent; use -t/--topdir to specify the workspace",
                    manifest_dir.display(),
                ))
            })?
            .to_path_buf(),
    };

    if !manifest_dir.starts_with(&topdir) {
        return Err(InitError::Generic(format!(
            "manifest directory {} is not inside workspace {}",
            manifest_dir.display(),
            topdir.display(),
        )));
    }

    Ok((topdir, manifest_dir))
}

/// Resolve the workspace target in remote (clone) mode. Caller has already
/// validated `-t` and the positional aren't both set.
fn resolve_topdir_remote(args: &InitArgs) -> Result<PathBuf, InitError> {
    let cwd = std::env::current_dir()
        .map_err(|e| InitError::Generic(format!("cannot get current directory: {e}")))?;
    let raw = args
        .topdir
        .as_deref()
        .or(args.directory.as_deref())
        .map(|p| {
            if p.is_absolute() {
                p.to_path_buf()
            } else {
                cwd.join(p)
            }
        })
        .unwrap_or(cwd);
    Ok(canonicalize_or_keep(&raw))
}

fn canonicalize_or_keep(p: &Path) -> PathBuf {
    p.canonicalize().unwrap_or_else(|_| p.to_path_buf())
}

/// Used only for equality comparison in the local-mode validation. We
/// don't canonicalize through filesystem if either side doesn't exist
/// yet — the comparison falls back to the lexical absolute form.
fn canonicalize_for_compare(p: &Path) -> PathBuf {
    canonicalize_or_keep(p)
}

// =====================================================================
// helpers
// =====================================================================

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

#[derive(Debug, thiserror::Error)]
enum InitError {
    #[error("directory is already inside a west workspace ({})", .0.display())]
    AlreadyInitialized(PathBuf),
    #[error("{0}")]
    Generic(String),
    #[error("io error on {}: {source}", path.display())]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error(transparent)]
    Config(#[from] west_core::config::ConfigError),
    #[error(transparent)]
    Manifest(#[from] west_core::manifest::ManifestError),
    #[error(transparent)]
    Vcs(#[from] west_core::vcs::VcsError),
}

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
