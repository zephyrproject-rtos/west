//! `ImportSource` implementation backed by the workspace's vcs.
//!
//! Per-project imports require reading a manifest file from inside a
//! project's working tree, at that project's manifest revision. This
//! module bridges from the data-layer `ImportSource` callback to the
//! workspace's `Vcs`: clone-if-missing, fetch, set manifest-rev,
//! checkout, then read the requested file.
//!
//! Output policy: import-source ops are intentionally quiet
//! (`Output::Capture` into a discarded buffer). The `west update` worker
//! later prints its own banners and live progress per project; we don't
//! want pre-import git output spilling between the user's command line
//! and the first banner.

use std::io;
use std::path::Path;

use west_core::manifest::{ImportSource, ImportSourceError, Project};
use west_core::vcs::{CheckoutTarget, FetchSpec, Output, Vcs, VcsError};

pub struct WorkspaceImportSource<'a> {
    workspace: &'a Path,
    vcs: &'a dyn Vcs,
}

impl<'a> WorkspaceImportSource<'a> {
    pub fn new(workspace: &'a Path, vcs: &'a dyn Vcs) -> Self {
        Self { workspace, vcs }
    }
}

impl ImportSource for WorkspaceImportSource<'_> {
    fn project_manifest(
        &self,
        project: &Project,
        relative_file: &str,
    ) -> Result<Option<String>, ImportSourceError> {
        let repo = self.workspace.join(&project.path);

        // 1. Ensure cloned.
        let already_cloned = repo.exists() && self.vcs.is_repo(&repo).unwrap_or(false);
        if !already_cloned {
            if let Some(parent) = repo.parent() {
                std::fs::create_dir_all(parent)
                    .map_err(|e| ImportSourceError(format!("create {}: {e}", parent.display())))?;
            }
            run_quiet(|out| {
                self.vcs.clone(
                    &project.url,
                    &repo,
                    Some(&project.revision),
                    Some(&project.remote_name),
                    out,
                )
            })
            .map_err(stringify)?;
        }

        // 2. Fetch (smart-skip will no-op if revision is local).
        run_quiet(|out| {
            self.vcs.fetch(
                &repo,
                &FetchSpec {
                    remote: &project.remote_name,
                    revision: Some(&project.revision),
                },
                out,
            )
        })
        .map_err(stringify)?;

        // 3. Resolve the manifest revision and record manifest-rev.
        let sha = match self.vcs.sha(&repo, "FETCH_HEAD") {
            Ok(s) => s,
            Err(_) => self.vcs.sha(&repo, &project.revision).map_err(stringify)?,
        };
        self.vcs
            .set_manifest_rev(&repo, &sha, Some("west update: pre-import"))
            .map_err(stringify)?;
        self.vcs
            .checkout(&repo, &CheckoutTarget::Detached(&sha))
            .map_err(stringify)?;

        // 4. Read the imported file.
        let path = repo.join(relative_file);
        match std::fs::read_to_string(&path) {
            Ok(body) => Ok(Some(body)),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(ImportSourceError(format!("read {}: {e}", path.display()))),
        }
    }
}

/// Run `op` with a discarding `Output::Capture` so import-source git
/// noise doesn't bleed onto the user's terminal before update's own
/// banners print.
fn run_quiet<F>(op: F) -> Result<(), VcsError>
where
    F: FnOnce(&mut Output<'_>) -> Result<(), VcsError>,
{
    let mut buf: Vec<u8> = Vec::new();
    let mut out = Output::Capture(&mut buf);
    op(&mut out)
}

fn stringify(e: VcsError) -> ImportSourceError {
    ImportSourceError(e.to_string())
}
