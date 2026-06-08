//! Python extension command discovery + dispatch.
//!
//! Today's `west` (python) lets any project ship its own subcommands
//! via a `west-commands` file (YAML, TOML, or JSON — extension
//! decides the format) referenced from the manifest's per-project
//! `west-commands:` field (or `self.west-commands:`).
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
//!    points at one or more `west-commands` files relative to its
//!    own root.
//! 3. Read + parse each file via `WestCommandsFile::from_path`
//!    (format chosen by extension: `.yml` / `.yaml` / `.toml` /
//!    `.json`). Build a flat `HashMap<command-name, ExtensionSpec>`.
//!    Skip projects that aren't cloned (their files can't be on
//!    disk yet).
//! 4. Look up `<name>` in the map. Miss → `west: unknown command`.
//! 5. Hit → spawn `python -m west._dispatch <module-path>
//!    <class-name> -- <user-argv>` with the resolved python
//!    interpreter and `WEST_TOPDIR` in env. Propagate exit code.

use std::collections::HashMap;
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};

use west_core::manifest::{Manifest, WestCommandsRef, relative_path_escapes_root};
use west_core::vcs::Vcs;
use west_core::west_commands::{WestCommandsError, WestCommandsFile};

use super::config::LoadedConfig;
use crate::exit;

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
    // `WestCommandsError` carries serde_saphyr / toml_edit / serde_json
    // error sources (a big enum) — box it so the outer
    // `Result<_, ExtensionError>` stays small (clippy's
    // `result_large_err` lint).
    #[error("{}: {source}", path.display())]
    ParseCommandsFile {
        path: PathBuf,
        #[source]
        source: Box<WestCommandsError>,
    },
    /// A `west-commands` file declared a `file:` whose path traverses
    /// out of the owning project. v1 matched the wording exactly:
    /// "extension command python file ... escapes project path ...".
    #[error(
        "extension command python file {file:?} escapes project path {}",
        project_path.display()
    )]
    EscapesProject {
        project_path: PathBuf,
        file: PathBuf,
    },
    #[error("spawn python: {0}")]
    Spawn(#[source] std::io::Error),
}

impl From<super::workspace::WorkspaceError> for ExtensionError {
    fn from(e: super::workspace::WorkspaceError) -> Self {
        match e {
            super::workspace::WorkspaceError::NotInWorkspace => ExtensionError::NotInWorkspace,
            super::workspace::WorkspaceError::Config(s) => ExtensionError::Config(s),
            super::workspace::WorkspaceError::Manifest(s) => ExtensionError::Manifest(s),
            super::workspace::WorkspaceError::Vcs(s) => ExtensionError::Vcs(s),
        }
    }
}

/// Top-level entry from `Command::External(args)`. Returns an
/// `ExitCode` so the dispatch arm can return directly without
/// further mapping.
pub(crate) fn run(args: &[OsString], loaded: &LoadedConfig) -> ExitCode {
    let name = match args.first() {
        Some(a) => a.to_string_lossy().into_owned(),
        None => {
            log::error!("unknown command");
            return exit::FAILURE;
        }
    };
    let user_argv: Vec<&OsString> = args.iter().skip(1).collect();

    // Extension discovery needs a workspace + a loadable manifest.
    // If either is unavailable, the user typed an unknown name from
    // outside a workspace — fall through to the classic "unknown
    // command" error (matches python, matches pre-extension rust
    // behaviour). Don't pretend their typo is "an extension we
    // couldn't find".
    let workspace = match super::workspace::resolve_workspace_dir() {
        Ok(w) => w,
        Err(_) => {
            log::error!("unknown command: {name}");
            return exit::FAILURE;
        }
    };

    // Failure-to-discover handling, mirroring v1's split:
    //
    //   - Extension-source problems (`ParseCommandsFile`,
    //     `EscapesProject`) get surfaced verbatim — a malformed
    //     `west-commands` file or a `file:` that traverses out of
    //     its project is an extension-author bug the workspace
    //     owner needs to see, even when the typed name isn't in the
    //     broken file.
    //   - Other errors (no workspace, manifest unparseable, vcs
    //     unavailable) are off-topic for the extension layer — the
    //     user typed a command name from outside a healthy
    //     workspace, and those surfaces will fire when they run a
    //     command that legitimately needs them. Treat as a clean
    //     "unknown command".
    let spec = match find_spec(&name, &workspace, loaded) {
        Ok(Some(s)) => s,
        Ok(None) => {
            log::error!("unknown command: {name}");
            return exit::FAILURE;
        }
        Err(e @ ExtensionError::ParseCommandsFile { .. }) => {
            log::error!("could not load extension command(s): {e}");
            return exit::FAILURE;
        }
        Err(e @ ExtensionError::EscapesProject { .. }) => {
            log::error!("{e}");
            return exit::FAILURE;
        }
        Err(_) => {
            log::error!("unknown command: {name}");
            return exit::FAILURE;
        }
    };

    match spawn(&spec, &user_argv, &workspace, loaded) {
        Ok(code) => code,
        Err(e) => {
            log::error!("{e}");
            exit::FAILURE
        }
    }
}

/// `commands.allow_extensions` — v1's workspace opt-out. When set to
/// `false`, extension discovery is bypassed entirely (every name
/// surfaces as "unknown command"). Default `true`.
fn allow_extensions(loaded: &LoadedConfig) -> bool {
    loaded
        .config
        .get_bool("commands.allow_extensions")
        .ok()
        .flatten()
        .unwrap_or(true)
}

/// Discover and look up `name`. Returns `Ok(None)` when discovery
/// succeeded but no extension matches; `Err` when discovery itself
/// failed (vcs unavailable, manifest unparseable, etc.).
fn find_spec(
    name: &str,
    workspace: &Path,
    loaded: &LoadedConfig,
) -> Result<Option<ExtensionSpec>, ExtensionError> {
    if !allow_extensions(loaded) {
        return Ok(None);
    }
    let (loaded_manifest, vcs, _skipped) =
        super::workspace::load_manifest_resolved(workspace, &loaded.config)?;
    let extensions = discover(workspace, &loaded_manifest.manifest, vcs.as_ref())?;
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
        exit::SUCCESS
    } else {
        ExitCode::from(u8::try_from(code).unwrap_or(1))
    })
}

/// Discover every extension command reachable from `manifest`,
/// keyed by the user-visible command name. Skips uncloned projects
/// silently (their `west-commands` files can't be on disk yet);
/// skips projects with no `west_commands:` declaration.
fn discover(
    workspace: &Path,
    manifest: &Manifest,
    vcs: &dyn Vcs,
) -> Result<HashMap<String, ExtensionSpec>, ExtensionError> {
    let mut out: HashMap<String, ExtensionSpec> = HashMap::new();

    // Manifest project's own `self.west-commands:`. The synthetic
    // entry lives at `<workspace>/<self.path>`.
    let self_root = workspace.join(&manifest.self_.path);
    if super::workspace::is_cloned(vcs, &self_root) {
        for cmd_file in &manifest.self_.west_commands {
            absorb_commands_file(
                &self_root,
                &manifest.self_.path,
                cmd_file,
                "manifest",
                &mut out,
            )?;
        }
    }

    // Per-project entries. Each project may declare zero or more
    // west-commands paths (.yml/.yaml/.toml/.json) relative to its
    // own root.
    for project in &manifest.projects {
        let project_root = workspace.join(&project.path);
        if !super::workspace::is_cloned(vcs, &project_root) {
            continue;
        }
        for cmd_file in &project.west_commands {
            absorb_commands_file(
                &project_root,
                &project.path,
                cmd_file,
                &project.name,
                &mut out,
            )?;
        }
    }

    Ok(out)
}

fn absorb_commands_file(
    project_root: &Path,
    project_rel_path: &Path,
    cmd_ref: &WestCommandsRef,
    project_name: &str,
    out: &mut HashMap<String, ExtensionSpec>,
) -> Result<(), ExtensionError> {
    let file_abs = project_root.join(&cmd_ref.path);
    if !file_abs.exists() {
        // Manifest pointed at a west-commands file that isn't
        // checked in; silently skip (matches v1's behaviour).
        return Ok(());
    }
    let file = WestCommandsFile::from_path(&file_abs).map_err(|source| {
        ExtensionError::ParseCommandsFile {
            path: file_abs.clone(),
            source: Box::new(source),
        }
    })?;
    // `file:` paths inside the west-commands file resolve against
    // the directory of the manifest that declared this entry. For
    // entries declared at the project root (the common case)
    // `base_dir` is empty and `resolution_root == project_root`.
    let resolution_root = project_root.join(&cmd_ref.base_dir);
    for entry in file.entries {
        // Reject `file:` values whose lexical normalization traverses
        // out of the owning project (e.g. `../../zephyr/evil.py`).
        // Check the combined `base_dir + file` so a benign `file:`
        // inside a base subdir can't escape via that base. Matches
        // the v1 `escapes_directory` check in `commands.py`.
        let combined = cmd_ref.base_dir.join(&entry.file);
        if relative_path_escapes_root(&combined) {
            return Err(ExtensionError::EscapesProject {
                project_path: project_rel_path.to_path_buf(),
                file: entry.file.clone(),
            });
        }
        let module_path = resolution_root.join(&entry.file);
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

/// One project's contribution to the workspace's extension command
/// catalog. Used by `west help` to render the
/// "extension commands from project X (path: Y):" sections.
#[derive(Debug)]
pub(crate) struct ProjectExtensions {
    /// Project name (matches manifest.projects[].name, or the
    /// literal "manifest" for the self-project).
    pub(crate) project: String,
    /// Workspace-relative path, the way the manifest declares it.
    pub(crate) path: PathBuf,
    /// `(command-name, help-text)` pairs in declaration order. Help
    /// is the `help:` field from the project's `west-commands`
    /// file; absent fields become an empty string in the output.
    pub(crate) commands: Vec<(String, String)>,
}

/// Discover extensions and return them grouped by project, in
/// manifest order, for the `west help` listing. Aligns with python
/// v1's "extension commands from project <name> (path: <path>):"
/// section format. Uncloned projects and projects without a
/// `west-commands` file are silently excluded — same contract as
/// `discover`.
pub(crate) fn list_for_help(
    loaded: &LoadedConfig,
) -> Result<Vec<ProjectExtensions>, ExtensionError> {
    if !allow_extensions(loaded) {
        return Ok(Vec::new());
    }
    let workspace = super::workspace::resolve_workspace_dir()?;
    let (loaded_manifest, vcs, _skipped) =
        super::workspace::load_manifest_resolved(&workspace, &loaded.config)?;
    let manifest = &loaded_manifest.manifest;

    let mut groups: Vec<ProjectExtensions> = Vec::new();

    // Self-project ("manifest") first. Matches the order the
    // dispatcher uses and python v1's section order.
    let self_root = workspace.join(&manifest.self_.path);
    if super::workspace::is_cloned(vcs.as_ref(), &self_root) {
        let mut commands: Vec<(String, String)> = Vec::new();
        for cmd_file in &manifest.self_.west_commands {
            collect_commands_into(&self_root, cmd_file, &mut commands)?;
        }
        if !commands.is_empty() {
            groups.push(ProjectExtensions {
                project: "manifest".to_owned(),
                path: manifest.self_.path.clone(),
                commands,
            });
        }
    }

    for project in &manifest.projects {
        let project_root = workspace.join(&project.path);
        if !super::workspace::is_cloned(vcs.as_ref(), &project_root) {
            continue;
        }
        let mut commands: Vec<(String, String)> = Vec::new();
        for cmd_file in &project.west_commands {
            collect_commands_into(&project_root, cmd_file, &mut commands)?;
        }
        if !commands.is_empty() {
            groups.push(ProjectExtensions {
                project: project.name.clone(),
                path: project.path.clone(),
                commands,
            });
        }
    }

    Ok(groups)
}

fn collect_commands_into(
    project_root: &Path,
    cmd_ref: &WestCommandsRef,
    out: &mut Vec<(String, String)>,
) -> Result<(), ExtensionError> {
    let file_abs = project_root.join(&cmd_ref.path);
    if !file_abs.exists() {
        return Ok(());
    }
    let file = WestCommandsFile::from_path(&file_abs).map_err(|source| {
        ExtensionError::ParseCommandsFile {
            path: file_abs.clone(),
            source: Box::new(source),
        }
    })?;
    for entry in file.entries {
        for cmd in entry.commands {
            out.push((cmd.name, cmd.help.unwrap_or_default()));
        }
    }
    Ok(())
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
