//! Git client for the [`Vcs`](super::Vcs) trait. Subprocess-based; no
//! libgit2 dependency.

mod progress;

use std::ffi::OsString;
use std::io::{self, BufReader, Read};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Mutex;

use crate::config::Configuration;

use super::{
    CheckoutTarget, CloneSpec, ColorMode, CommitSummary, DiffOutcome, DiffSpec, FetchSpec, Output,
    ProgressSink, StatusMode, StatusOutcome, StatusSpec, SubmoduleScope, Vcs, VcsError,
};

const NAME: &str = "git";

/// Where the manifest-rev pointer lives in a git repo. Plain
/// `refs/heads/<name>` rather than `refs/west/<name>` so it stays visible
/// to `git branch` and other ordinary tooling — this is the established
/// location users expect from prior west releases.
const MANIFEST_REV_REF: &str = "refs/heads/manifest-rev";

#[derive(Debug)]
pub struct GitClient {
    opts: GitOptions,
}

#[derive(Debug, Clone)]
pub struct GitOptions {
    /// Path to the `git` executable. Defaults to `"git"` (PATH lookup).
    /// Sourced from the `tool.git.binary` config key.
    pub binary: Option<PathBuf>,
    /// Sourced from `tool.git.fetch.strategy`. Default: `Smart`.
    pub fetch_strategy: FetchStrategy,
    /// Sourced from `tool.git.fetch.tags`. `Some(true)` passes `--tags`,
    /// `Some(false)` passes `--no-tags`, `None` leaves the flag off (git
    /// applies its own default — fetch tags reachable from fetched commits).
    pub fetch_tags: Option<bool>,
    /// Sourced from `tool.git.fetch.depth`. When set, fetches are shallow
    /// to that depth via `--depth=N`.
    pub fetch_depth: Option<u32>,
    /// Sourced from `tool.git.fetch.force`. Default `true` — multi-remote
    /// repos otherwise silently fail to advance their tracking refs when the
    /// manifest revision points at a different commit than the one already
    /// fetched.
    pub fetch_force: bool,
    /// Sourced from `tool.git.submodules.recurse`. Default `true`. Drives
    /// `--recursive` on `git submodule update` and `git submodule sync`.
    pub submodules_recurse: bool,
    /// Sourced from `tool.git.submodules.sync`. Default `true`. When `true`,
    /// runs `git submodule sync` before `git submodule update`.
    pub submodules_sync: bool,
}

impl Default for GitOptions {
    fn default() -> Self {
        Self {
            binary: None,
            fetch_strategy: FetchStrategy::default(),
            fetch_tags: None,
            fetch_depth: None,
            fetch_force: true,
            submodules_recurse: true,
            submodules_sync: true,
        }
    }
}

/// Strategy for [`Vcs::fetch`](super::Vcs::fetch). `Smart` skips the network
/// call when the requested revision is already resolvable locally; `Always`
/// fetches unconditionally.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub enum FetchStrategy {
    #[default]
    Smart,
    Always,
}

impl GitClient {
    pub fn new(opts: GitOptions) -> Self {
        Self { opts }
    }

    pub fn from_config(config: &Configuration) -> Result<Self, VcsError> {
        let binary = match config.get_str("tool.git.binary") {
            Ok(s) => s.map(PathBuf::from),
            Err(e) => return Err(bad_option("tool.git.binary", &e.to_string())),
        };

        let fetch_strategy = match config.get_str("tool.git.fetch.strategy") {
            Ok(None) => FetchStrategy::default(),
            Ok(Some(s)) => match s.as_str() {
                "smart" => FetchStrategy::Smart,
                "always" => FetchStrategy::Always,
                other => {
                    return Err(bad_option(
                        "tool.git.fetch.strategy",
                        &format!("expected \"smart\" or \"always\", got {other:?}"),
                    ));
                }
            },
            Err(e) => return Err(bad_option("tool.git.fetch.strategy", &e.to_string())),
        };

        let fetch_tags = match config.get_bool("tool.git.fetch.tags") {
            Ok(opt) => opt,
            Err(e) => return Err(bad_option("tool.git.fetch.tags", &e.to_string())),
        };

        let fetch_depth = match config.get("tool.git.fetch.depth") {
            Ok(None) => None,
            Ok(Some(crate::config::ConfigValue::Integer(i))) => {
                if i <= 0 {
                    return Err(bad_option(
                        "tool.git.fetch.depth",
                        &format!("must be a positive integer, got {i}"),
                    ));
                }
                Some(u32::try_from(i).map_err(|_| {
                    bad_option("tool.git.fetch.depth", &format!("must fit in u32, got {i}"))
                })?)
            }
            Ok(Some(other)) => {
                return Err(bad_option(
                    "tool.git.fetch.depth",
                    &format!("expected integer, got {other:?}"),
                ));
            }
            Err(e) => return Err(bad_option("tool.git.fetch.depth", &e.to_string())),
        };

        let fetch_force = match config.get_bool("tool.git.fetch.force") {
            Ok(opt) => opt.unwrap_or(true),
            Err(e) => return Err(bad_option("tool.git.fetch.force", &e.to_string())),
        };

        let submodules_recurse = match config.get_bool("tool.git.submodules.recurse") {
            Ok(opt) => opt.unwrap_or(true),
            Err(e) => return Err(bad_option("tool.git.submodules.recurse", &e.to_string())),
        };

        let submodules_sync = match config.get_bool("tool.git.submodules.sync") {
            Ok(opt) => opt.unwrap_or(true),
            Err(e) => return Err(bad_option("tool.git.submodules.sync", &e.to_string())),
        };

        Ok(Self::new(GitOptions {
            binary,
            fetch_strategy,
            fetch_tags,
            fetch_depth,
            fetch_force,
            submodules_recurse,
            submodules_sync,
        }))
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

    /// Run `git` for one of the long-lived ops (`clone`, `fetch`,
    /// `rebase`, `submodule update`) routing stdio according to `out`.
    ///
    /// `Output::Native` attaches the child's stdio to the parent so git
    /// can detect a TTY and render its own progress with `\r`-overwrite.
    /// `Output::Stream(sink)` pipes stderr (and stdout, in case rebase
    /// or submodule diagnostics land there), spawns a reader thread per
    /// stream that translates each line through the [`progress`] parser,
    /// and pushes events into the sink.
    fn run_with_output(&self, args: &[&str], out: &mut Output<'_>) -> Result<(), VcsError> {
        match out {
            Output::Native => {
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
                        // git wrote stderr to the terminal directly; we
                        // didn't capture it.
                        stderr: String::new(),
                    })
                }
            }
            Output::Stream(sink) => self.run_streaming(args, *sink),
        }
    }

    /// Spawn `git` with `Stdio::piped()` for stdout and stderr, run two
    /// reader threads (one per stream) that parse each line via
    /// [`progress::parse_line`] and push events through the supplied
    /// sink, then `wait()` for the child and emit `Finished` on success.
    fn run_streaming(&self, args: &[&str], sink: &mut dyn ProgressSink) -> Result<(), VcsError> {
        let mut child = Command::new(self.binary())
            .args(args)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| io_to_err(e, NAME))?;

        let stdout = child.stdout.take().expect("piped stdout");
        let stderr = child.stderr.take().expect("piped stderr");

        // Capture stderr verbatim too — used for the CommandFailed
        // diagnostic if the child exits non-zero. The reader thread
        // appends to this buffer alongside emitting events, so we have
        // a faithful transcript without re-running.
        let stderr_buf: Mutex<Vec<u8>> = Mutex::new(Vec::new());
        let shared_sink: Mutex<&mut dyn ProgressSink> = Mutex::new(sink);

        std::thread::scope(|scope| {
            scope.spawn(|| {
                stream_reader(stderr, &shared_sink, Some(&stderr_buf));
            });
            scope.spawn(|| {
                stream_reader(stdout, &shared_sink, None);
            });
        });

        let status = child.wait().map_err(|e| io_to_err(e, NAME))?;
        if !status.success() {
            let stderr =
                String::from_utf8_lossy(&stderr_buf.into_inner().unwrap_or_default()).into_owned();
            return Err(VcsError::CommandFailed {
                client: NAME,
                argv: argv_strings(args),
                exit_code: status.code(),
                stderr,
            });
        }

        // Notify the sink that the op completed cleanly. Implementations
        // typically use this to clear/finalise the visual.
        let mut sink_guard = shared_sink.lock().expect("sink mutex poisoned");
        sink_guard.event(super::ProgressEvent::Finished);
        Ok(())
    }
}

/// Read `stream` line-by-line, push parsed events through `sink`, and
/// (optionally) tee bytes into `verbatim` for fault-time diagnostics.
fn stream_reader<R: Read + Send>(
    stream: R,
    sink: &Mutex<&mut dyn ProgressSink>,
    verbatim: Option<&Mutex<Vec<u8>>>,
) {
    // git emits progress via `\r` rewrites on the same logical line.
    // Split on either `\r` or `\n` so we see each frame; the parser
    // strips the trailing chars anyway.
    let reader = BufReader::new(stream);
    let mut line_buf = Vec::with_capacity(256);
    for byte in reader.bytes() {
        let Ok(byte) = byte else { break };
        if let Some(buf) = verbatim
            && let Ok(mut b) = buf.lock()
        {
            b.push(byte);
        }
        match byte {
            b'\n' | b'\r' => {
                if !line_buf.is_empty() {
                    flush_line(&line_buf, sink);
                    line_buf.clear();
                }
            }
            other => line_buf.push(other),
        }
    }
    if !line_buf.is_empty() {
        flush_line(&line_buf, sink);
    }
}

fn flush_line(line: &[u8], sink: &Mutex<&mut dyn ProgressSink>) {
    let s = String::from_utf8_lossy(line);
    let events = progress::parse_line(&s);
    if events.is_empty() {
        return;
    }
    let mut guard = match sink.lock() {
        Ok(g) => g,
        Err(_) => return,
    };
    for event in events {
        guard.event(event);
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

    fn clone(&self, spec: &CloneSpec<'_>, out: &mut Output<'_>) -> Result<(), VcsError> {
        let dest_str = spec.dest.to_string_lossy().into_owned();
        let mut argv: Vec<&str> = vec!["clone", "--progress"];
        if spec.mirror {
            // `--mirror` implies `--bare` and a refspec that mirrors every
            // ref under `refs/*`. `--branch` / `--origin` don't apply to a
            // bare mirror — git rejects the combination — so we ignore
            // them in this mode.
            argv.push("--mirror");
        } else {
            // `git clone --branch` accepts branch and tag names. Bare commit
            // SHAs aren't supported here; landing on one requires a follow-up
            // checkout.
            if let Some(r) = spec.revision {
                argv.extend(["--branch", r]);
            }
            if let Some(o) = spec.origin {
                argv.extend(["--origin", o]);
            }
        }
        // `--` to be explicit about argv boundaries.
        argv.push("--");
        argv.push(spec.url);
        argv.push(&dest_str);
        self.run_with_output(&argv, out)
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

    fn fetch(
        &self,
        repo: &Path,
        spec: &FetchSpec<'_>,
        out: &mut Output<'_>,
    ) -> Result<String, VcsError> {
        // Smart strategy: if we're being asked for a specific revision and
        // it's already resolvable here, no need to talk to the network. The
        // caller resolves moving refs (branches) to their tip before calling
        // us, so a hit here means the commit really is current.
        if matches!(self.opts.fetch_strategy, FetchStrategy::Smart)
            && let Some(rev) = spec.revision
            && let Ok(sha) = self.sha(repo, rev)
        {
            log::trace!("git: smart fetch skipped for {rev:?} (already local)");
            return Ok(sha);
        }

        let repo_str = repo.to_string_lossy().into_owned();
        let depth_arg = self.opts.fetch_depth.map(|d| format!("--depth={d}"));

        let mut argv: Vec<&str> = vec!["-C", &repo_str, "fetch", "--progress"];
        if self.opts.fetch_force {
            argv.push("--force");
        }
        match self.opts.fetch_tags {
            Some(true) => argv.push("--tags"),
            Some(false) => argv.push("--no-tags"),
            None => {}
        }
        if let Some(d) = depth_arg.as_deref() {
            argv.push(d);
        }
        argv.push("--");
        argv.push(spec.remote);
        if let Some(rev) = spec.revision {
            argv.push(rev);
        }
        self.run_with_output(&argv, out)?;
        // After an active fetch with a positional ref, `FETCH_HEAD` is the
        // just-fetched tip — that's the canonical sha for the requested
        // revision. For a default-refspec fetch (`revision: None`),
        // `FETCH_HEAD`'s merge-target line is what callers get.
        self.sha(repo, "FETCH_HEAD")
    }

    fn checkout(
        &self,
        repo: &Path,
        target: &CheckoutTarget<'_>,
        out: &mut Output<'_>,
    ) -> Result<(), VcsError> {
        let repo_str = repo.to_string_lossy().into_owned();
        match *target {
            CheckoutTarget::Detached(rev) => self.run_with_output(
                &[
                    "-C",
                    &repo_str,
                    // Suppress the long detached-HEAD advice text — west is the
                    // tool, the user isn't running git directly here.
                    "-c",
                    "advice.detachedHead=false",
                    "checkout",
                    "--detach",
                    rev,
                ],
                out,
            ),
            CheckoutTarget::Branch(name) => {
                self.run_with_output(&["-C", &repo_str, "checkout", name], out)
            }
        }
    }

    fn rebase(&self, repo: &Path, onto: &str, out: &mut Output<'_>) -> Result<(), VcsError> {
        let repo_str = repo.to_string_lossy().into_owned();
        self.run_with_output(&["-C", &repo_str, "rebase", onto], out)
    }

    fn is_clean(&self, repo: &Path) -> Result<bool, VcsError> {
        let repo_str = repo.to_string_lossy().into_owned();
        let res = self.run(&["-C", &repo_str, "status", "--porcelain"])?;
        check_success(&res)?;
        Ok(res.output.stdout.iter().all(|b| b.is_ascii_whitespace()))
    }

    fn head_branch(&self, repo: &Path) -> Result<Option<String>, VcsError> {
        let repo_str = repo.to_string_lossy().into_owned();
        let res = self.run(&["-C", &repo_str, "rev-parse", "--abbrev-ref", "HEAD"])?;
        check_success(&res)?;
        let stdout = std::str::from_utf8(&res.output.stdout).map_err(|e| VcsError::BadOutput {
            client: NAME,
            argv: res.argv.clone(),
            detail: format!("non-UTF-8 stdout: {e}"),
        })?;
        let trimmed = stdout.trim();
        // git emits the literal "HEAD" when HEAD is detached.
        if trimmed.is_empty() || trimmed == "HEAD" {
            Ok(None)
        } else {
            Ok(Some(trimmed.to_owned()))
        }
    }

    fn update_submodules(
        &self,
        repo: &Path,
        scope: &SubmoduleScope<'_>,
        reference: Option<&Path>,
        out: &mut Output<'_>,
    ) -> Result<(), VcsError> {
        // Empty Specific scope is an explicit no-op (caller may have
        // collected an empty list from manifest-driven filtering).
        if let SubmoduleScope::Specific(paths) = scope
            && paths.is_empty()
        {
            return Ok(());
        }

        let repo_str = repo.to_string_lossy().into_owned();

        if self.opts.submodules_sync {
            let mut argv: Vec<&str> = vec!["-C", &repo_str, "submodule", "sync"];
            if self.opts.submodules_recurse {
                argv.push("--recursive");
            }
            if let SubmoduleScope::Specific(paths) = scope {
                argv.push("--");
                argv.extend(paths.iter().copied());
            }
            self.run_with_output(&argv, out)?;
        }

        let reference_str = reference.map(|p| p.to_string_lossy().into_owned());
        let mut argv: Vec<&str> = vec![
            "-C",
            &repo_str,
            "submodule",
            "update",
            "--init",
            "--progress",
        ];
        if self.opts.submodules_recurse {
            argv.push("--recursive");
        }
        if let Some(r) = reference_str.as_deref() {
            argv.extend(["--reference", r]);
        }
        if let SubmoduleScope::Specific(paths) = scope {
            argv.push("--");
            argv.extend(paths.iter().copied());
        }
        self.run_with_output(&argv, out)
    }

    fn set_remote_url(&self, repo: &Path, remote: &str, url: &str) -> Result<(), VcsError> {
        let repo_str = repo.to_string_lossy().into_owned();
        let res = self.run(&["-C", &repo_str, "remote", "set-url", remote, url])?;
        check_success(&res)
    }

    fn set_manifest_rev(
        &self,
        repo: &Path,
        sha: &str,
        reason: Option<&str>,
    ) -> Result<(), VcsError> {
        let repo_str = repo.to_string_lossy().into_owned();
        let mut argv: Vec<&str> = vec!["-C", &repo_str, "update-ref"];
        if let Some(msg) = reason {
            argv.extend(["-m", msg]);
        }
        argv.extend([MANIFEST_REV_REF, sha]);
        let res = self.run(&argv)?;
        check_success(&res)
    }

    fn manifest_rev(&self, repo: &Path) -> Result<Option<String>, VcsError> {
        let repo_str = repo.to_string_lossy().into_owned();
        // `--verify` makes rev-parse fail (rather than echo back the literal
        // arg) when the ref is missing.
        let res = self.run(&[
            "-C",
            &repo_str,
            "rev-parse",
            "--verify",
            "--quiet",
            MANIFEST_REV_REF,
        ])?;
        if !res.output.status.success() {
            // `--quiet` makes rev-parse exit 1 with empty stderr when the ref
            // doesn't exist. Anything else is a real error.
            if res.output.status.code() == Some(1) && res.output.stderr.is_empty() {
                return Ok(None);
            }
            return Err(make_command_failed(&res));
        }
        let stdout = std::str::from_utf8(&res.output.stdout).map_err(|e| VcsError::BadOutput {
            client: NAME,
            argv: res.argv.clone(),
            detail: format!("non-UTF-8 stdout: {e}"),
        })?;
        let trimmed = stdout.trim();
        if trimmed.is_empty() {
            return Ok(None);
        }
        Ok(Some(trimmed.to_owned()))
    }

    fn commit_summary(&self, repo: &Path, rev: &str) -> Result<CommitSummary, VcsError> {
        let repo_str = repo.to_string_lossy().into_owned();
        // %h = abbreviated sha (git decides the width based on
        // collision risk); %x09 = literal TAB; %s = subject line.
        // TAB is a safe separator because git collapses newlines and
        // tabs out of `%s`.
        let res = self.run(&["-C", &repo_str, "log", "-1", "--format=%h%x09%s", rev])?;
        check_success(&res)?;
        let stdout = std::str::from_utf8(&res.output.stdout).map_err(|e| VcsError::BadOutput {
            client: NAME,
            argv: res.argv.clone(),
            detail: format!("non-UTF-8 stdout: {e}"),
        })?;
        let line = stdout.lines().next().unwrap_or("");
        let (short, subject) = line.split_once('\t').ok_or_else(|| VcsError::BadOutput {
            client: NAME,
            argv: res.argv.clone(),
            detail: format!("expected `<short>\\t<subject>`, got {line:?}"),
        })?;
        Ok(CommitSummary {
            short_sha: short.to_owned(),
            subject: subject.to_owned(),
        })
    }

    fn diff(
        &self,
        repo: &Path,
        spec: &DiffSpec<'_>,
        writer: &mut dyn std::io::Write,
    ) -> Result<DiffOutcome, VcsError> {
        let repo_str = repo.to_string_lossy().into_owned();
        // Build the argv: `-C <repo> diff --exit-code [--color=…]
        // [--src-prefix=… --dst-prefix=…] [from] [to] -- [extra]`.
        // `--exit-code` is what gives us the empty/non-empty split
        // via the process exit code.
        let mut args: Vec<String> = vec![
            "-C".to_owned(),
            repo_str,
            "diff".to_owned(),
            "--exit-code".to_owned(),
        ];
        match spec.color {
            ColorMode::Always => args.push("--color=always".to_owned()),
            ColorMode::Never => args.push("--color=never".to_owned()),
            // Auto = no flag; git applies its own heuristic. Note that
            // capturing stdout to a pipe (our case) makes git's
            // heuristic decide "never" — callers that want color in
            // a captured buffer should pass `Always` explicitly.
            ColorMode::Auto => {}
        }
        if let Some(prefix) = spec.path_prefix {
            args.push(format!("--src-prefix={prefix}/"));
            args.push(format!("--dst-prefix={prefix}/"));
        }
        if let Some(from) = spec.from_rev {
            args.push(from.to_owned());
        }
        if let Some(to) = spec.to_rev {
            args.push(to.to_owned());
        }
        // Forward extras inline rather than after a `--` separator.
        // git's `--` ends the option list and treats everything
        // after as pathspecs — but our callers typically pass diff
        // FLAGS (`--stat`, `-w`, `--name-only`). Users who want
        // pathspecs supply the `--` themselves inside extra_args.
        if !spec.extra_args.is_empty() {
            args.extend(spec.extra_args.iter().cloned());
        }

        let arg_refs: Vec<&str> = args.iter().map(String::as_str).collect();
        let res = self.run(&arg_refs)?;

        // git's --exit-code: 0 = no diff, 1 = diff present, ≥2 = error.
        let outcome = match res.output.status.code() {
            Some(0) => DiffOutcome::Empty,
            Some(1) => DiffOutcome::NonEmpty,
            Some(code) => {
                return Err(VcsError::CommandFailed {
                    client: NAME,
                    argv: res.argv,
                    exit_code: Some(code),
                    stderr: String::from_utf8_lossy(&res.output.stderr).into_owned(),
                });
            }
            None => {
                return Err(VcsError::CommandFailed {
                    client: NAME,
                    argv: res.argv,
                    exit_code: None,
                    stderr: String::from_utf8_lossy(&res.output.stderr).into_owned(),
                });
            }
        };

        // Write the diff body even for `Empty` — stdout will be
        // empty in that case, so this is a zero-byte write, which
        // simplifies the contract for callers (the writer is always
        // populated with whatever stdout produced).
        writer.write_all(&res.output.stdout).map_err(|source| VcsError::Io {
            path: repo.to_path_buf(),
            source,
        })?;
        Ok(outcome)
    }

    fn status(
        &self,
        repo: &Path,
        spec: &StatusSpec<'_>,
        writer: &mut dyn std::io::Write,
    ) -> Result<StatusOutcome, VcsError> {
        let repo_str = repo.to_string_lossy().into_owned();

        // Detection: porcelain v1 is stable across git versions and
        // always produces empty stdout on a clean tree. One call,
        // unambiguous Clean/Dirty result.
        let porcelain = self.run(&["-C", &repo_str, "status", "--porcelain=v1"])?;
        check_success(&porcelain)?;
        let outcome = if porcelain.output.stdout.is_empty() {
            StatusOutcome::Clean
        } else {
            StatusOutcome::Dirty
        };

        // Display: run `git status [-s]` separately so it honours the
        // user's color preference. We deliberately don't reuse the
        // porcelain output for Short mode display — porcelain is
        // intentionally colorless, but `git status -s` colours the
        // status letters interactively (M/A/D/?). Users at the
        // terminal expect that colouring.
        //
        // `git status` doesn't accept `--color=…` directly the way
        // `git diff` does (verified by tests). The canonical way to
        // force colour on/off is the top-level `-c color.status=…`
        // (and `color.ui=…` for older gits).
        let mut args: Vec<String> = Vec::new();
        match spec.color {
            ColorMode::Always => {
                args.extend([
                    "-c".to_owned(),
                    "color.status=always".to_owned(),
                    "-c".to_owned(),
                    "color.ui=always".to_owned(),
                ]);
            }
            ColorMode::Never => {
                args.extend([
                    "-c".to_owned(),
                    "color.status=never".to_owned(),
                    "-c".to_owned(),
                    "color.ui=never".to_owned(),
                ]);
            }
            ColorMode::Auto => {}
        }
        args.extend(["-C".to_owned(), repo_str, "status".to_owned()]);
        if matches!(spec.mode, StatusMode::Short) {
            args.push("-s".to_owned());
        }
        if !spec.extra_args.is_empty() {
            args.extend(spec.extra_args.iter().cloned());
        }
        let arg_refs: Vec<&str> = args.iter().map(String::as_str).collect();
        let display = self.run(&arg_refs)?;
        check_success(&display)?;
        writer
            .write_all(&display.output.stdout)
            .map_err(|source| VcsError::Io {
                path: repo.to_path_buf(),
                source,
            })?;

        Ok(outcome)
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

fn bad_option(key: &str, detail: &str) -> VcsError {
    VcsError::BadOption {
        key: key.to_owned(),
        detail: detail.to_owned(),
    }
}
