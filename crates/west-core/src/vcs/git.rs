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
    CheckoutTarget, CloneKind, CloneSpec, ColorMode, CommitSummary, DiffOutcome, DiffSpec,
    FetchSpec, InitSpec, Output, ProgressSink, RevSpec, RevType, StatusMode, StatusOutcome,
    StatusSpec, SubmoduleScope, SubmoduleStrategy, Vcs, VcsError,
};

const NAME: &str = "git";

/// Where the manifest-rev pointer lives in a git repo. Plain
/// `refs/heads/<name>` rather than `refs/west/<name>` so it stays visible
/// to `git branch` and other ordinary tooling — this is the established
/// location users expect from prior west releases. Resolution detail
/// of [`RevSpec::ManifestRev`]; consumers stay typed in `RevSpec` and
/// never see this literal.
const MANIFEST_REV_REF: &str = "refs/heads/manifest-rev";

/// Scratch ref namespace used by [`GitClient::fetch`] to land a
/// revision the server won't serve directly (a bare SHA). The fetch
/// pulls every branch into `refs/west/*` and resolves the SHA from
/// there; [`GitClient::set_manifest_rev`] tears the namespace down
/// once `manifest-rev` pins the objects, so it never accumulates.
/// Internal to this client — the trait never mentions it.
const WEST_SCRATCH_REFSPEC: &str = "+refs/heads/*:refs/west/*";
const WEST_SCRATCH_PATTERN: &str = "refs/west/";

/// Resolve a [`RevSpec`] to the string git wants on the command line.
/// Single site for the git-specific encoding of `Head` / `ManifestRev`;
/// callers stay shape-agnostic.
fn resolve_rev<'a>(rev: RevSpec<'a>) -> &'a str {
    match rev {
        RevSpec::Head => "HEAD",
        RevSpec::ManifestRev => MANIFEST_REV_REF,
        RevSpec::Named(s) => s,
    }
}

/// Heuristic: could this revision string be a raw git object name
/// (SHA)? Used by `fetch` to decide whether to fetch the revision
/// directly (servers may refuse a bare SHA) or via the all-branches
/// scratch refspec. Matches v1's `_maybe_sha`: all hex, no longer
/// than a full SHA-1 (40). Deliberately permissive — a hex-looking
/// branch/tag name misclassified here just takes the (always-safe)
/// scratch path, which fetches everything and still resolves it. The
/// inverse error (a real SHA treated as a name) is the costly one, so
/// we lean toward "yes". (SHA-256's 64-char names are not covered;
/// west's object-format support is a separate concern.)
fn looks_like_sha(rev: &str) -> bool {
    !rev.is_empty() && rev.len() <= 40 && rev.bytes().all(|b| b.is_ascii_hexdigit())
}

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
    /// Sourced from `tool.git.fetch.tags`. `Some(true)` (the default
    /// when the config is unset) passes `--tags`, ensuring named tag
    /// revisions land as local refs. `Some(false)` passes `--no-tags`.
    /// Matches v1, which hardcoded `--tags`.
    pub fetch_tags: Option<bool>,
    /// Sourced from `tool.git.fetch.narrow`. When `true`, fetch the
    /// exact requested revision and nothing else: skip tags, and fetch
    /// the revision directly even when it looks like a SHA (which may
    /// fail depending on the Git host). The CLI's `--narrow` /
    /// `update.narrow` map onto this. Mirrors v1's `--narrow`.
    pub fetch_narrow: bool,
    /// Sourced from `tool.git.fetch.extra-args` (a TOML list of
    /// strings). Spliced verbatim into the `git fetch` argv before the
    /// `--` separator — a general escape hatch (shallow `--depth=N`,
    /// `--filter=blob:none`, …). The CLI's `--fetch-opt` maps onto it.
    /// Mirrors v1's `-o`/`--fetch-opt`.
    pub fetch_extra_args: Vec<String>,
    /// Sourced from `tool.git.clone.extra-args` (a TOML list of
    /// strings). Spliced verbatim into a non-mirror `git clone` argv
    /// before the `--` separator. `west init`'s `--clone-opt` maps
    /// onto it. Mirrors v1's `-o`/`--clone-opt`.
    pub clone_extra_args: Vec<String>,
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
    /// Sourced from `tool.git.submodules.init-config` (a TOML array of
    /// `KEY=VALUE` strings). Each entry is prepended as `-c KEY=VALUE` to
    /// the `git submodule update --init` invocation, mirroring v1's
    /// `--submodule-init-config` flag. The narrow scope (only the
    /// submodule-init step, not arbitrary git calls) matches v1: the
    /// motivating case is opting back into `protocol.file.allow=always`
    /// for sandboxed submodule clones without weakening other git
    /// operations.
    pub submodules_init_config: Vec<String>,
}

impl Default for GitOptions {
    fn default() -> Self {
        Self {
            binary: None,
            fetch_strategy: FetchStrategy::default(),
            fetch_tags: None,
            fetch_narrow: false,
            fetch_extra_args: Vec::new(),
            clone_extra_args: Vec::new(),
            fetch_depth: None,
            fetch_force: true,
            submodules_recurse: true,
            submodules_sync: true,
            submodules_init_config: Vec::new(),
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

        let fetch_narrow = match config.get_bool("tool.git.fetch.narrow") {
            Ok(opt) => opt.unwrap_or(false),
            Err(e) => return Err(bad_option("tool.git.fetch.narrow", &e.to_string())),
        };

        let fetch_extra_args = match config.get_list_str("tool.git.fetch.extra-args") {
            Ok(opt) => opt.unwrap_or_default(),
            Err(e) => return Err(bad_option("tool.git.fetch.extra-args", &e.to_string())),
        };

        let clone_extra_args = match config.get_list_str("tool.git.clone.extra-args") {
            Ok(opt) => opt.unwrap_or_default(),
            Err(e) => return Err(bad_option("tool.git.clone.extra-args", &e.to_string())),
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

        let submodules_init_config = match config.get_list_str("tool.git.submodules.init-config") {
            Ok(opt) => {
                let entries = opt.unwrap_or_default();
                for entry in &entries {
                    if !entry.contains('=') {
                        return Err(bad_option(
                            "tool.git.submodules.init-config",
                            &format!("each entry must be KEY=VALUE, got {entry:?}"),
                        ));
                    }
                }
                entries
            }
            Err(e) => return Err(bad_option("tool.git.submodules.init-config", &e.to_string())),
        };

        Ok(Self::new(GitOptions {
            binary,
            fetch_strategy,
            fetch_tags,
            fetch_narrow,
            fetch_extra_args,
            clone_extra_args,
            fetch_depth,
            fetch_force,
            submodules_recurse,
            submodules_sync,
            submodules_init_config,
        }))
    }

    fn binary(&self) -> &Path {
        self.opts.binary.as_deref().unwrap_or(Path::new("git"))
    }

    /// Whether to suppress tag fetching. `--narrow` implies it;
    /// otherwise honor the explicit `tool.git.fetch.tags` (which
    /// defaults to fetching tags). Shared by `clone` and `fetch` so
    /// they stay consistent.
    fn no_tags(&self) -> bool {
        self.opts.fetch_narrow || self.opts.fetch_tags == Some(false)
    }

    /// Delete every ref under `pattern` (e.g. `refs/west/` or
    /// `refs/heads/`). `update-ref -d` takes no globs, so enumerate
    /// with `for-each-ref` and drop each. Empty namespace ⇒ no-op.
    fn delete_refs_under(&self, repo: &Path, pattern: &str) -> Result<(), VcsError> {
        let repo_str = repo.to_string_lossy().into_owned();
        let res = self.run(&["-C", &repo_str, "for-each-ref", "--format=%(refname)", pattern])?;
        check_success(&res)?;
        let stdout = std::str::from_utf8(&res.output.stdout).map_err(|e| VcsError::BadOutput {
            client: NAME,
            argv: res.argv.clone(),
            detail: format!("non-UTF-8 for-each-ref output: {e}"),
        })?;
        for refname in stdout.lines().map(str::trim).filter(|l| !l.is_empty()) {
            let res = self.run(&["-C", &repo_str, "update-ref", "-d", refname])?;
            check_success(&res)?;
        }
        Ok(())
    }

    /// Post-`clone` cleanup for [`CloneKind::Managed`]: detach HEAD so
    /// the local branch git clone checked out isn't current, then drop
    /// every local branch. West owns the branch namespace via
    /// `manifest-rev` (which doesn't exist yet on a fresh clone, so
    /// deleting all of `refs/heads/` is safe here).
    fn detach_and_prune_branches(&self, repo: &Path) -> Result<(), VcsError> {
        let repo_str = repo.to_string_lossy().into_owned();
        let res = self.run(&["-C", &repo_str, "checkout", "--quiet", "--detach", "HEAD"])?;
        check_success(&res)?;
        self.delete_refs_under(repo, "refs/heads/")
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
        match spec.kind {
            CloneKind::Mirror => {
                // `--mirror` implies `--bare` and a refspec that mirrors
                // every ref under `refs/*`. `--branch` / `--origin` don't
                // apply to a bare mirror — git rejects the combination —
                // so they're ignored in this mode.
                argv.push("--mirror");
            }
            CloneKind::Working | CloneKind::Managed => {
                // `--branch` accepts branch and tag names (not bare SHAs)
                // and only matters for a working checkout. `Managed` lands
                // its revision via a follow-up fetch + checkout, so it
                // never sets `revision`.
                if matches!(spec.kind, CloneKind::Working)
                    && let Some(r) = spec.revision
                {
                    argv.extend(["--branch", r]);
                }
                if let Some(o) = spec.origin {
                    argv.extend(["--origin", o]);
                }
                // `git clone` fetches all tags by default, so a later
                // `--no-tags` fetch wouldn't undo them. When tags are
                // disabled (`--narrow` / `tool.git.fetch.tags = false`),
                // clone with `--no-tags` too — this also configures the
                // remote's tagOpt so future fetches stay tag-free.
                if self.no_tags() {
                    argv.push("--no-tags");
                }
                // Caller-supplied passthrough (`tool.git.clone.extra-args`
                // / `--clone-opt`), spliced before the `--` separator.
                // Mirror clones are internal (auto-cache) and don't take
                // it.
                for arg in &self.opts.clone_extra_args {
                    argv.push(arg);
                }
            }
        }
        // `--` to be explicit about argv boundaries.
        argv.push("--");
        argv.push(spec.url);
        argv.push(&dest_str);
        self.run_with_output(&argv, out)?;

        // West-managed clones own no local branches: detach HEAD and
        // drop the branch git clone left behind (west tracks the
        // checked-out revision via manifest-rev instead).
        if matches!(spec.kind, CloneKind::Managed) {
            self.detach_and_prune_branches(spec.dest)?;
        }
        Ok(())
    }

    fn init(&self, spec: &InitSpec<'_>) -> Result<(), VcsError> {
        let dest_str = spec.dest.to_string_lossy().into_owned();
        // `-c init.defaultBranch=…` silences the "Using 'master' as the
        // name…" advice on every git version (the flag `--initial-branch`
        // is 2.28+, but the config key predates it). The placeholder
        // branch is unborn and we never commit on it — the network flow
        // fetches then checks out a detached HEAD — so it never
        // materialises as a real ref.
        let res = self.run(&[
            "-c",
            "init.defaultBranch=west-init",
            "init",
            "--",
            &dest_str,
        ])?;
        check_success(&res)?;
        if let Some(origin) = spec.origin {
            let res = self.run(&["-C", &dest_str, "remote", "add", "--", origin, spec.url])?;
            check_success(&res)?;
        }
        Ok(())
    }

    fn sha(&self, repo: &Path, rev: RevSpec<'_>) -> Result<String, VcsError> {
        let repo_str = repo.to_string_lossy().into_owned();
        let qualified = format!("{rev}^{{commit}}", rev = resolve_rev(rev));
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

    fn rev_type(&self, repo: &Path, rev: RevSpec<'_>) -> Result<RevType, VcsError> {
        // Two-step, mirrors v1's `_rev_type`:
        //   1. `git cat-file -t <rev>` — annotated tags come back as
        //      `tag`; lightweight tags, branches, and SHAs all come
        //      back as `commit`. Blob/tree are valid git objects but
        //      not commit-ish — not what fetch callers want.
        //   2. For `commit`, `git rev-parse --verify --symbolic-full-name`
        //      tells us whether the user named a branch (`refs/heads/…`),
        //      a lightweight tag (`refs/tags/…`), or a raw SHA (empty
        //      output — there's no symbolic name for it).
        // Non-zero exits at step 1 (unresolvable rev) or step 2
        // (ambiguous: same name as both a tag and a branch, etc.) map
        // to `Other`, which the fetch callers treat as "don't skip".
        let repo_str = repo.to_string_lossy().into_owned();
        let rev_str = resolve_rev(rev);
        let res = self.run(&["-C", &repo_str, "cat-file", "-t", rev_str])?;
        if !res.output.status.success() {
            return Ok(RevType::Other);
        }
        let kind = std::str::from_utf8(&res.output.stdout)
            .map_err(|e| VcsError::BadOutput {
                client: NAME,
                argv: res.argv.clone(),
                detail: format!("non-UTF-8 cat-file output: {e}"),
            })?
            .trim();
        match kind {
            "tag" => return Ok(RevType::Tag),
            "commit" => {}
            // blob/tree/other: not commit-ish; let the caller fetch
            // (it'll fail loudly if the rev is genuinely garbage).
            _ => return Ok(RevType::Other),
        }
        let res = self.run(&[
            "-C",
            &repo_str,
            "rev-parse",
            "--verify",
            "--symbolic-full-name",
            rev_str,
        ])?;
        if !res.output.status.success() {
            return Ok(RevType::Other);
        }
        let full = std::str::from_utf8(&res.output.stdout)
            .map_err(|e| VcsError::BadOutput {
                client: NAME,
                argv: res.argv.clone(),
                detail: format!("non-UTF-8 rev-parse output: {e}"),
            })?
            .trim();
        if full.starts_with("refs/heads/") || full.starts_with("refs/remotes/") {
            Ok(RevType::Branch)
        } else if full.starts_with("refs/tags/") {
            // Lightweight tags land here (annotated tags exited at step 1).
            Ok(RevType::Tag)
        } else if full.is_empty() {
            // No symbolic name — a raw SHA or expression like `HEAD~2`.
            Ok(RevType::Commit)
        } else {
            Ok(RevType::Other)
        }
    }

    fn ls_tree_at_ref(
        &self,
        repo: &Path,
        rev: RevSpec<'_>,
        relative_path: &Path,
    ) -> Result<Option<Vec<String>>, VcsError> {
        let repo_str = repo.to_string_lossy().into_owned();
        let spec = format!(
            "{rev}:{}",
            relative_path.to_string_lossy(),
            rev = resolve_rev(rev)
        );
        // Two-step: first `cat-file -t` to disambiguate tree vs blob
        // vs missing (git stderrs vary by version, but the type-probe
        // is unambiguous); then `ls-tree --name-only` if it's a tree.
        let probe = self.run(&["-C", &repo_str, "cat-file", "-t", &spec])?;
        if !probe.output.status.success() {
            // Missing ref or path — soft-fail. Use the same stderr
            // pattern set as `read_at_ref`; real failures bubble up.
            let stderr = String::from_utf8_lossy(&probe.output.stderr);
            let lower = stderr.to_ascii_lowercase();
            if lower.contains("not a valid object name")
                || lower.contains("invalid object name")
                || lower.contains("does not exist")
                || lower.contains("unknown revision")
                || lower.contains("ambiguous argument")
            {
                return Ok(None);
            }
            return Err(VcsError::CommandFailed {
                client: NAME,
                argv: probe.argv,
                exit_code: probe.output.status.code(),
                stderr: stderr.into_owned(),
            });
        }
        let kind = std::str::from_utf8(&probe.output.stdout)
            .map(str::trim)
            .map_err(|e| VcsError::BadOutput {
                client: NAME,
                argv: probe.argv.clone(),
                detail: format!("cat-file -t non-UTF-8 stdout: {e}"),
            })?;
        if kind != "tree" {
            // Blob (or anything else): the caller treats this as
            // "not a directory" and falls back to `read_at_ref`.
            return Ok(None);
        }
        // `ls-tree --name-only <rev>:<path>` writes one name per line.
        // Use the same `<rev>:<path>` spec as the probe; `name-only`
        // strips the mode/type/sha columns. NUL-terminated mode (`-z`)
        // would be safer for filenames with newlines but git's natural
        // line-mode is sufficient for manifest YAML names.
        let res = self.run(&["-C", &repo_str, "ls-tree", "--name-only", &spec])?;
        check_success(&res)?;
        let stdout = std::str::from_utf8(&res.output.stdout).map_err(|e| VcsError::BadOutput {
            client: NAME,
            argv: res.argv.clone(),
            detail: format!("ls-tree non-UTF-8 stdout: {e}"),
        })?;
        Ok(Some(
            stdout
                .lines()
                .map(str::trim_end)
                .filter(|l| !l.is_empty())
                .map(str::to_owned)
                .collect(),
        ))
    }

    fn read_at_ref(
        &self,
        repo: &Path,
        rev: RevSpec<'_>,
        relative_path: &Path,
    ) -> Result<Option<Vec<u8>>, VcsError> {
        let repo_str = repo.to_string_lossy().into_owned();
        let spec = format!(
            "{rev}:{}",
            relative_path.to_string_lossy(),
            rev = resolve_rev(rev)
        );
        // `cat-file -p <rev>:<path>` writes the raw object contents to
        // stdout. Equivalent to v1's `git show <ref>:<path>` for the
        // blob case and explicit about "bytes please" — no smudge,
        // pager, or diff machinery in the way.
        let res = self.run(&["-C", &repo_str, "cat-file", "-p", &spec])?;
        if res.output.status.success() {
            return Ok(Some(res.output.stdout));
        }
        // Distinguish "ref/path absent" (soft) from real failures.
        // git uses exit-128 for both, so we match on the stderr
        // signature instead. The set of patterns mirrors what we see
        // from the binary across versions on missing-revision and
        // missing-path errors.
        let stderr = String::from_utf8_lossy(&res.output.stderr);
        // `git cat-file -p`'s stderr for missing objects varies by git
        // version and whether the ref or the path is the missing half.
        // The case-insensitive substring set below covers all the
        // wordings observed across recent git releases.
        let lower = stderr.to_ascii_lowercase();
        if lower.contains("not a valid object name")
            || lower.contains("invalid object name")
            || lower.contains("does not exist")
            || lower.contains("unknown revision")
            || lower.contains("ambiguous argument")
        {
            return Ok(None);
        }
        check_success(&res)?;
        // `check_success` returned Ok on a non-zero status with an
        // unrecognized stderr — treat as a real failure rather than
        // a silent miss.
        Err(VcsError::CommandFailed {
            client: NAME,
            argv: res.argv,
            exit_code: res.output.status.code(),
            stderr: stderr.into_owned(),
        })
    }

    fn is_ancestor(
        &self,
        repo: &Path,
        ancestor: RevSpec<'_>,
        descendant: RevSpec<'_>,
    ) -> Result<bool, VcsError> {
        // `git merge-base --is-ancestor A B` exits 0 if A is ancestor of B,
        // 1 if not, and >1 on real errors (bad ref, etc.).
        let repo_str = repo.to_string_lossy().into_owned();
        let res = self.run(&[
            "-C",
            &repo_str,
            "merge-base",
            "--is-ancestor",
            resolve_rev(ancestor),
            resolve_rev(descendant),
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
        // Smart strategy: skip the fetch when the manifest pins an
        // immutable revision the local repo already resolves. Mirrors
        // v1's `set_new_manifest_rev` gate (`not in ('tag', 'commit')`):
        // branches and unknown refs always fetch; tags and SHAs short-
        // circuit. The classification is authoritative — it asks git
        // via `cat-file -t` / `rev-parse --symbolic-full-name`, not a
        // string-shape heuristic, so an all-hex branch name like
        // `cafebabe` correctly returns `Branch` and we still fetch.
        if matches!(self.opts.fetch_strategy, FetchStrategy::Smart)
            && let Some(rev) = spec.revision
            && matches!(self.rev_type(repo, RevSpec::Named(rev))?, RevType::Tag | RevType::Commit)
            && let Ok(sha) = self.sha(repo, RevSpec::Named(rev))
        {
            // v1's `dbg('skipping unnecessary fetch')` — the smart
            // strategy short-circuited because the pinned immutable
            // revision is already resolvable locally. Prefix with the
            // repo dir's basename (usually the project's name) for
            // provenance without dragging the full workspace path
            // into every line.
            log::debug!("{}: skipping unnecessary fetch for {rev}", repo_label(repo));
            return Ok(sha);
        }

        let repo_str = repo.to_string_lossy().into_owned();
        let depth_arg = self.opts.fetch_depth.map(|d| format!("--depth={d}"));

        // Refspec strategy:
        //   - No revision: default refspec; resolve FETCH_HEAD.
        //   - SHA-shaped revision, not narrow: many hosts (GitHub,
        //     …) refuse to serve a bare SHA, so instead fetch every
        //     branch into the scratch namespace `refs/west/*` (tags
        //     come along via --tags) and hope the SHA is reachable;
        //     resolve the SHA directly afterwards. `set_manifest_rev`
        //     tears the scratch refs down once manifest-rev pins the
        //     objects. This is the init+fetch path's substitute for
        //     "clone brought the default branch down first".
        //   - Otherwise (branch / tag, or narrow): fetch the revision
        //     directly and resolve FETCH_HEAD.
        let use_scratch = match spec.revision {
            Some(rev) => !self.opts.fetch_narrow && looks_like_sha(rev),
            None => false,
        };

        let mut argv: Vec<&str> = vec!["-C", &repo_str, "fetch", "--progress"];
        if self.opts.fetch_force {
            argv.push("--force");
        }
        // v1 always passed `--tags` so that a manifest revision like
        // `v2.0` lands as a local tag ref, not just FETCH_HEAD. Match
        // that as the default; `--narrow` / `tool.git.fetch.tags =
        // false` opts out.
        argv.push(if self.no_tags() { "--no-tags" } else { "--tags" });
        if let Some(d) = depth_arg.as_deref() {
            argv.push(d);
        }
        // Caller-supplied passthrough (`tool.git.fetch.extra-args` /
        // `--fetch-opt`), spliced verbatim before the `--` separator.
        for arg in &self.opts.fetch_extra_args {
            argv.push(arg);
        }
        argv.push("--");
        argv.push(spec.remote);
        if use_scratch {
            argv.push(WEST_SCRATCH_REFSPEC);
        } else if let Some(rev) = spec.revision {
            argv.push(rev);
        }
        // v1's `small_banner(f'{name}: fetching, need revision {rev}')`.
        // No project name at this layer (we operate on a repo path), so
        // the repo dir's basename prefixes the message — usually the
        // project name, short enough to keep `-vv` readable when N
        // projects fetch in parallel. DEBUG (not INFO) so `-v` stays
        // quiet at default volume.
        if let Some(rev) = spec.revision {
            log::debug!("{}: fetching, need revision {rev}", repo_label(repo));
        }
        self.run_with_output(&argv, out)?;

        match (use_scratch, spec.revision) {
            // Scratch fetch: FETCH_HEAD is ambiguous (one entry per
            // branch), but the SHA is now reachable from refs/west/*,
            // so resolve it directly.
            (true, Some(rev)) => self.sha(repo, RevSpec::Named(rev)),
            // After an active fetch with a positional ref, FETCH_HEAD
            // is the just-fetched tip — the canonical sha for the
            // requested revision. For a default-refspec fetch
            // (revision: None) it's the merge-target line.
            _ => self.sha(repo, RevSpec::Named("FETCH_HEAD")),
        }
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

    fn rebase(
        &self,
        repo: &Path,
        onto: RevSpec<'_>,
        out: &mut Output<'_>,
    ) -> Result<(), VcsError> {
        let repo_str = repo.to_string_lossy().into_owned();
        self.run_with_output(&["-C", &repo_str, "rebase", resolve_rev(onto)], out)
    }

    fn is_clean(&self, repo: &Path) -> Result<bool, VcsError> {
        let repo_str = repo.to_string_lossy().into_owned();
        let res = self.run(&["-C", &repo_str, "status", "--porcelain"])?;
        check_success(&res)?;
        Ok(res.output.stdout.iter().all(|b| b.is_ascii_whitespace()))
    }

    fn head_branch(&self, repo: &Path) -> Result<Option<String>, VcsError> {
        let repo_str = repo.to_string_lossy().into_owned();
        // `symbolic-ref --short -q HEAD` names the branch HEAD is on
        // (born OR unborn) and exits 1 with no output when HEAD is
        // detached. Preferred over `rev-parse --abbrev-ref HEAD`,
        // which errors on an unborn HEAD (fresh `init`) instead of
        // reporting the branch.
        let res = self.run(&["-C", &repo_str, "symbolic-ref", "--short", "-q", "HEAD"])?;
        match res.output.status.code() {
            Some(0) => {}
            // Detached HEAD: no branch.
            Some(1) => return Ok(None),
            _ => return Err(make_command_failed(&res)),
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
        // An unborn branch (fresh init, no commit yet) has no history
        // to keep or rebase onto — report None so callers detach.
        let verify = self.run(&["-C", &repo_str, "rev-parse", "--verify", "--quiet", "HEAD"])?;
        if !verify.output.status.success() {
            return Ok(None);
        }
        Ok(Some(trimmed.to_owned()))
    }

    fn update_submodules(
        &self,
        repo: &Path,
        scope: &SubmoduleScope<'_>,
        strategy: SubmoduleStrategy,
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
        let mut argv: Vec<&str> = vec!["-C", &repo_str];
        // `tool.git.submodules.init-config` entries land here as
        // `-c KEY=VALUE` (between `-C <repo>` and the `submodule`
        // verb), matching v1's `--submodule-init-config` semantics:
        // applies only to the `submodule update --init` call, not to
        // `submodule sync` or any other git invocation.
        for entry in &self.opts.submodules_init_config {
            argv.push("-c");
            argv.push(entry);
        }
        argv.extend(["submodule", "update", "--init", "--progress"]);
        // v1 mirrors `west update -r` into the inner `git submodule
        // update --rebase` so local commits in submodule worktrees
        // survive a re-update. `--checkout` is the git default; emit
        // it explicitly so the intent is visible in transcripts.
        match strategy {
            SubmoduleStrategy::Checkout => argv.push("--checkout"),
            SubmoduleStrategy::Rebase => argv.push("--rebase"),
        }
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
        check_success(&res)?;
        // Tidy the scratch namespace `fetch` uses to land bare-SHA
        // revisions. manifest-rev now pins those objects, so dropping
        // `refs/west/*` can't lose them. No-op when the fetch took the
        // direct refspec (the namespace is empty).
        self.delete_refs_under(repo, WEST_SCRATCH_PATTERN)
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

    fn commit_summary(&self, repo: &Path, rev: RevSpec<'_>) -> Result<CommitSummary, VcsError> {
        let repo_str = repo.to_string_lossy().into_owned();
        // %h = abbreviated sha (git decides the width based on
        // collision risk); %x09 = literal TAB; %s = subject line.
        // TAB is a safe separator because git collapses newlines and
        // tabs out of `%s`.
        let res = self.run(&[
            "-C",
            &repo_str,
            "log",
            "-1",
            "--format=%h%x09%s",
            resolve_rev(rev),
        ])?;
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
            args.push(resolve_rev(from).to_owned());
        }
        if let Some(to) = spec.to_rev {
            args.push(resolve_rev(to).to_owned());
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

/// Short human label for a repo path — its basename, falling back to
/// the full display when the path has no terminating component
/// (root, `..`-only). Used as the prefix on per-fetch log lines so a
/// parallel `west update -vv` stays readable without dragging the
/// full workspace path into every diagnostic.
fn repo_label(repo: &std::path::Path) -> std::borrow::Cow<'_, str> {
    match repo.file_name() {
        Some(name) => name.to_string_lossy(),
        None => repo.to_string_lossy(),
    }
}

