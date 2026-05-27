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
use west_core::vcs::{RevSpec, Vcs};

const DEFAULT_MANIFEST_FILE: &str = "west.yml";

#[derive(Debug, thiserror::Error)]
pub(crate) enum WorkspaceError {
    #[error("not inside a west workspace (no .west/ found)")]
    NotInWorkspace,
    #[error("{0}")]
    Config(String),
    #[error("{0}")]
    Manifest(String),
    #[error("{0}")]
    Vcs(String),
}

/// Walk up from the current working directory until `.west/` is
/// found, returning the workspace root. Mirrors python's
/// `util.west_topdir()`.
pub(crate) fn resolve_workspace_dir() -> Result<PathBuf, WorkspaceError> {
    let cwd = std::env::current_dir()
        .map_err(|e| WorkspaceError::Config(format!("cannot get current directory: {e}")))?;
    west_core::topdir::topdir(&cwd).map_err(|_| WorkspaceError::NotInWorkspace)
}

/// Map a user-facing project selector to the form
/// [`west_core::manifest::Manifest::resolve_projects`] matches against
/// (project name OR manifest-relative path).
///
/// Bare names — no separator, no `./` / `../` prefix, not absolute —
/// pass through unchanged so the manifest's name-lookup branch wins.
/// Anything that looks like a path is anchored at the current working
/// directory and made workspace-relative by stripping the
/// canonicalized workspace prefix. Selectors that don't end up under
/// the workspace are returned as-is — `resolve_projects` will reject
/// them with `UnknownProject`, preserving the legacy error.
///
/// Two strip attempts cover the two cases:
///   1. Canonicalize both sides — handles macOS's `/var → /private/var`
///      redirect and any other symlink games along the path.
///   2. Plain textual strip against the canonicalized workspace — used
///      when the project isn't on disk yet (pre-`west update`), so
///      `canonicalize` on its absolute path would fail.
pub(crate) fn normalize_project_selector(sel: &str, workspace: &Path) -> String {
    let path = Path::new(sel);
    let looks_pathy = path.is_absolute()
        || sel.contains('/')
        || sel.contains(std::path::MAIN_SEPARATOR)
        || sel == "."
        || sel == ".."
        || sel.starts_with("./")
        || sel.starts_with("../");
    if !looks_pathy {
        return sel.to_owned();
    }
    let cwd = match std::env::current_dir() {
        Ok(c) => c,
        Err(_) => return sel.to_owned(),
    };
    let abs = if path.is_absolute() {
        path.to_path_buf()
    } else {
        cwd.join(path)
    };
    let ws_can = workspace
        .canonicalize()
        .unwrap_or_else(|_| workspace.to_path_buf());
    if let Ok(can) = abs.canonicalize()
        && let Ok(rel) = can.strip_prefix(&ws_can)
    {
        return rel.to_string_lossy().into_owned();
    }
    if let Ok(rel) = abs.strip_prefix(&ws_can) {
        return rel.to_string_lossy().into_owned();
    }
    sel.to_owned()
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

/// One-shot variant of [`load_manifest`] for the common case: build
/// the [`Vcs`] from config, wrap it in a [`ReadOnlyImportSource`],
/// and return the loaded manifest, the live vcs handle, and the names
/// of any uncloned projects whose per-project imports were silently
/// skipped during resolution.
///
/// Every per-project-iterating command (`list`, `forall`, `grep`,
/// `diff`, `status`, `compare`, `extension`, `manifest`) used to
/// repeat the same three lines — `from_config`, `ReadOnlyImportSource::new`,
/// `load_manifest` — with each command re-exposing a `Vcs(...)` error
/// variant of its own. This entry collapses all three into one call
/// and routes vcs-construction failures through [`WorkspaceError::Vcs`]
/// so the command's existing `From<WorkspaceError>` is the only thing
/// that needs to know.
///
/// Most callers ignore the skipped-imports list. `list` uses it to
/// emit a one-line "partial listing" warning and signal partial
/// success with a non-zero exit code.
#[allow(clippy::type_complexity)]
pub(crate) fn load_manifest_resolved(
    workspace: &Path,
    config: &Configuration,
) -> Result<(LoadedManifest, Box<dyn Vcs>, Vec<String>), WorkspaceError> {
    let vcs = west_core::vcs::from_config(config)
        .map_err(|e| WorkspaceError::Vcs(e.to_string()))?;
    let source = ReadOnlyImportSource::new(workspace, vcs.as_ref());
    let loaded = load_manifest(workspace, config, &source)?;
    let skipped = source.skipped();
    Ok((loaded, vcs, skipped))
}

/// "Has this project's working tree been materialized by
/// `west update`?" — the per-project filter that gates parallel
/// per-project work (diff/status/compare/grep/forall/extension).
///
/// `exists && vcs.is_repo` covers the two ways a project can be
/// missing: not on disk at all (never updated) or on disk as a
/// non-repo (half-clone or stale directory). VCS errors collapse
/// to `false` — same v1 behaviour.
pub(crate) fn is_cloned(vcs: &dyn Vcs, abs_path: &Path) -> bool {
    abs_path.exists() && vcs.is_repo(abs_path).unwrap_or(false)
}

/// Read the workspace's `manifest.path` config option — the
/// authoritative answer to "where is the manifest repo in the
/// workspace?". This is *not* the same as `Manifest::self_.path`
/// (the YAML's `self.path` field, which is advisory). Callers that
/// need the synthetic manifest-project's `path` for output rendering
/// should use this value by default.
pub(crate) fn manifest_path_from_config(
    config: &Configuration,
) -> Result<PathBuf, WorkspaceError> {
    config
        .get_str("manifest.path")
        .map_err(|e| WorkspaceError::Config(e.to_string()))?
        .map(PathBuf::from)
        .ok_or_else(|| {
            WorkspaceError::Config("manifest.path is not set in workspace config".into())
        })
}

/// Lower-level entry that returns the raw `Manifest` without any
/// workspace-config-derived filters. Used by `update`, which assembles
/// its own [`LoadedManifest`] around an in-flight `WorkspaceImportSource`.
pub(crate) fn load_bare_manifest(
    workspace: &Path,
    config: &Configuration,
    source: &dyn ImportSource,
) -> Result<Manifest, WorkspaceError> {
    let manifest_path = manifest_path_from_config(config)?;
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
        .ls_tree_at_ref(repo, RevSpec::ManifestRev, path)
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
                    .read_at_ref(repo, RevSpec::ManifestRev, &nested)
                    .map_err(ImportSourceError::new)?;
                let Some(bytes) = bytes else { continue };
                let body = String::from_utf8(bytes).map_err(|e| {
                    ImportSourceError::msg(format!(
                        "{project_name}: non-utf8 manifest at manifest-rev:{}: {e}",
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
                .read_at_ref(repo, RevSpec::ManifestRev, path)
                .map_err(ImportSourceError::new)?;
            match bytes {
                Some(b) => Ok(Some(ImportContent::Single(String::from_utf8(b).map_err(
                    |e| {
                        ImportSourceError::msg(format!(
                            "{project_name}: non-utf8 manifest at manifest-rev:{relative_file}: {e}",
                        ))
                    },
                )?))),
                None => Ok(None),
            }
        }
    }
}


#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::tempdir;

    /// Build a workspace tempdir with `subdir/project1` and `project2`
    /// pre-created so `canonicalize` succeeds on those targets.
    fn workspace_with_projects() -> tempfile::TempDir {
        let tmp = tempdir().expect("tempdir");
        fs::create_dir_all(tmp.path().join("subdir/project1")).unwrap();
        fs::create_dir(tmp.path().join("project2")).unwrap();
        tmp
    }

    #[test]
    fn bare_name_passes_through() {
        let ws = workspace_with_projects();
        assert_eq!(
            normalize_project_selector("project1", ws.path()),
            "project1"
        );
        // Names starting with '.' are NOT path-like unless the prefix is './'.
        assert_eq!(
            normalize_project_selector(".hidden-project", ws.path()),
            ".hidden-project"
        );
    }

    #[test]
    fn absolute_path_inside_workspace_becomes_relative() {
        let ws = workspace_with_projects();
        let abs = ws.path().join("subdir/project1");
        assert_eq!(
            normalize_project_selector(abs.to_str().unwrap(), ws.path()),
            "subdir/project1"
        );
    }

    #[test]
    fn absolute_path_outside_workspace_is_unchanged() {
        let ws = workspace_with_projects();
        let outside = tempdir().unwrap();
        let abs = outside.path().join("not-in-ws");
        // Leaf doesn't exist, so canonicalize fails; textual strip
        // also fails (different prefix). Returns the original.
        let s = abs.to_str().unwrap();
        assert_eq!(normalize_project_selector(s, ws.path()), s);
    }

    #[test]
    fn relative_dot_anchors_at_cwd() {
        let ws = workspace_with_projects();
        let nested = ws.path().join("subdir/project1");
        // Save+restore cwd; on test parallelism this would race, but
        // the rust test runner serializes per-process by default
        // and we have only one cwd-touching test in the file.
        let prev = std::env::current_dir().unwrap();
        std::env::set_current_dir(&nested).unwrap();
        let result = normalize_project_selector(".", ws.path());
        std::env::set_current_dir(prev).unwrap();
        assert_eq!(result, "subdir/project1");
    }

    #[test]
    fn uncloned_project_path_still_normalizes() {
        // canonicalize would fail on the leaf (no directory), so this
        // exercises the textual-strip fallback.
        let ws = workspace_with_projects();
        let ws_can = ws.path().canonicalize().unwrap();
        let leaf = ws_can.join("not-yet-cloned");
        let s = leaf.to_str().unwrap();
        assert_eq!(
            normalize_project_selector(s, ws.path()),
            "not-yet-cloned"
        );
    }
}
