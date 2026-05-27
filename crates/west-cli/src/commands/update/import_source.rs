//! `ImportSource` implementation backed by the workspace's vcs.
//!
//! Per-project imports require reading a manifest file from inside a
//! project's working tree, at that project's manifest revision. This
//! module bridges from the data-layer `ImportSource` callback to the
//! workspace's `Vcs`: clone-if-missing, fetch, set manifest-rev,
//! checkout, then read the requested file.
//!
//! Output policy — matches `west update`'s per-project worker UX:
//!
//! - With an attached [`ImportProgress`] (TTY + non-raw): each
//!   import-resolution clone gets its own indicatif progress bar with
//!   phase + percentage. On completion, the bar is replaced by a
//!   permanent `prefix ✓ <sha> <subject>` row above any still-active
//!   bars — same shape `update`'s main worker pool prints for each
//!   project. Failure: `prefix ✗ <msg>` row.
//!
//! - Without progress (`--raw` / non-TTY): git output goes straight to
//!   the parent's stdio via [`Output::Native`]. Previously this path
//!   used a `NullSink` ("intentionally quiet") which silently hung
//!   minutes-long clones on Zephyr-sized workspaces — fixed.

use std::path::Path;

use indicatif::{MultiProgress, ProgressBar};

use west_core::manifest::{ImportContent, ImportSource, ImportSourceError, Project};
use west_core::vcs::{
    CheckoutTarget, CommitSummary, FetchSpec, InitSpec, Output, RevSpec, Vcs, VcsError,
};

use super::Settings;
use super::cache;
use crate::progress::{
    IndicatifSink, PREFIX_WIDTH, TICK_INTERVAL, render_done_line, render_failed_line,
    spinner_style, truncate_prefix,
};

/// Holds the `MultiProgress` that import-resolution clones render
/// into. Construct once per `west update` invocation and attach via
/// [`WorkspaceImportSource::with_progress`].
///
/// `update::run` builds one of these only when the active mode would
/// use indicatif for the main worker pool too (TTY + non-raw); other
/// modes leave it unset and the source falls back to native git
/// stdio.
pub struct ImportProgress {
    multi: MultiProgress,
}

impl ImportProgress {
    pub fn new() -> Self {
        Self {
            multi: MultiProgress::new(),
        }
    }
}

impl Default for ImportProgress {
    fn default() -> Self {
        Self::new()
    }
}

pub struct WorkspaceImportSource<'a> {
    workspace: &'a Path,
    vcs: &'a dyn Vcs,
    progress: Option<&'a ImportProgress>,
    settings: Option<&'a Settings>,
}

impl<'a> WorkspaceImportSource<'a> {
    pub fn new(workspace: &'a Path, vcs: &'a dyn Vcs) -> Self {
        Self {
            workspace,
            vcs,
            progress: None,
            settings: None,
        }
    }

    pub fn with_progress(mut self, progress: &'a ImportProgress) -> Self {
        self.progress = Some(progress);
        self
    }

    /// Route import-resolution clones through the cache helper so the
    /// auto-cache mirror gets populated during import resolution.
    /// Without this the main worker pool would re-clone the project
    /// across the network when it later processes the same project.
    pub fn with_settings(mut self, settings: &'a Settings) -> Self {
        self.settings = Some(settings);
        self
    }
}

impl ImportSource for WorkspaceImportSource<'_> {
    fn project_root(&self, project: &Project) -> Option<std::path::PathBuf> {
        Some(self.workspace.join(&project.path))
    }

    fn project_manifest(
        &self,
        project: &Project,
        relative_file: &str,
    ) -> Result<Option<ImportContent>, ImportSourceError> {
        let repo = self.workspace.join(&project.path);

        match self.progress {
            None => {
                self.materialize_native(project, &repo)?;
            }
            Some(progress) => {
                self.materialize_with_progress(project, &repo, progress)?;
            }
        }

        // Read the imported file(s) from git at `manifest-rev`.
        // `materialize` above just ran `set_manifest_rev(repo, sha)`,
        // so the ref points at the commit we just landed. Directory
        // imports (`import: <dir>/`) and single-file imports share
        // the same helper as `ReadOnlyImportSource`.
        crate::commands::workspace::read_project_import(
            self.vcs,
            &repo,
            relative_file,
            &project.name,
        )
    }
}

impl WorkspaceImportSource<'_> {
    /// Native-stdio path. Git's own progress goes to the parent's
    /// terminal. Used in `--raw` / non-TTY modes.
    fn materialize_native(
        &self,
        project: &Project,
        repo: &Path,
    ) -> Result<String, ImportSourceError> {
        let mut out = Output::Native;
        self.materialize(project, repo, &mut out)
    }

    /// Indicatif path. Each call adds a per-project bar to the
    /// shared `MultiProgress`; on completion the bar is replaced by a
    /// permanent done / failed row.
    fn materialize_with_progress(
        &self,
        project: &Project,
        repo: &Path,
        progress: &ImportProgress,
    ) -> Result<(), ImportSourceError> {
        let prefix = truncate_prefix(&project.name, PREFIX_WIDTH);
        let bar = progress.multi.add(ProgressBar::new_spinner());
        bar.set_prefix(prefix.clone());
        bar.set_style(spinner_style());
        bar.set_message("resolving import…");
        bar.enable_steady_tick(TICK_INTERVAL);

        let mut sink = IndicatifSink::new(bar.clone(), None);
        let result = {
            let mut out = Output::Stream(&mut sink);
            self.materialize(project, repo, &mut out)
        };

        bar.finish_and_clear();
        match &result {
            Ok(sha) => {
                let summary = self
                    .vcs
                    .commit_summary(repo, RevSpec::Named(sha))
                    .ok()
                    .unwrap_or_else(|| CommitSummary {
                        short_sha: sha.chars().take(12).collect(),
                        subject: "imported".into(),
                    });
                let _ = progress.multi.println(render_done_line(&prefix, &summary));
            }
            Err(e) => {
                let _ = progress
                    .multi
                    .println(render_failed_line(&prefix, &e.to_string()));
            }
        }
        result.map(|_| ())
    }

    /// Run the actual clone / fetch / set-manifest-rev / checkout
    /// sequence. Returns the SHA the working tree was checked out to —
    /// the progress path uses it to look up the commit subject for
    /// the done line.
    ///
    /// When [`Settings`] is attached the initial clone is routed through
    /// [`cache::clone_via_cache`] so the auto-cache mirror is populated
    /// once during import resolution; the main worker pool then reuses
    /// the same cache (smart-skipped fetch) instead of re-cloning the
    /// project across the network.
    fn materialize(
        &self,
        project: &Project,
        repo: &Path,
        out: &mut Output<'_>,
    ) -> Result<String, ImportSourceError> {
        let already_cloned = repo.exists() && self.vcs.is_repo(repo).unwrap_or(false);
        if !already_cloned {
            match self.settings {
                // With settings (the normal `west update` flow) the
                // cache helper picks init-vs-clone based on whether a
                // cache source matches.
                Some(settings) => {
                    cache::materialize(self.vcs, project, settings, repo, out)
                        .map_err(ImportSourceError::new)?;
                }
                // No settings ⇒ no cache possible. Init an empty repo
                // wired to the URL; the fetch below pulls exactly the
                // requested revision (manifests routinely pin bare
                // SHAs, which a clone --branch wouldn't accept anyway).
                None => {
                    if let Some(parent) = repo.parent() {
                        std::fs::create_dir_all(parent)
                            .map_err(|source| VcsError::Io {
                                path: parent.to_path_buf(),
                                source,
                            })
                            .map_err(ImportSourceError::new)?;
                    }
                    self.vcs
                        .init(&InitSpec {
                            url: &project.url,
                            dest: repo,
                            origin: Some(&project.remote_name),
                        })
                        .map_err(ImportSourceError::new)?;
                }
            }
        }

        // Fetch. The returned sha is what `project.revision` resolves to
        // (FETCH_HEAD^{commit} after an active fetch, the locally-
        // resolved revision on smart-skip). Don't sniff FETCH_HEAD
        // afterward — it persists across fetches and would be stale on
        // the smart-skip path.
        let sha = self
            .vcs
            .fetch(
                repo,
                // Fetch by URL — v1 contract; the local
                // `[remote "<remote_name>"]` git config is a user
                // convenience, not the source of truth for west.
                &FetchSpec {
                    remote: &project.url,
                    revision: Some(&project.revision),
                },
                out,
            )
            .map_err(ImportSourceError::new)?;

        self.vcs
            .set_manifest_rev(repo, &sha, Some("west update: pre-import"))
            .map_err(ImportSourceError::new)?;
        self.vcs
            .checkout(repo, &CheckoutTarget::Detached(&sha), out)
            .map_err(ImportSourceError::new)?;
        Ok(sha)
    }
}
