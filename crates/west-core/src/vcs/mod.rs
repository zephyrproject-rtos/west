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
use std::io;
use std::path::{Path, PathBuf};

use crate::config::Configuration;

mod git;

pub use git::{FetchStrategy, GitClient, GitOptions};

/// Operations every VCS client supports.
///
/// The `Send + Sync` bound lets callers share a `Box<dyn Vcs>` across
/// threads (e.g. `west update -j N`); concrete clients hold no shared
/// mutable state.
///
/// Methods that produce user-visible progress take a `&mut dyn io::Write`
/// for that progress; the implementation forwards captured stderr/stdout
/// to it, and the caller decides where the bytes land (terminal, per-task
/// buffer, indicatif progress bar, …). Lookups (`sha`, `is_repo`, …) don't
/// produce progress and don't take a writer.
pub trait Vcs: fmt::Debug + Send + Sync {
    /// Client identifier (`"git"`, `"jj"`, …). Stable; surfaces in errors.
    fn name(&self) -> &'static str;

    /// `true` if `path` is a working copy of this VCS.
    fn is_repo(&self, path: &Path) -> Result<bool, VcsError>;

    /// Clone `url` into `dest`. `revision` is a branch, tag, or — where the
    /// client supports it — a commit reference; each client interprets it as
    /// the user would expect when passing it to the underlying tool.
    /// `origin` overrides the remote name (default `"origin"` for git).
    /// Progress output (git's "Cloning into …", "Receiving objects: …") is
    /// forwarded to `out`.
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
        out: &mut dyn io::Write,
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
    /// at client construction. Progress output is forwarded to `out`.
    fn fetch(
        &self,
        repo: &Path,
        spec: &FetchSpec<'_>,
        out: &mut dyn io::Write,
    ) -> Result<(), VcsError>;

    /// Move HEAD in `repo` to `target`.
    ///
    /// `Detached(rev)` lands HEAD on the commit without binding it to a
    /// branch — the safe default for updates. `Branch(name)` switches to an
    /// existing local branch.
    fn checkout(&self, repo: &Path, target: &CheckoutTarget<'_>) -> Result<(), VcsError>;

    /// Rebase the current branch in `repo` onto `onto`. Fails if the
    /// rebase has conflicts; the working tree is left in whatever state the
    /// underlying tool leaves it. Progress output is forwarded to `out`.
    fn rebase(&self, repo: &Path, onto: &str, out: &mut dyn io::Write) -> Result<(), VcsError>;

    /// `true` when `repo`'s working tree has no uncommitted changes.
    fn is_clean(&self, repo: &Path) -> Result<bool, VcsError>;

    /// The branch HEAD currently points at. `Ok(None)` when HEAD is
    /// detached (or otherwise not on a branch).
    fn head_branch(&self, repo: &Path) -> Result<Option<String>, VcsError>;

    /// Materialize the submodules in `repo`. `scope` selects all submodules
    /// or a specific list (paths within the repo). Behavior knobs (recursion,
    /// pre-update sync) live in `tool.<client>.submodules.*` config. Progress
    /// output is forwarded to `out`.
    ///
    /// Clients that don't have a submodule concept should return a
    /// [`VcsError::CommandFailed`] when invoked on a non-empty scope; for
    /// `Specific(&[])` the call must be a no-op so callers can pass through
    /// an empty manifest list without branching.
    fn update_submodules(
        &self,
        repo: &Path,
        scope: &SubmoduleScope<'_>,
        out: &mut dyn io::Write,
    ) -> Result<(), VcsError>;

    /// Record `sha` as the manifest-rev of `repo`. `reason`, if given, is
    /// recorded with the underlying ref operation so users can inspect why
    /// the pointer moved (git: shows up in `git reflog refs/heads/manifest-rev`).
    /// Implementations choose where the pointer is stored.
    fn set_manifest_rev(
        &self,
        repo: &Path,
        sha: &str,
        reason: Option<&str>,
    ) -> Result<(), VcsError>;

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

/// Which submodules to act on in [`Vcs::update_submodules`].
#[derive(Debug, Clone, Copy)]
pub enum SubmoduleScope<'a> {
    /// All submodules declared in the repo.
    All,
    /// A specific list of submodule paths (relative to the repo root). An
    /// empty slice is a no-op.
    Specific(&'a [&'a str]),
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
