//! Shared workspace + manifest loading helpers for commands
//! that iterate cloned projects.
//!
//! Five commands (`list`, `forall`, `extension`, `manifest`,
//! `diff`) all do the same thing at startup: walk up from cwd
//! to find `.west/`, read `manifest.path` + `manifest.file`
//! from config, then load the manifest with a `ReadOnlyImportSource`
//! that silently skips uncloned per-project imports. This module
//! holds the single canonical implementation.
//!
//! `init` and `update` deliberately don't use these:
//!
//! - `init` doesn't have a workspace yet (it's creating one), and
//!   its `resolve_workspace_dir` variant accepts a positional
//!   target directory.
//! - `update` uses `WorkspaceImportSource` (clone-on-demand) rather
//!   than `ReadOnlyImportSource` because it's the one command that
//!   materializes projects.
//!
//! Error mapping: each command's own error enum gains a
//! `From<WorkspaceError>` impl so workspace setup can use `?` and
//! the command's existing error wiring stays unchanged downstream.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use west_core::config::Configuration;
use west_core::manifest::{ImportPolicy, ImportSource, ImportSourceError, Manifest, Project};
use west_core::vcs::Vcs;

const DEFAULT_MANIFEST_FILE: &str = "west.yml";

#[derive(Debug, thiserror::Error)]
pub(crate) enum WorkspaceError {
    #[error("not inside a west workspace (no .west/ found)")]
    NotInWorkspace,
    #[error("{0}")]
    Config(String),
    #[error("{0}")]
    Manifest(String),
}

/// Walk up from the current working directory until `.west/` is
/// found, returning the workspace root. Mirrors python's
/// `util.west_topdir()`.
pub(crate) fn resolve_workspace_dir() -> Result<PathBuf, WorkspaceError> {
    let cwd = std::env::current_dir()
        .map_err(|e| WorkspaceError::Config(format!("cannot get current directory: {e}")))?;
    west_core::topdir::topdir(&cwd).map_err(|_| WorkspaceError::NotInWorkspace)
}

/// Load the manifest at `<workspace>/<manifest.path>/<manifest.file>`
/// using `source` for import resolution. The two config keys are
/// read here so callers don't need their own copies; `manifest.file`
/// defaults to `west.yml`.
pub(crate) fn load_manifest(
    workspace: &Path,
    config: &Configuration,
    source: &dyn ImportSource,
) -> Result<Manifest, WorkspaceError> {
    let manifest_path: PathBuf = config
        .get_str("manifest.path")
        .map_err(|e| WorkspaceError::Config(e.to_string()))?
        .map(PathBuf::from)
        .ok_or_else(|| {
            WorkspaceError::Config("manifest.path is not set in workspace config".into())
        })?;
    let manifest_file: PathBuf = config
        .get_str("manifest.file")
        .map_err(|e| WorkspaceError::Config(e.to_string()))?
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(DEFAULT_MANIFEST_FILE));
    let manifest_repo_root = workspace.join(&manifest_path);
    let full = manifest_repo_root.join(&manifest_file);
    Manifest::from_path_with_imports(&full, &manifest_repo_root, source, ImportPolicy::RESOLVE_ALL)
        .map_err(|e| WorkspaceError::Manifest(format!("manifest {}: {e}", full.display())))
}

/// Read-only `ImportSource` for commands that don't materialize
/// projects (everything except `update`).
///
/// Filesystem imports resolve cleanly; per-project imports for
/// uncloned projects return `Ok(None)` rather than failing, so the
/// resolver continues with whatever's actually on disk. The names
/// of skipped projects are accumulated for callers that want to
/// warn (`west list` surfaces them at the end of the run);
/// callers that don't care just drop the source after manifest
/// loading.
pub(crate) struct ReadOnlyImportSource<'a> {
    workspace: &'a Path,
    vcs: &'a dyn Vcs,
    skipped: Mutex<Vec<String>>,
}

impl<'a> ReadOnlyImportSource<'a> {
    pub(crate) fn new(workspace: &'a Path, vcs: &'a dyn Vcs) -> Self {
        Self {
            workspace,
            vcs,
            skipped: Mutex::new(Vec::new()),
        }
    }

    /// Names of uncloned projects whose per-project imports were
    /// silently skipped during the most recent manifest load.
    /// `list` uses this to print a one-line "skipped import of
    /// <project> (not yet cloned)" warning.
    pub(crate) fn skipped(&self) -> Vec<String> {
        self.skipped
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .clone()
    }
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
