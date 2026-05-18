//! `west manifest` — inspect / dump / freeze / validate the workspace's
//! manifest. Four mutually-exclusive action flags via `--path`,
//! `--validate`, `--resolve`, `--freeze` (clap's `ArgGroup`-driven
//! required exclusivity). `--untracked` is intentionally deferred to
//! a follow-up commit: full-workspace traversal opens scope questions
//! (walkdir dep choice, performance for large workspaces, what counts
//! as "owned") that don't share much with the other four modes.
//!
//! `--resolve` / `--freeze` emit the canonical resolved manifest (post
//! import-expansion). The output format is chosen by `--format` when
//! given; otherwise it follows the source manifest's own extension
//! (`*.yml`/`*.yaml` → YAML, `*.toml` → TOML, `*.json` → JSON, with
//! YAML as the catch-all default for the conventional `west.yml` name).
//!
//! Workspace + manifest loading mirrors `list` / `forall` / `extension`:
//! `ReadOnlyImportSource` resolves filesystem imports cleanly; per-
//! project imports for uncloned projects are skipped silently here
//! (the resolved output reflects only what's currently visible).

use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::Mutex;

use clap::{ArgGroup, Args, ValueEnum};

use west_core::config::Configuration;
use west_core::manifest::{ImportSource, ImportSourceError, Manifest, Project};
use west_core::vcs::{self, Vcs};

use super::config::LoadedConfig;

const DEFAULT_MANIFEST_FILE: &str = "west.yml";

#[derive(Args, Debug)]
#[command(group = ArgGroup::new("action")
    .required(true)
    .multiple(false)
    .args(["path", "validate", "resolve", "freeze"]))]
pub struct ManifestArgs {
    /// Print the absolute path of the active manifest file.
    #[arg(long)]
    pub path: bool,

    /// Parse the manifest and report success / failure. Exit 0 on
    /// success.
    #[arg(long)]
    pub validate: bool,

    /// Emit the resolved manifest (imports applied) to stdout.
    #[arg(long)]
    pub resolve: bool,

    /// Like `--resolve`, with each project's `revision` replaced by
    /// the SHA its working tree currently points at. Uncloned projects
    /// are an error — run `west update` first.
    #[arg(long)]
    pub freeze: bool,

    /// Output format for `--resolve` / `--freeze`. Defaults to the
    /// source manifest's own format (yaml / toml / json by extension;
    /// yaml otherwise).
    #[arg(long, value_name = "FMT", value_enum)]
    pub format: Option<Format>,

    /// Write output to PATH instead of stdout. Applies to `--resolve` /
    /// `--freeze`.
    #[arg(long, value_name = "PATH")]
    pub out: Option<PathBuf>,
}

#[derive(Copy, Clone, Debug, ValueEnum)]
pub enum Format {
    Yaml,
    Toml,
    Json,
}

#[derive(Debug, thiserror::Error)]
pub enum ManifestCmdError {
    #[error("not inside a west workspace (no .west/ found)")]
    NotInWorkspace,
    #[error("{0}")]
    Config(String),
    #[error("{0}")]
    Manifest(String),
    #[error("{0}")]
    Vcs(String),
    #[error("project {name:?} is not cloned at {}; run 'west update' first",
            path.display())]
    UncloneProject { name: String, path: PathBuf },
    #[error("yaml serialize: {0}")]
    YamlSer(#[source] serde_yaml_ng::Error),
    #[error("toml serialize: {0}")]
    TomlSer(#[source] toml_edit::ser::Error),
    #[error("json serialize: {0}")]
    JsonSer(#[source] serde_json::Error),
    #[error("write {}: {source}", path.display())]
    Io {
        path: PathBuf,
        #[source]
        source: io::Error,
    },
}

pub fn run(args: ManifestArgs, loaded: &mut LoadedConfig) -> ExitCode {
    match run_inner(args, loaded) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("west: {e}");
            ExitCode::FAILURE
        }
    }
}

fn run_inner(args: ManifestArgs, loaded: &mut LoadedConfig) -> Result<(), ManifestCmdError> {
    if args.path {
        return action_path(loaded);
    }
    if args.validate {
        return action_validate(loaded);
    }
    if args.resolve {
        return action_resolve(args, loaded);
    }
    if args.freeze {
        return action_freeze(args, loaded);
    }
    // ArgGroup(required=true) makes this unreachable in practice.
    unreachable!("clap ArgGroup ensures exactly one action is set");
}

// ----- Actions -------------------------------------------------------------

fn action_path(loaded: &LoadedConfig) -> Result<(), ManifestCmdError> {
    let workspace = resolve_workspace_dir()?;
    let (_root, full) = manifest_paths(&workspace, &loaded.config)?;
    println!("{}", full.display());
    Ok(())
}

fn action_validate(loaded: &LoadedConfig) -> Result<(), ManifestCmdError> {
    // load_manifest already does the full parse + import resolution;
    // any failure surfaces with a clear message.
    let workspace = resolve_workspace_dir()?;
    let vcs = vcs::from_config(&loaded.config).map_err(|e| ManifestCmdError::Vcs(e.to_string()))?;
    let source = ReadOnlyImportSource {
        workspace: workspace.as_path(),
        vcs: vcs.as_ref(),
        skipped: Mutex::new(Vec::new()),
    };
    let _ = load_manifest(&workspace, &loaded.config, &source)?;
    println!("manifest is valid");
    Ok(())
}

fn action_resolve(args: ManifestArgs, loaded: &LoadedConfig) -> Result<(), ManifestCmdError> {
    let workspace = resolve_workspace_dir()?;
    let vcs = vcs::from_config(&loaded.config).map_err(|e| ManifestCmdError::Vcs(e.to_string()))?;
    let source = ReadOnlyImportSource {
        workspace: workspace.as_path(),
        vcs: vcs.as_ref(),
        skipped: Mutex::new(Vec::new()),
    };
    let manifest = load_manifest(&workspace, &loaded.config, &source)?;
    let (_root, full) = manifest_paths(&workspace, &loaded.config)?;

    let format = select_format(args.format, &full);
    let value = manifest.to_value();
    let body = serialize(&value, format)?;
    write_output(args.out.as_deref(), &body)
}

fn action_freeze(args: ManifestArgs, loaded: &LoadedConfig) -> Result<(), ManifestCmdError> {
    let workspace = resolve_workspace_dir()?;
    let vcs = vcs::from_config(&loaded.config).map_err(|e| ManifestCmdError::Vcs(e.to_string()))?;
    let source = ReadOnlyImportSource {
        workspace: workspace.as_path(),
        vcs: vcs.as_ref(),
        skipped: Mutex::new(Vec::new()),
    };
    let manifest = load_manifest(&workspace, &loaded.config, &source)?;
    let (_root, full) = manifest_paths(&workspace, &loaded.config)?;

    // Build the canonical value, then rewrite each project's revision
    // with the SHA the working tree currently points at. We use
    // `manifest-rev` when available (the ref `west update` writes);
    // otherwise fall back to `HEAD`.
    let mut value = manifest.to_value();
    let projects = value["manifest"]["projects"]
        .as_array_mut()
        .expect("to_value emits manifest.projects as array");
    for (i, project) in manifest.projects.iter().enumerate() {
        let repo = workspace.join(&project.path);
        if !is_cloned(&repo, vcs.as_ref()) {
            return Err(ManifestCmdError::UncloneProject {
                name: project.name.clone(),
                path: repo,
            });
        }
        let sha = vcs
            .sha(&repo, "refs/heads/manifest-rev")
            .or_else(|_| vcs.sha(&repo, "HEAD"))
            .map_err(|e| ManifestCmdError::Vcs(format!("{}: {e}", project.name)))?;
        projects[i]["revision"] = serde_json::Value::String(sha);
    }

    let format = select_format(args.format, &full);
    let body = serialize(&value, format)?;
    write_output(args.out.as_deref(), &body)
}

// ----- Helpers -------------------------------------------------------------

fn select_format(explicit: Option<Format>, manifest_file: &Path) -> Format {
    if let Some(f) = explicit {
        return f;
    }
    match manifest_file.extension().and_then(|e| e.to_str()) {
        Some("toml") => Format::Toml,
        Some("json") => Format::Json,
        _ => Format::Yaml, // yaml/yml/(missing) all fall through here
    }
}

fn serialize(value: &serde_json::Value, format: Format) -> Result<String, ManifestCmdError> {
    match format {
        Format::Yaml => serde_yaml_ng::to_string(value).map_err(ManifestCmdError::YamlSer),
        Format::Toml => toml_edit::ser::to_string_pretty(value).map_err(ManifestCmdError::TomlSer),
        Format::Json => {
            // Pretty-printed for human-friendliness; trailing newline
            // for tooling that expects line-terminated output.
            let mut s = serde_json::to_string_pretty(value).map_err(ManifestCmdError::JsonSer)?;
            s.push('\n');
            Ok(s)
        }
    }
}

fn write_output(out: Option<&Path>, body: &str) -> Result<(), ManifestCmdError> {
    match out {
        Some(path) => fs::write(path, body).map_err(|source| ManifestCmdError::Io {
            path: path.to_owned(),
            source,
        }),
        None => {
            let mut stdout = io::stdout().lock();
            stdout
                .write_all(body.as_bytes())
                .map_err(|source| ManifestCmdError::Io {
                    path: PathBuf::from("<stdout>"),
                    source,
                })
        }
    }
}

fn resolve_workspace_dir() -> Result<PathBuf, ManifestCmdError> {
    let cwd = std::env::current_dir()
        .map_err(|e| ManifestCmdError::Config(format!("cannot get current directory: {e}")))?;
    west_core::topdir::topdir(&cwd).map_err(|_| ManifestCmdError::NotInWorkspace)
}

/// Resolve `(manifest_repo_root, manifest_file_path)` from the
/// workspace + config — same lookup `list` / `forall` / `extension` use.
fn manifest_paths(
    workspace: &Path,
    config: &Configuration,
) -> Result<(PathBuf, PathBuf), ManifestCmdError> {
    let manifest_path: PathBuf = config
        .get_str("manifest.path")
        .map_err(|e| ManifestCmdError::Config(e.to_string()))?
        .map(PathBuf::from)
        .ok_or_else(|| {
            ManifestCmdError::Config("manifest.path is not set in workspace config".into())
        })?;
    let manifest_file: PathBuf = config
        .get_str("manifest.file")
        .map_err(|e| ManifestCmdError::Config(e.to_string()))?
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(DEFAULT_MANIFEST_FILE));
    let manifest_repo_root = workspace.join(&manifest_path);
    let full = manifest_repo_root.join(&manifest_file);
    Ok((manifest_repo_root, full))
}

fn load_manifest(
    workspace: &Path,
    config: &Configuration,
    source: &ReadOnlyImportSource<'_>,
) -> Result<Manifest, ManifestCmdError> {
    let (manifest_repo_root, full) = manifest_paths(workspace, config)?;
    Manifest::from_path_with_imports(&full, &manifest_repo_root, source)
        .map_err(|e| ManifestCmdError::Manifest(format!("manifest {}: {e}", full.display())))
}

fn is_cloned(path: &Path, vcs: &dyn Vcs) -> bool {
    path.exists() && vcs.is_repo(path).unwrap_or(false)
}

/// Read-only import source shared with `list` / `forall` / `extension`.
/// An uncloned project's per-project import is skipped silently; the
/// `--resolve` / `--freeze` output reflects only what's visible.
struct ReadOnlyImportSource<'a> {
    workspace: &'a Path,
    vcs: &'a dyn Vcs,
    skipped: Mutex<Vec<String>>,
}

impl ImportSource for ReadOnlyImportSource<'_> {
    fn project_manifest(
        &self,
        project: &Project,
        relative_file: &str,
    ) -> Result<Option<String>, ImportSourceError> {
        let repo = self.workspace.join(&project.path);
        if !repo.exists() || !self.vcs.is_repo(&repo).unwrap_or(false) {
            self.skipped
                .lock()
                .unwrap_or_else(|p| p.into_inner())
                .push(project.name.clone());
            return Ok(None);
        }
        let path = repo.join(relative_file);
        match std::fs::read_to_string(&path) {
            Ok(body) => Ok(Some(body)),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(ImportSourceError::msg(format!(
                "read {}: {e}",
                path.display()
            ))),
        }
    }
}
