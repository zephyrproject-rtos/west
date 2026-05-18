//! `west manifest` — inspect / dump / freeze / validate the workspace's
//! manifest. Five mutually-exclusive action flags via `--path`,
//! `--validate`, `--resolve`, `--freeze`, `--untracked` (clap's
//! `ArgGroup`-driven required exclusivity).
//!
//! `--resolve` / `--freeze` emit the canonical resolved manifest (post
//! import-expansion). The output format is chosen by `--format` when
//! given; otherwise it follows the source manifest's own extension
//! (`*.yml`/`*.yaml` → YAML, `*.toml` → TOML, `*.json` → JSON, with
//! YAML as the catch-all default for the conventional `west.yml` name).
//!
//! `--untracked` lists workspace entries not owned by any project,
//! matching v1's directory-collapsed shape: a non-owned subtree
//! emits the subtree root as a single line rather than every file
//! inside it. Files at the workspace root, orphan directories
//! between projects, and symlinks (treated as files — not
//! followed) all show up. Paths are emitted relative to the
//! caller's cwd (so `west -C <ws> manifest --untracked` and a
//! plain `west manifest --untracked` run from the workspace root
//! both show bare names; running from a subdir produces
//! `../orphan.txt` style paths). The default is plain text (one
//! path per line); `--format yaml|toml|json` wraps the list as
//! `{untracked: [...]}` (TOML's table-only root requires a key —
//! and the wrap reads cleanly under jq/yq pipelines anyway).
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
    .args(["path", "validate", "resolve", "freeze", "untracked"]))]
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

    /// List workspace files not owned by any project. Default output
    /// is one workspace-relative path per line; `--format` wraps the
    /// list under an `untracked:` key.
    #[arg(long)]
    pub untracked: bool,

    /// Output format. For `--resolve` / `--freeze`: defaults to the
    /// source manifest's own format (yaml / toml / json by extension;
    /// yaml otherwise). For `--untracked`: when omitted, plain
    /// line-based text; when set, wraps the list as
    /// `{untracked: [...]}` in the chosen format.
    #[arg(long, value_name = "FMT", value_enum)]
    pub format: Option<Format>,

    /// Write output to PATH instead of stdout. Applies to
    /// `--resolve` / `--freeze` / `--untracked`.
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
    if args.untracked {
        return action_untracked(args, loaded);
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

fn action_untracked(args: ManifestArgs, loaded: &LoadedConfig) -> Result<(), ManifestCmdError> {
    let workspace = resolve_workspace_dir()?;
    let vcs = vcs::from_config(&loaded.config).map_err(|e| ManifestCmdError::Vcs(e.to_string()))?;
    let source = ReadOnlyImportSource {
        workspace: workspace.as_path(),
        vcs: vcs.as_ref(),
        skipped: Mutex::new(Vec::new()),
    };
    let manifest = load_manifest(&workspace, &loaded.config, &source)?;

    // Owned roots: directories west and its projects manage. A
    // directory that matches one of these gets pruned outright; a
    // directory that contains one gets recursed into so the
    // *non-owned* slices around the project still get inspected.
    // The `.west/` dir is owned too — west's own state.
    let west_dir = workspace.join(west_core::WEST_DIR);
    let mut owned: Vec<PathBuf> = Vec::with_capacity(manifest.projects.len() + 2);
    owned.push(west_dir);
    owned.push(workspace.join(&manifest.self_.path));
    for p in &manifest.projects {
        owned.push(workspace.join(&p.path));
    }
    // Sort so the "enclosing project wins" rule matches v1's
    // behavior in workspaces with nested projects (an enclosing
    // project's path always sorts before any path nested under it).
    owned.sort();

    let mut untracked: Vec<PathBuf> = Vec::new();
    find_untracked(&workspace, &owned, &mut untracked);
    untracked.sort();

    // Match v1's `os.path.relpath(u, Path.cwd())`: emit paths
    // relative to the caller's cwd. A user running `west manifest
    // --untracked` from a project subdirectory sees `../orphan.txt`
    // rather than a workspace-rooted path; running from the
    // workspace root collapses to bare names. `-C <dir>` flips the
    // process's cwd before we get here, so this also picks up
    // that convention.
    let cwd = std::env::current_dir()
        .map_err(|e| ManifestCmdError::Config(format!("cannot get current directory: {e}")))?;
    let mut paths: Vec<String> = untracked
        .iter()
        .map(|p| relative_to(p, &cwd).to_string_lossy().into_owned())
        .collect();
    paths.sort();

    let body = format_untracked(&paths, args.format)?;
    write_output(args.out.as_deref(), &body)
}

/// Compute `target` expressed relative to `base`, walking up via
/// `..` when `target` lives outside `base`. Both arguments should
/// be absolute. The result has no leading `./`. Falls back to the
/// absolute `target` if either side isn't a normal absolute path
/// (extremely unusual on supported platforms; matches python's
/// `os.path.relpath` quietly-returning-something contract).
fn relative_to(target: &Path, base: &Path) -> PathBuf {
    use std::path::Component;
    if !target.is_absolute() || !base.is_absolute() {
        return target.to_path_buf();
    }
    let t: Vec<Component> = target.components().collect();
    let b: Vec<Component> = base.components().collect();
    let common = t.iter().zip(b.iter()).take_while(|(a, c)| a == c).count();
    let mut out = PathBuf::new();
    for _ in common..b.len() {
        out.push("..");
    }
    for c in &t[common..] {
        out.push(c.as_os_str());
    }
    if out.as_os_str().is_empty() {
        out.push(".");
    }
    out
}

/// Walk `dir` top-down, classifying each entry against `owned`:
///
/// - non-directory (regular file, symlink — even a symlink to a
///   directory) → emit;
/// - directory that matches an owned root → drop (the project owns
///   what's inside);
/// - directory that *contains* an owned root → recurse, so the
///   non-owned siblings around the project still get inspected;
/// - directory with no owned root inside it → emit the directory
///   itself, do not recurse. Matches v1's
///   `west.app.project.ProjectCommand._untracked`: an untracked
///   subtree collapses to a single output line.
///
/// Unreadable entries are silently dropped — a perm-denied subdir
/// shouldn't make `--untracked` fail; it's not actionable from the
/// user's perspective.
fn find_untracked(dir: &Path, owned: &[PathBuf], out: &mut Vec<PathBuf>) {
    let entries = match std::fs::read_dir(dir) {
        Ok(it) => it,
        Err(_) => return,
    };
    for entry in entries {
        let entry = match entry {
            Ok(e) => e,
            Err(_) => continue,
        };
        let path = entry.path();
        let file_type = match entry.file_type() {
            Ok(t) => t,
            Err(_) => continue,
        };
        // Treat symlinks (even symlinks to directories) as files —
        // we don't recurse through them. Matches v1's `not
        // e.is_dir() or e.is_symlink()` check.
        if !file_type.is_dir() || file_type.is_symlink() {
            out.push(path);
            continue;
        }
        // Real directory. Walk the sorted owned list: an exact
        // match means this dir IS a project; a path-prefix match
        // means a project lives inside; anything else means the
        // dir holds no owned content and is itself an untracked
        // subtree.
        let mut handled = false;
        for op in owned {
            if op == &path {
                handled = true;
                break;
            }
            if op.starts_with(&path) {
                find_untracked(&path, owned, out);
                handled = true;
                break;
            }
        }
        if !handled {
            out.push(path);
        }
    }
}

/// Render the sorted `paths` list. `None` → plain line-based text;
/// `Some(format)` → wrap as `{untracked: [...]}` and serialize via
/// the chosen format. The wrap is mandatory for TOML (top-level
/// must be a table) and incidentally reads cleanly under jq / yq.
fn format_untracked(
    paths: &[String],
    format: Option<Format>,
) -> Result<String, ManifestCmdError> {
    match format {
        None => {
            if paths.is_empty() {
                Ok(String::new())
            } else {
                let mut s = paths.join("\n");
                s.push('\n');
                Ok(s)
            }
        }
        Some(f) => {
            let value = serde_json::json!({ "untracked": paths });
            serialize(&value, f)
        }
    }
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
    fn project_root(&self, project: &Project) -> Option<PathBuf> {
        Some(self.workspace.join(&project.path))
    }

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
