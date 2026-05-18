//! Python extension command discovery + dispatch.
//!
//! Today's `west` (python) lets any project ship its own subcommands
//! via a `west-commands.yml` file referenced from the manifest's
//! per-project `west-commands:` field (or `self.west-commands:`).
//! The classic flow is "all-python, same-process"; the rust port
//! keeps discovery + the rust↔python protocol on this side, and
//! delegates the actual command implementation to a python
//! subprocess that imports the installed `west` python package.
//!
//! Dispatch sequence (`west <name> <user-argv>`):
//!
//! 1. Resolve workspace + load the manifest (read-only,
//!    same `ReadOnlyImportSource` shape as `list` / `forall`).
//! 2. Walk `manifest.projects` + the synthetic manifest project for
//!    declarations. Each project that declares `west_commands:`
//!    points at one or more YAML files relative to its own root.
//! 3. Read + parse each YAML. Build a flat `HashMap<command-name,
//!    ExtensionSpec>`. Skip projects that aren't cloned (their
//!    YAML isn't on disk yet).
//! 4. Look up `<name>` in the map. Miss → `west: unknown command`.
//! 5. Hit → spawn `python -m west._dispatch <module-path>
//!    <class-name> -- <user-argv>` with the resolved python
//!    interpreter and `WEST_TOPDIR` in env. Propagate exit code.

use std::collections::HashMap;
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};
use std::sync::Mutex;

use west_core::manifest::{ImportSource, ImportSourceError, Manifest, Project};
use west_core::vcs::{self, Vcs};
use west_core::west_commands::{WestCommandsError, WestCommandsFile};

use super::config::LoadedConfig;

const DEFAULT_MANIFEST_FILE: &str = "west.yml";

/// Where to find one extension command's python implementation.
#[derive(Debug, Clone)]
pub(crate) struct ExtensionSpec {
    /// Project the command was discovered in. Held for future
    /// diagnostics (`west: extension <name> from project <p>
    /// failed`) — not consumed yet.
    #[allow(dead_code)]
    pub(crate) project: String,
    /// Absolute path to the python file declaring the WestCommand subclass.
    pub(crate) module_path: PathBuf,
    /// Python class to instantiate.
    pub(crate) class: String,
}

#[derive(Debug, thiserror::Error)]
pub(crate) enum ExtensionError {
    #[error("not inside a west workspace (no .west/ found)")]
    NotInWorkspace,
    #[error("{0}")]
    Config(String),
    #[error("{0}")]
    Manifest(String),
    #[error("{0}")]
    Vcs(String),
    // `WestCommandsError` carries a `serde_saphyr::Error` which is
    // a big enum — box it so the outer `Result<_, ExtensionError>`
    // stays small (clippy's `result_large_err` lint).
    #[error("{}: {source}", path.display())]
    YamlParse {
        path: PathBuf,
        #[source]
        source: Box<WestCommandsError>,
    },
    #[error("spawn python: {0}")]
    Spawn(#[source] std::io::Error),
}

/// Top-level entry from `Command::External(args)`. Returns an
/// `ExitCode` so the dispatch arm can return directly without
/// further mapping.
pub(crate) fn run(args: &[OsString], loaded: &LoadedConfig) -> ExitCode {
    let name = match args.first() {
        Some(a) => a.to_string_lossy().into_owned(),
        None => {
            eprintln!("west: unknown command");
            return ExitCode::FAILURE;
        }
    };
    let user_argv: Vec<&OsString> = args.iter().skip(1).collect();

    // Extension discovery needs a workspace + a loadable manifest.
    // If either is unavailable, the user typed an unknown name from
    // outside a workspace — fall through to the classic "unknown
    // command" error (matches python, matches pre-extension rust
    // behaviour). Don't pretend their typo is "an extension we
    // couldn't find".
    let workspace = match resolve_workspace_dir() {
        Ok(w) => w,
        Err(_) => {
            eprintln!("west: unknown command: {name}");
            return ExitCode::FAILURE;
        }
    };

    // Any failure to discover (no manifest configured, vcs
    // unavailable, yaml parse error in a project) is treated as
    // "no extensions available" — the user's typo wasn't an
    // extension and the surrounding error surfaces are off-topic
    // here. The user gets a clean "unknown command". Real
    // workspace problems show up when they invoke other commands
    // (list / update / etc.) that legitimately need the manifest.
    let spec = match find_spec(&name, &workspace, loaded) {
        Ok(Some(s)) => s,
        Ok(None) | Err(_) => {
            eprintln!("west: unknown command: {name}");
            return ExitCode::FAILURE;
        }
    };

    match spawn(&spec, &user_argv, &workspace, loaded) {
        Ok(code) => code,
        Err(e) => {
            eprintln!("west: {e}");
            ExitCode::FAILURE
        }
    }
}

/// Discover and look up `name`. Returns `Ok(None)` when discovery
/// succeeded but no extension matches; `Err` when discovery itself
/// failed (vcs unavailable, manifest unparseable, etc.).
fn find_spec(
    name: &str,
    workspace: &Path,
    loaded: &LoadedConfig,
) -> Result<Option<ExtensionSpec>, ExtensionError> {
    let vcs = vcs::from_config(&loaded.config).map_err(|e| ExtensionError::Vcs(e.to_string()))?;
    let source = ReadOnlyImportSource {
        workspace,
        vcs: vcs.as_ref(),
        skipped: Mutex::new(Vec::new()),
    };
    let manifest = load_manifest(workspace, &loaded.config, &source)?;
    let extensions = discover(workspace, &manifest, vcs.as_ref())?;
    Ok(extensions.get(name).cloned())
}

fn spawn(
    spec: &ExtensionSpec,
    user_argv: &[&OsString],
    workspace: &Path,
    loaded: &LoadedConfig,
) -> Result<ExitCode, ExtensionError> {
    let python = resolve_python();
    let mut cmd = Command::new(&python);
    cmd.arg("-m")
        .arg("west._dispatch")
        .arg(&spec.module_path)
        .arg(&spec.class);
    // Forward the binary's `--config NAME=VALUE` and `--config-file
    // PATH` flags so the python `Configuration` the dispatcher builds
    // sees the same layer stack + inline overrides as the rust
    // binary. Each flag is a repeatable `--inline-config k=v` /
    // `--extra-config-file p` argpair that `_dispatch.py` collects
    // before the `--` user-argv separator.
    for pair in &loaded.inline_pairs {
        cmd.arg("--inline-config").arg(pair);
    }
    for path in &loaded.extra_files {
        cmd.arg("--extra-config-file").arg(path);
    }
    cmd.arg("--")
        .args(user_argv.iter().map(|s| s.as_os_str()))
        .env("WEST_TOPDIR", workspace);
    let status = cmd.status().map_err(ExtensionError::Spawn)?;
    let code = status.code().unwrap_or(1);
    Ok(if code == 0 {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(u8::try_from(code).unwrap_or(1))
    })
}

/// Discover every extension command reachable from `manifest`,
/// keyed by the user-visible command name. Skips uncloned projects
/// silently (their YAML can't be on disk yet); skips projects with
/// no `west_commands:` declaration.
fn discover(
    workspace: &Path,
    manifest: &Manifest,
    vcs: &dyn Vcs,
) -> Result<HashMap<String, ExtensionSpec>, ExtensionError> {
    let mut out: HashMap<String, ExtensionSpec> = HashMap::new();

    // Manifest project's own `self.west-commands:`. The synthetic
    // entry lives at `<workspace>/<self.path>`.
    let self_root = workspace.join(&manifest.self_.path);
    if is_cloned(&self_root, vcs) {
        for yml in &manifest.self_.west_commands {
            absorb_yaml(&self_root, yml, "manifest", &mut out)?;
        }
    }

    // Per-project entries. Each project may declare zero or more
    // YAML paths relative to its own root.
    for project in &manifest.projects {
        let project_root = workspace.join(&project.path);
        if !is_cloned(&project_root, vcs) {
            continue;
        }
        for yml in &project.west_commands {
            absorb_yaml(&project_root, yml, &project.name, &mut out)?;
        }
    }

    Ok(out)
}

fn absorb_yaml(
    project_root: &Path,
    yml_rel: &Path,
    project_name: &str,
    out: &mut HashMap<String, ExtensionSpec>,
) -> Result<(), ExtensionError> {
    let yml_abs = project_root.join(yml_rel);
    if !yml_abs.exists() {
        // Manifest pointed at a yaml that isn't checked in;
        // silently skip (matches python's behaviour for missing
        // west-commands files).
        return Ok(());
    }
    let file =
        WestCommandsFile::from_path(&yml_abs).map_err(|source| ExtensionError::YamlParse {
            path: yml_abs.clone(),
            source: Box::new(source),
        })?;
    for entry in file.entries {
        // The python file path is relative to the project root,
        // not to the yaml file.
        let module_path = project_root.join(&entry.file);
        for cmd in entry.commands {
            // First-declaration-wins (matches `commands.py`'s
            // OrderedDict pattern; later duplicates are shadowed).
            out.entry(cmd.name.clone())
                .or_insert_with(|| ExtensionSpec {
                    project: project_name.to_owned(),
                    module_path: module_path.clone(),
                    class: cmd.class.clone(),
                });
        }
    }
    Ok(())
}

fn is_cloned(path: &Path, vcs: &dyn Vcs) -> bool {
    path.exists() && vcs.is_repo(path).unwrap_or(false)
}

/// Pick which python interpreter to spawn. Order:
///
/// 1. `WEST_PYTHON` env var (explicit override).
/// 2. `VIRTUAL_ENV/bin/python` (or `Scripts/python.exe` on Windows).
/// 3. `python3` from PATH.
///
/// Caller passes the result to `Command::new` — if it's not
/// executable we fall through to spawn errors with the system
/// shell's "command not found" reporting.
fn resolve_python() -> PathBuf {
    if let Ok(p) = std::env::var("WEST_PYTHON") {
        return PathBuf::from(p);
    }
    if let Ok(venv) = std::env::var("VIRTUAL_ENV") {
        let candidate = if cfg!(windows) {
            PathBuf::from(venv).join("Scripts").join("python.exe")
        } else {
            PathBuf::from(venv).join("bin").join("python")
        };
        if candidate.exists() {
            return candidate;
        }
    }
    PathBuf::from("python3")
}

// =====================================================================
// Workspace + manifest loading (mirror of list / forall)
// =====================================================================

fn resolve_workspace_dir() -> Result<PathBuf, ExtensionError> {
    let cwd = std::env::current_dir()
        .map_err(|e| ExtensionError::Config(format!("cannot get current directory: {e}")))?;
    west_core::topdir::topdir(&cwd).map_err(|_| ExtensionError::NotInWorkspace)
}

fn load_manifest(
    workspace: &Path,
    config: &west_core::config::Configuration,
    source: &dyn ImportSource,
) -> Result<Manifest, ExtensionError> {
    let manifest_path: PathBuf = config
        .get_str("manifest.path")
        .map_err(|e| ExtensionError::Config(e.to_string()))?
        .map(PathBuf::from)
        .ok_or_else(|| {
            ExtensionError::Config("manifest.path is not set in workspace config".into())
        })?;
    let manifest_file: PathBuf = config
        .get_str("manifest.file")
        .map_err(|e| ExtensionError::Config(e.to_string()))?
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(DEFAULT_MANIFEST_FILE));
    let manifest_repo_root = workspace.join(&manifest_path);
    let full = manifest_repo_root.join(&manifest_file);
    Manifest::from_path_with_imports(&full, &manifest_repo_root, source)
        .map_err(|e| ExtensionError::Manifest(format!("manifest {}: {e}", full.display())))
}

/// Same shape as `list` / `forall`: read-only manifest import
/// source. Filesystem imports work as normal; per-project imports
/// for uncloned projects are skipped silently.
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
