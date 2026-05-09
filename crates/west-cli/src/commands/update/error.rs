//! Typed errors for the `west update` per-project worker.
//!
//! Each variant tags an upstream `VcsError` (or `io::Error`) with the
//! VCS operation that produced it. Callers can pattern-match to
//! differentiate "fetch failed" from "rebase conflict" from "checkout
//! failed (dirty tree)" without parsing strings.

use std::io;
use std::path::PathBuf;

use west_core::vcs::VcsError;

#[derive(Debug, thiserror::Error)]
pub(super) enum UpdateError {
    #[error("create parent directory {}: {source}", path.display())]
    CreateParent {
        path: PathBuf,
        #[source]
        source: io::Error,
    },
    #[error("clone {url}: {source}")]
    Clone {
        url: String,
        #[source]
        source: VcsError,
    },
    #[error("fetch from {remote}: {source}")]
    Fetch {
        remote: String,
        #[source]
        source: VcsError,
    },
    #[error("record manifest-rev: {0}")]
    SetManifestRev(#[source] VcsError),
    #[error("read HEAD branch: {0}")]
    HeadBranch(#[source] VcsError),
    #[error("ancestor check: {0}")]
    IsAncestor(#[source] VcsError),
    #[error("rebase onto manifest-rev: {0}")]
    Rebase(#[source] VcsError),
    #[error("checkout {sha}: {source}")]
    Checkout {
        sha: String,
        #[source]
        source: VcsError,
    },
    #[error("update submodules: {0}")]
    Submodules(#[source] VcsError),
}
