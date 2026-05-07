//! VCS abstraction.
//!
//! `Vcs` is the trait every implementation must satisfy; concrete
//! implementations are *clients* of the underlying VCS tool (`GitClient`,
//! eventually `JjClient`, …). The trait surface stays declarative: each
//! method describes what the caller wants, not how the underlying tool
//! achieves it. Behavior knobs (fetch strategy, shallow depth, tag handling,
//! …) live in `tool.<client>.<key>` config keys and are read inside the
//! client — the trait surface never grows for them.
//!
//! # Configuration
//!
//! - `vcs.client = "<name>"` — selects which client to use. Defaults to
//!   `"git"`.
//! - `tool.<client>.<key>` — per-client knobs, in a `[tool.<client>]` table.
//!   Mirrors `pyproject.toml`'s `[tool.<name>]` namespace. For example,
//!   `tool.git.binary` overrides the path to the `git` executable.
//!
//! Operations that are inherently specific to one client (shallow clone,
//! submodule init, jj-style colocation) are NOT in the trait. They live in
//! the client's own options struct, populated from `tool.<client>.<key>`.
//!
//! # Manifest-rev
//!
//! West stores a per-project pointer to "the revision the manifest told us
//! to be at." That pointer is read on every update to decide whether the
//! working tree needs to move. The trait exposes
//! [`Vcs::set_manifest_rev`] / [`Vcs::manifest_rev`]; how the pointer is
//! stored is the client's business — git uses `refs/heads/manifest-rev`,
//! other clients are free to choose.

use std::error::Error;
use std::fmt;
use std::path::{Path, PathBuf};

use crate::config::Configuration;

mod git;

pub use git::{FetchStrategy, GitClient, GitOptions};

/// Operations every VCS client supports.
pub trait Vcs: fmt::Debug {
    /// Client identifier (`"git"`, `"jj"`, …). Stable; surfaces in errors.
    fn name(&self) -> &'static str;

    /// `true` if `path` is a working copy of this VCS.
    fn is_repo(&self, path: &Path) -> Result<bool, VcsError>;

    /// Clone `url` into `dest`. `revision` is a branch, tag, or — where the
    /// client supports it — a commit reference; each client interprets it as
    /// the user would expect when passing it to the underlying tool.
    /// `origin` overrides the remote name (default `"origin"` for git).
    ///
    /// Git note: passes `revision` to `git clone --branch`, which accepts
    /// branch and tag names only. Landing at a bare commit SHA requires a
    /// follow-up [`Vcs::checkout`].
    fn clone(
        &self,
        url: &str,
        dest: &Path,
        revision: Option<&str>,
        origin: Option<&str>,
    ) -> Result<(), VcsError>;

    /// Resolve `rev` to a commit SHA in `repo`. `"HEAD"` resolves the current
    /// commit.
    fn sha(&self, repo: &Path, rev: &str) -> Result<String, VcsError>;

    /// Is `ancestor` reachable as an ancestor of `descendant`?
    fn is_ancestor(&self, repo: &Path, ancestor: &str, descendant: &str) -> Result<bool, VcsError>;

    /// Bring remote refs in `repo` up to date with `spec.remote`. Whether
    /// the network call is actually made (smart-skip when the requested
    /// revision is already local), how tags are handled, and whether the
    /// fetch is shallow are all driven by `tool.<client>.fetch.*` keys read
    /// at client construction.
    fn fetch(&self, repo: &Path, spec: &FetchSpec<'_>) -> Result<(), VcsError>;

    /// Move HEAD in `repo` to `target`.
    ///
    /// `Detached(rev)` lands HEAD on the commit without binding it to a
    /// branch — the safe default for updates. `Branch(name)` switches to an
    /// existing local branch.
    fn checkout(&self, repo: &Path, target: &CheckoutTarget<'_>) -> Result<(), VcsError>;

    /// `true` when `repo`'s working tree has no uncommitted changes.
    fn is_clean(&self, repo: &Path) -> Result<bool, VcsError>;

    /// Record `sha` as the manifest-rev of `repo`. Implementations choose
    /// where to store it; git writes `refs/heads/manifest-rev`.
    fn set_manifest_rev(&self, repo: &Path, sha: &str) -> Result<(), VcsError>;

    /// Read the recorded manifest-rev of `repo`. Returns `Ok(None)` if no
    /// manifest-rev has been recorded yet (fresh clone, hand-curated dir).
    fn manifest_rev(&self, repo: &Path) -> Result<Option<String>, VcsError>;
}

/// What to fetch.
///
/// `revision` is optional: if `Some`, the implementation may apply the
/// configured fetch strategy (e.g. `smart` skips the fetch when the revision
/// is already locally available). If `None`, a default refspec fetch is
/// performed (`git fetch <remote>`).
#[derive(Debug, Clone, Copy)]
pub struct FetchSpec<'a> {
    pub remote: &'a str,
    pub revision: Option<&'a str>,
}

/// Where to land HEAD on [`Vcs::checkout`].
#[derive(Debug, Clone, Copy)]
pub enum CheckoutTarget<'a> {
    /// A commit (SHA or any other revspec the client resolves). HEAD is
    /// detached after the call.
    Detached(&'a str),
    /// An existing local branch.
    Branch(&'a str),
}

/// Errors common to any client. Implementations wrap their tool-specific
/// failures into one of these.
#[derive(Debug)]
pub enum VcsError {
    /// The client tool returned a non-zero exit code. Captures the argv and
    /// stderr for diagnostics.
    CommandFailed {
        client: &'static str,
        argv: Vec<String>,
        exit_code: Option<i32>,
        stderr: String,
    },
    /// The client tool isn't on PATH or couldn't be executed.
    ClientUnavailable {
        client: &'static str,
        source: std::io::Error,
    },
    /// File-system I/O error around a repo or working tree.
    Io {
        path: PathBuf,
        source: std::io::Error,
    },
    /// Output from the client tool didn't match the expected shape.
    BadOutput {
        client: &'static str,
        argv: Vec<String>,
        detail: String,
    },
    /// `vcs.client` named a client we don't recognize.
    UnknownClient(String),
    /// A `tool.<client>.<key>` config value had the wrong type or shape.
    BadOption { key: String, detail: String },
}

impl fmt::Display for VcsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            VcsError::CommandFailed {
                client,
                argv,
                exit_code,
                stderr,
            } => {
                let code = match exit_code {
                    Some(c) => c.to_string(),
                    None => "?".to_owned(),
                };
                let stderr = stderr.trim();
                write!(
                    f,
                    "{client} command failed (exit={code}): {}{}{}",
                    argv.join(" "),
                    if stderr.is_empty() { "" } else { ": " },
                    stderr,
                )
            }
            VcsError::ClientUnavailable { client, source } => {
                write!(f, "{client} client is unavailable: {source}")
            }
            VcsError::Io { path, source } => {
                write!(f, "io error on {}: {source}", path.display())
            }
            VcsError::BadOutput {
                client,
                argv,
                detail,
            } => {
                write!(
                    f,
                    "{client} produced unexpected output for `{}`: {detail}",
                    argv.join(" ")
                )
            }
            VcsError::UnknownClient(name) => {
                write!(f, "unknown vcs.client: {name:?}")
            }
            VcsError::BadOption { key, detail } => {
                write!(f, "bad option {key:?}: {detail}")
            }
        }
    }
}

impl Error for VcsError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            VcsError::ClientUnavailable { source, .. } => Some(source),
            VcsError::Io { source, .. } => Some(source),
            _ => None,
        }
    }
}

/// Read `vcs.client` from `config` and return the matching implementation.
/// Defaults to `"git"` when the key is unset.
pub fn from_config(config: &Configuration) -> Result<Box<dyn Vcs>, VcsError> {
    let name = match config.get_str("vcs.client") {
        Ok(Some(s)) => s,
        Ok(None) => "git".to_owned(),
        Err(e) => {
            return Err(VcsError::BadOption {
                key: "vcs.client".to_owned(),
                detail: e.to_string(),
            });
        }
    };
    match name.as_str() {
        "git" => Ok(Box::new(GitClient::from_config(config)?)),
        other => Err(VcsError::UnknownClient(other.to_owned())),
    }
}
