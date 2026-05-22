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
use west_core::loaded::{LoadedManifest, ProjectFilter, ProjectFilterError};
use west_core::manifest::{
    ImportContent, ImportPolicy, ImportSource, ImportSourceError, Manifest, NamedBody, Project,
};
use west_core::vcs::{MANIFEST_REV_REF, Vcs};

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
/// using `source` for import resolution, **plus** the workspace's
/// `manifest.group-filter` and `manifest.project-filter` so the
/// returned [`LoadedManifest`] can answer activity queries by itself.
/// Every project-iterating command should route through this entry
/// rather than `Manifest::from_path*` directly — the wrapper makes
/// project-filter bypasses a type error rather than a silent bug.
pub(crate) fn load_manifest(
    workspace: &Path,
    config: &Configuration,
    source: &dyn ImportSource,
) -> Result<LoadedManifest, WorkspaceError> {
    let manifest = load_bare_manifest(workspace, config, source)?;
    let config_group_filter = super::select::read_manifest_group_filter(config)
        .map_err(WorkspaceError::Config)?;
    let project_filter = ProjectFilter::from_config(config).map_err(WorkspaceError::from)?;
    Ok(LoadedManifest::new(
        manifest,
        config_group_filter,
        project_filter,
    ))
}

/// Lower-level entry that returns the raw `Manifest` without any
/// workspace-config-derived filters. Used by `update`, which assembles
/// its own [`LoadedManifest`] around an in-flight `WorkspaceImportSource`.
pub(crate) fn load_bare_manifest(
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
    Manifest::from_path_with(
        &full,
        Some(&manifest_repo_root),
        Some(source),
        ImportPolicy::RESOLVE_ALL,
    )
    .map_err(|e| WorkspaceError::Manifest(format!("manifest {}: {e}", full.display())))
}

impl From<ProjectFilterError> for WorkspaceError {
    fn from(e: ProjectFilterError) -> Self {
        WorkspaceError::Config(e.to_string())
    }
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
    ) -> Result<Option<ImportContent>, ImportSourceError> {
        let repo = self.workspace.join(&project.path);
        if !repo.exists() || !self.vcs.is_repo(&repo).unwrap_or(false) {
            self.skipped
                .lock()
                .unwrap_or_else(|p| p.into_inner())
                .push(project.name.clone());
            return Ok(None);
        }
        match read_project_import(self.vcs, &repo, relative_file, &project.name)? {
            Some(content) => Ok(Some(content)),
            None => {
                self.skipped
                    .lock()
                    .unwrap_or_else(|p| p.into_inner())
                    .push(project.name.clone());
                Ok(None)
            }
        }
    }
}

/// Shared read-from-git helper used by [`ReadOnlyImportSource`] and
/// [`crate::commands::update::import_source::WorkspaceImportSource`].
///
/// Tries the directory-import path first via [`Vcs::ls_tree_at_ref`]:
/// for a tree, the YAML/TOML/JSON entries are sorted, read via
/// [`Vcs::read_at_ref`], and returned as
/// [`ImportContent::Multiple`]. Other extensions (`.txt`, …) are
/// silently filtered out — matches v1 and `absorb_filesystem_import`'s
/// own directory rule.
///
/// On a blob path, falls back to [`Vcs::read_at_ref`] and returns
/// [`ImportContent::Single`]. Missing ref / missing path collapses
/// to `Ok(None)` so the import is soft-skipped.
pub(crate) fn read_project_import(
    vcs: &dyn Vcs,
    repo: &Path,
    relative_file: &str,
    project_name: &str,
) -> Result<Option<ImportContent>, ImportSourceError> {
    let path = std::path::Path::new(relative_file);
    match vcs
        .ls_tree_at_ref(repo, MANIFEST_REV_REF, path)
        .map_err(ImportSourceError::new)?
    {
        Some(mut entries) => {
            entries.retain(|name| {
                matches!(
                    std::path::Path::new(name)
                        .extension()
                        .and_then(|s| s.to_str()),
                    Some("yml") | Some("yaml") | Some("toml") | Some("json")
                )
            });
            entries.sort();
            let mut bodies: Vec<NamedBody> = Vec::with_capacity(entries.len());
            for name in entries {
                let nested = path.join(&name);
                let bytes = vcs
                    .read_at_ref(repo, MANIFEST_REV_REF, &nested)
                    .map_err(ImportSourceError::new)?;
                let Some(bytes) = bytes else { continue };
                let body = String::from_utf8(bytes).map_err(|e| {
                    ImportSourceError::msg(format!(
                        "{project_name}: non-utf8 manifest at {MANIFEST_REV_REF}:{}: {e}",
                        nested.display(),
                    ))
                })?;
                // The filename (without the directory prefix) carries
                // the extension the resolver uses to pick its parser.
                bodies.push(NamedBody { name, body });
            }
            Ok(Some(ImportContent::Multiple(bodies)))
        }
        None => {
            let bytes = vcs
                .read_at_ref(repo, MANIFEST_REV_REF, path)
                .map_err(ImportSourceError::new)?;
            match bytes {
                Some(b) => Ok(Some(ImportContent::Single(String::from_utf8(b).map_err(
                    |e| {
                        ImportSourceError::msg(format!(
                            "{project_name}: non-utf8 manifest at {MANIFEST_REV_REF}:{relative_file}: {e}",
                        ))
                    },
                )?))),
                None => Ok(None),
            }
        }
    }
}
