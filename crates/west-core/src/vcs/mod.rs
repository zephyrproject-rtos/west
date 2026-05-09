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

use std::fmt;
use std::io::Write;
use std::path::{Path, PathBuf};

use crate::config::Configuration;

mod git;

pub use git::{FetchStrategy, GitClient, GitOptions};

/// Where progress output from a long-lived child process goes.
///
/// `Native` attaches the child's stdout/stderr to the parent process —
/// the underlying tool renders its own progress directly to the user's
/// terminal. The caller doesn't get the bytes back. Use this for serial,
/// single-operation flows where parsing adds no value.
///
/// `Stream(sink)` requests a piped, line-by-line stream parsed into
/// [`ProgressEvent`]s. Each implementation is expected to coax progress
/// out of the underlying tool (for git that means injecting `--progress`
/// so it emits even with stdio piped). Use this for parallel flows or
/// any caller that wants to render a progress UI.
pub enum Output<'a> {
    Native,
    Stream(&'a mut dyn ProgressSink),
}

impl fmt::Debug for Output<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Output::Native => f.write_str("Output::Native"),
            Output::Stream(_) => f.write_str("Output::Stream(<sink>)"),
        }
    }
}

/// A single progress observation produced by a [`Vcs`] implementation.
///
/// The trait is intentionally narrow: it expresses the union of what
/// real VCS tools tell us during a long-running op, in a shape the CLI
/// can render uniformly. Each concrete client (subprocess git, future
/// libgit2-via-`git2`, jj, …) translates its native progress mechanism
/// into these events; the consumer never has to know which client is
/// running.
#[derive(Debug, Clone)]
pub enum ProgressEvent<'a> {
    /// Free-form line that didn't match a recognised progress pattern —
    /// banners ("Cloning into …"), error messages, "From <url>" lines,
    /// etc. Sinks usually preserve these verbatim for replay on failure.
    Line(&'a str),
    /// Phase boundary. `total` is `Some(n)` when the client announces an
    /// up-front object/byte count; `None` otherwise.
    Phase { name: &'a str, total: Option<u64> },
    /// In-flight tick for the current phase. `done` is monotonically
    /// non-decreasing within a phase; `total` may grow (server-side
    /// discovery during fetch).
    Tick { done: u64, total: Option<u64> },
    /// The op finished successfully. Sinks typically clear/finalise.
    Finished,
}

/// Consumer of [`ProgressEvent`]s emitted by [`Output::Stream`].
///
/// Implementations are called from a reader thread inside the client, so
/// the trait is `Send`. Interior synchronisation is the implementation's
/// responsibility (typically not needed — the trait method takes
/// `&mut self`, so each invocation has exclusive access).
pub trait ProgressSink: Send {
    fn event(&mut self, event: ProgressEvent<'_>);
}

/// Drops every event. The "be quiet" sink — used by tests that don't
/// assert on output and by internal client work that shouldn't surface
/// to the user (e.g. resolving a per-project import behind the scenes).
pub struct NullSink;

impl ProgressSink for NullSink {
    fn event(&mut self, _event: ProgressEvent<'_>) {}
}

/// Formats events back to text in a writer. Used by reporters that want
/// a deterministic per-project transcript (no live UI), e.g. when stderr
/// isn't a TTY and indicatif degrades to nothing.
pub struct LineSink<W: Write + Send> {
    writer: W,
}

impl<W: Write + Send> LineSink<W> {
    pub fn new(writer: W) -> Self {
        Self { writer }
    }

    pub fn into_inner(self) -> W {
        self.writer
    }
}

impl<W: Write + Send> ProgressSink for LineSink<W> {
    fn event(&mut self, event: ProgressEvent<'_>) {
        // Best-effort: a stderr/buffer write failure here is unrecoverable
        // and doesn't usefully propagate; just drop.
        let _ = match event {
            ProgressEvent::Line(s) => writeln!(self.writer, "{s}"),
            ProgressEvent::Phase { name, total: None } => {
                writeln!(self.writer, "{name}:")
            }
            ProgressEvent::Phase {
                name,
                total: Some(t),
            } => writeln!(self.writer, "{name}: 0/{t}"),
            ProgressEvent::Tick { done, total: None } => {
                writeln!(self.writer, "  {done}")
            }
            ProgressEvent::Tick {
                done,
                total: Some(t),
            } => writeln!(self.writer, "  {done}/{t}"),
            ProgressEvent::Finished => Ok(()),
        };
    }
}

/// Operations every VCS client supports.
///
/// The `Send + Sync` bound lets callers share a `Box<dyn Vcs>` across
/// threads (e.g. `west update -j N`); concrete clients hold no shared
/// mutable state.
///
/// Methods that produce user-visible progress take a `&mut Output<'_>`.
/// `Native` keeps the underlying tool's stdio attached to the parent
/// (the tool renders its own progress); `Stream(sink)` pipes stderr
/// through a parser into structured [`ProgressEvent`]s. Lookups (`sha`,
/// `is_repo`, …) don't produce progress and don't take an `Output`.
pub trait Vcs: fmt::Debug + Send + Sync {
    /// Client identifier (`"git"`, `"jj"`, …). Stable; surfaces in errors.
    fn name(&self) -> &'static str;

    /// `true` if `path` is a working copy of this VCS.
    fn is_repo(&self, path: &Path) -> Result<bool, VcsError>;

    /// Clone `spec.url` into `spec.dest`. Progress output (git's "Cloning
    /// into …", "Receiving objects: …") is forwarded to `out`. See
    /// [`CloneSpec`] for the full set of clone parameters.
    fn clone(&self, spec: &CloneSpec<'_>, out: &mut Output<'_>) -> Result<(), VcsError>;

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
    ///
    /// Returns the commit SHA that the requested revision now resolves to —
    /// `FETCH_HEAD^{commit}` after an active fetch, or the locally-resolved
    /// revision when smart-skip kicked in. Callers use this directly as the
    /// new `manifest-rev` rather than re-reading `FETCH_HEAD`, which would
    /// be stale on the smart-skip path.
    fn fetch(
        &self,
        repo: &Path,
        spec: &FetchSpec<'_>,
        out: &mut Output<'_>,
    ) -> Result<String, VcsError>;

    /// Move HEAD in `repo` to `target`.
    ///
    /// `Detached(rev)` lands HEAD on the commit without binding it to a
    /// branch — the safe default for updates. `Branch(name)` switches to an
    /// existing local branch.
    fn checkout(&self, repo: &Path, target: &CheckoutTarget<'_>) -> Result<(), VcsError>;

    /// Rebase the current branch in `repo` onto `onto`. Fails if the
    /// rebase has conflicts; the working tree is left in whatever state the
    /// underlying tool leaves it. Progress output is forwarded to `out`.
    fn rebase(&self, repo: &Path, onto: &str, out: &mut Output<'_>) -> Result<(), VcsError>;

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
    /// `reference`, when `Some`, names a local repository whose object
    /// store is shared with the submodule clone — git: `--reference
    /// <path>`. Used by the cache-aware `west update` flow to point
    /// submodule init at a pre-populated mirror so the network round-
    /// trips disappear. Applies once per call, so callers wanting
    /// per-submodule references invoke `update_submodules` per submodule.
    ///
    /// Clients that don't have a submodule concept should return a
    /// [`VcsError::CommandFailed`] when invoked on a non-empty scope; for
    /// `Specific(&[])` the call must be a no-op so callers can pass through
    /// an empty manifest list without branching.
    fn update_submodules(
        &self,
        repo: &Path,
        scope: &SubmoduleScope<'_>,
        reference: Option<&Path>,
        out: &mut Output<'_>,
    ) -> Result<(), VcsError>;

    /// Rewrite `repo`'s `<remote>` URL to `url`. After a cache-driven
    /// clone (where the original URL pointed at a local mirror), the
    /// caller flips the recorded URL to the project's real upstream so
    /// subsequent fetches reach the network.
    fn set_remote_url(&self, repo: &Path, remote: &str, url: &str) -> Result<(), VcsError>;

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

/// What to clone.
///
/// `revision` is a branch, tag, or — where the client supports it — a
/// commit reference; each client interprets it as the user would expect
/// when passing it to the underlying tool. `origin` overrides the remote
/// name (default `"origin"` for git).
///
/// Git note: `revision` is passed to `git clone --branch`, which accepts
/// branch and tag names only. Landing at a bare commit SHA requires a
/// follow-up [`Vcs::checkout`].
///
/// `mirror = true` produces a bare mirror clone (`git clone --mirror`),
/// suitable as a reference cache for subsequent normal clones from
/// `dest`. In mirror mode `revision` and `origin` are ignored — they
/// don't apply to a `--mirror` clone.
#[derive(Debug, Clone, Copy)]
pub struct CloneSpec<'a> {
    pub url: &'a str,
    pub dest: &'a Path,
    pub revision: Option<&'a str>,
    pub origin: Option<&'a str>,
    pub mirror: bool,
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
#[derive(Debug, thiserror::Error)]
pub enum VcsError {
    /// The client tool returned a non-zero exit code. Captures the argv and
    /// stderr for diagnostics.
    #[error(
        "{client} command failed (exit={}): {}{}{}",
        match exit_code { Some(c) => c.to_string(), None => "?".to_owned() },
        argv.join(" "),
        if stderr.trim().is_empty() { "" } else { ": " },
        stderr.trim(),
    )]
    CommandFailed {
        client: &'static str,
        argv: Vec<String>,
        exit_code: Option<i32>,
        stderr: String,
    },
    /// The client tool isn't on PATH or couldn't be executed.
    #[error("{client} client is unavailable: {source}")]
    ClientUnavailable {
        client: &'static str,
        #[source]
        source: std::io::Error,
    },
    /// File-system I/O error around a repo or working tree.
    #[error("io error on {}: {source}", path.display())]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    /// Output from the client tool didn't match the expected shape.
    #[error("{client} produced unexpected output for `{}`: {detail}", argv.join(" "))]
    BadOutput {
        client: &'static str,
        argv: Vec<String>,
        detail: String,
    },
    /// `vcs.client` named a client we don't recognize.
    #[error("unknown vcs.client: {0:?}")]
    UnknownClient(String),
    /// A `tool.<client>.<key>` config value had the wrong type or shape.
    #[error("bad option {key:?}: {detail}")]
    BadOption { key: String, detail: String },
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
