//! VCS abstraction.
//!
//! `Vcs` is the trait every implementation must satisfy; concrete
//! implementations are *clients* of the underlying VCS tool (`GitClient`,
//! eventually `JjClient`, …). The trait surface stays minimal: each operation
//! is something every plausible client can do.
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

use std::error::Error;
use std::fmt;
use std::path::{Path, PathBuf};

use crate::config::Configuration;

mod git;

pub use git::{GitClient, GitOptions};

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
    /// follow-up checkout (which will arrive when `update` lands).
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
