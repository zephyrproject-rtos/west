//! Git client for the [`Vcs`](super::Vcs) trait. Subprocess-based; no
//! libgit2 dependency.

use std::ffi::OsString;
use std::io;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use crate::config::Configuration;

use super::{Vcs, VcsError};

const NAME: &str = "git";

#[derive(Debug)]
pub struct GitClient {
    opts: GitOptions,
}

#[derive(Debug, Default, Clone)]
pub struct GitOptions {
    /// Path to the `git` executable. Defaults to `"git"` (PATH lookup).
    /// Sourced from the `tool.git.binary` config key.
    pub binary: Option<PathBuf>,
}

impl GitClient {
    pub fn new(opts: GitOptions) -> Self {
        Self { opts }
    }

    pub fn from_config(config: &Configuration) -> Result<Self, VcsError> {
        let binary = match config.get_str("tool.git.binary") {
            Ok(s) => s.map(PathBuf::from),
            Err(e) => {
                return Err(VcsError::BadOption {
                    key: "tool.git.binary".to_owned(),
                    detail: e.to_string(),
                });
            }
        };
        Ok(Self::new(GitOptions { binary }))
    }

    fn binary(&self) -> &Path {
        self.opts.binary.as_deref().unwrap_or(Path::new("git"))
    }

    /// Run `git` with `args`. stdout/stderr are captured. The current
    /// directory is inherited (git's `-C <dir>` is the right way to change it).
    fn run(&self, args: &[&str]) -> Result<RunResult, VcsError> {
        let output = Command::new(self.binary())
            .args(args)
            .stdin(Stdio::null())
            .output()
            .map_err(|e| io_to_err(e, NAME))?;
        Ok(RunResult {
            argv: argv_strings(args),
            output,
        })
    }

    /// Run `git` with stdout/stderr inherited from the parent. Used by
    /// `clone` so the user sees git's progress output.
    fn run_inherit(&self, args: &[&str]) -> Result<(), VcsError> {
        let status = Command::new(self.binary())
            .args(args)
            .stdin(Stdio::null())
            .status()
            .map_err(|e| io_to_err(e, NAME))?;
        if status.success() {
            Ok(())
        } else {
            Err(VcsError::CommandFailed {
                client: NAME,
                argv: argv_strings(args),
                exit_code: status.code(),
                // No captured stderr — git wrote it directly to the terminal.
                stderr: String::new(),
            })
        }
    }
}

impl Vcs for GitClient {
    fn name(&self) -> &'static str {
        NAME
    }

    fn is_repo(&self, path: &Path) -> Result<bool, VcsError> {
        // `git -C <path> rev-parse --show-cdup` exits 0 when path is in a
        // working tree; the cdup is empty when path *is* the worktree root.
        // Non-zero exit => not a repo (not an error).
        if !path.exists() {
            return Ok(false);
        }
        let path_str = path.to_string_lossy().into_owned();
        let res = self.run(&["-C", &path_str, "rev-parse", "--show-cdup"])?;
        Ok(res.output.status.success())
    }

    fn clone(
        &self,
        url: &str,
        dest: &Path,
        revision: Option<&str>,
        origin: Option<&str>,
    ) -> Result<(), VcsError> {
        let dest_str = dest.to_string_lossy().into_owned();
        let mut argv: Vec<&str> = vec!["clone"];
        // `git clone --branch` accepts branch and tag names. Bare commit SHAs
        // aren't supported here; landing on one requires a follow-up checkout.
        if let Some(r) = revision {
            argv.extend(["--branch", r]);
        }
        if let Some(o) = origin {
            argv.extend(["--origin", o]);
        }
        // `--` to be explicit about argv boundaries.
        argv.push("--");
        argv.push(url);
        argv.push(&dest_str);
        self.run_inherit(&argv)
    }

    fn sha(&self, repo: &Path, rev: &str) -> Result<String, VcsError> {
        let repo_str = repo.to_string_lossy().into_owned();
        let qualified = format!("{rev}^{{commit}}");
        let res = self.run(&["-C", &repo_str, "rev-parse", &qualified])?;
        check_success(&res)?;
        let stdout = std::str::from_utf8(&res.output.stdout).map_err(|e| VcsError::BadOutput {
            client: NAME,
            argv: res.argv.clone(),
            detail: format!("non-UTF-8 stdout: {e}"),
        })?;
        let trimmed = stdout.trim();
        if trimmed.is_empty() {
            return Err(VcsError::BadOutput {
                client: NAME,
                argv: res.argv,
                detail: "rev-parse returned empty output".to_owned(),
            });
        }
        Ok(trimmed.to_owned())
    }

    fn is_ancestor(&self, repo: &Path, ancestor: &str, descendant: &str) -> Result<bool, VcsError> {
        // `git merge-base --is-ancestor A B` exits 0 if A is ancestor of B,
        // 1 if not, and >1 on real errors (bad ref, etc.).
        let repo_str = repo.to_string_lossy().into_owned();
        let res = self.run(&[
            "-C",
            &repo_str,
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
        ])?;
        match res.output.status.code() {
            Some(0) => Ok(true),
            Some(1) => Ok(false),
            _ => Err(make_command_failed(&res)),
        }
    }
}

// ---------- helpers ----------

struct RunResult {
    argv: Vec<String>,
    output: std::process::Output,
}

fn argv_strings(args: &[&str]) -> Vec<String> {
    let mut out = Vec::with_capacity(args.len() + 1);
    out.push("git".to_owned());
    out.extend(args.iter().map(|s| (*s).to_owned()));
    out
}

fn io_to_err(e: io::Error, client: &'static str) -> VcsError {
    if e.kind() == io::ErrorKind::NotFound {
        VcsError::ClientUnavailable { client, source: e }
    } else {
        // Use a placeholder path; this is reached for spawn failures that
        // aren't NotFound (rare on modern systems).
        VcsError::Io {
            path: PathBuf::from(OsString::from("git")),
            source: e,
        }
    }
}

fn check_success(res: &RunResult) -> Result<(), VcsError> {
    if res.output.status.success() {
        Ok(())
    } else {
        Err(make_command_failed(res))
    }
}

fn make_command_failed(res: &RunResult) -> VcsError {
    VcsError::CommandFailed {
        client: NAME,
        argv: res.argv.clone(),
        exit_code: res.output.status.code(),
        stderr: String::from_utf8_lossy(&res.output.stderr).into_owned(),
    }
}
