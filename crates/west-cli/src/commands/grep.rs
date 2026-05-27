//! `west grep` — run a grep-like tool in each cloned project.
//!
//! Three tools are supported:
//!
//! - `git-grep` (default) — runs `git grep` from the project's
//!   working tree. The git binary comes from the same
//!   `tool.git.binary` config the VCS layer reads.
//! - `ripgrep` — `rg` (falls back to `ripgrep` on PATH).
//! - `grep` — system grep with `--recursive` prepended.
//!
//! Selection precedence mirrors v1:
//!   1. `--tool / -t {git-grep,ripgrep,grep}`
//!   2. `grep.tool` config
//!   3. `git-grep` default
//!
//! Per-tool binary path:
//!   1. `--tool-path PATH`
//!   2. `grep.{tool}-path` config
//!   3. for git-grep: `tool.git.binary` (vcs layer)
//!   4. for ripgrep / grep: `which("rg"|"ripgrep"|"grep")`
//!
//! Per-tool default args:
//!   - `grep.{tool}-args` config (shlex-split), else
//!   - builtin defaults (`--recursive` for `grep`; empty otherwise)
//!
//! Output ordering: parallel execution + manifest-order drain.
//! The `=== name (path):` banner is chrome and goes to stderr;
//! matched lines (the result) go to stdout, like `grep -r`, so
//! `west grep … > f` stays banner-free. `-q` suppresses the banner.
//! Exit-code semantics from v1: 1 = no match (silent skip),
//! 0 = match (emit body), anything else = error (record + summary).
//!
//! Pass-through: everything after `--` is forwarded verbatim to the
//! tool. Same shape as `diff` / `status`.
//! `west grep -- pattern file1 file2` ⇒ `git grep ... -- pattern file1 file2`.

use std::io::{self, IsTerminal, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};

use clap::{Args, ValueEnum};
use rayon::prelude::*;

use west_core::config::{ConfigValue, Configuration};
use west_core::manifest::Project;

use super::config::LoadedConfig;
use super::select;

const MAX_DEFAULT_JOBS: usize = 8;

#[derive(Args, Debug)]
pub struct GrepArgs {
    /// Grep tool. Overrides `grep.tool` config; default `git-grep`.
    #[arg(short = 't', long, value_enum)]
    pub tool: Option<Tool>,

    /// Path to the tool's executable. Overrides per-tool config
    /// (`grep.{tool}-path`) and the built-in lookup.
    #[arg(long, value_name = "PATH")]
    pub tool_path: Option<PathBuf>,

    /// Color preference. Forwarded as `--color={always,never,auto}`
    /// to the tool (all three accept that form). Default sources:
    /// `grep.color` config, then `color.ui`, then "auto".
    #[arg(long, value_enum)]
    pub color: Option<ColorArg>,

    /// Project to grep (repeatable). Default: every active cloned
    /// project. Unlike `forall` / `diff`, `grep` does not take projects
    /// positionally — the positional space is the pattern + tool args.
    #[arg(short = 'p', long = "project", value_name = "PROJECT", action = clap::ArgAction::Append)]
    pub projects: Vec<String>,

    /// Maximum projects to grep concurrently. Twin of `grep.jobs`
    /// config; defaults to `min(num_cpus, 8)`.
    #[arg(short = 'j', long, value_name = "N")]
    pub jobs: Option<usize>,

    /// Pattern + flags forwarded verbatim to the grep tool. `west grep
    /// FOO` searches for `FOO`. Use `--` for flag-like tool args:
    /// `west grep -- -i foo` runs `git grep -i foo`. A second `--`
    /// (e.g. `west grep -- -- -needle`) reaches the tool unchanged so
    /// it can terminate its own flag parsing.
    #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
    pub tool_args: Vec<String>,
}

#[derive(ValueEnum, Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tool {
    /// `git grep` (the default).
    #[value(name = "git-grep")]
    GitGrep,
    /// `ripgrep` (`rg`).
    Ripgrep,
    /// System `grep`.
    Grep,
}

impl Tool {
    fn key(self) -> &'static str {
        match self {
            Tool::GitGrep => "git-grep",
            Tool::Ripgrep => "ripgrep",
            Tool::Grep => "grep",
        }
    }
}

#[derive(ValueEnum, Clone, Copy, Debug, PartialEq, Eq)]
pub enum ColorArg {
    Always,
    Never,
    Auto,
}

impl ColorArg {
    fn as_str(self) -> &'static str {
        match self {
            ColorArg::Always => "always",
            ColorArg::Never => "never",
            ColorArg::Auto => "auto",
        }
    }
}

#[derive(Debug, thiserror::Error)]
enum GrepError {
    #[error("not inside a west workspace (no .west/ found)")]
    NotInWorkspace,
    #[error("{0}")]
    Config(String),
    #[error("{0}")]
    Manifest(String),
    #[error("{0}")]
    Vcs(String),
    #[error("grep tool {tool:?} not found, please use --tool-path")]
    ToolNotFound { tool: &'static str },
}

impl From<super::workspace::WorkspaceError> for GrepError {
    fn from(e: super::workspace::WorkspaceError) -> Self {
        match e {
            super::workspace::WorkspaceError::NotInWorkspace => GrepError::NotInWorkspace,
            super::workspace::WorkspaceError::Config(s) => GrepError::Config(s),
            super::workspace::WorkspaceError::Manifest(s) => GrepError::Manifest(s),
            super::workspace::WorkspaceError::Vcs(s) => GrepError::Vcs(s),
        }
    }
}

pub fn run(args: GrepArgs, loaded: &mut LoadedConfig) -> ExitCode {
    if let Err(e) = splice_flags_into_config(&args, &mut loaded.config) {
        log::error!("{e}");
        return ExitCode::from(2);
    }
    match run_inner(args, loaded) {
        Ok(Outcome::Ok) => ExitCode::SUCCESS,
        Ok(Outcome::SomeFailed) => ExitCode::FAILURE,
        Err(e @ GrepError::ToolNotFound { .. }) => {
            log::error!("{e}");
            ExitCode::from(2)
        }
        Err(e) => {
            log::error!("{e}");
            ExitCode::FAILURE
        }
    }
}

enum Outcome {
    Ok,
    SomeFailed,
}

fn run_inner(args: GrepArgs, loaded: &LoadedConfig) -> Result<Outcome, GrepError> {
    let workspace = super::workspace::resolve_workspace_dir()?;
    let (loaded_manifest, vcs, _skipped) =
        super::workspace::load_manifest_resolved(workspace.as_path(), &loaded.config)?;
    let manifest = &loaded_manifest.manifest;

    // Resolve tool selection: --tool > grep.tool > GitGrep.
    let tool = match args.tool {
        Some(t) => t,
        None => match loaded
            .config
            .get_str("grep.tool")
            .map_err(|e| GrepError::Config(e.to_string()))?
            .as_deref()
        {
            Some("git-grep") | None => Tool::GitGrep,
            Some("ripgrep") => Tool::Ripgrep,
            Some("grep") => Tool::Grep,
            Some(other) => {
                return Err(GrepError::Config(format!(
                    "grep.tool: unknown value {other:?} (expected git-grep, ripgrep, or grep)"
                )));
            }
        },
    };
    let tool_path = resolve_tool_path(tool, args.tool_path.as_deref(), &loaded.config)?;
    let tool_args = build_tool_args(tool, args.color, &args.tool_args, &loaded.config)?;

    // Synthetic manifest project — same shape as forall/list: include
    // it in the default sweep, and route `manifest` / `<self.path>`
    // through `-p` to it.
    let synthetic_path = super::workspace::manifest_path_from_config(&loaded.config)?;
    let synthetic = select::synthetic_manifest_project(manifest, synthetic_path);

    // Project selection (v1: `_cloned_projects(args, only_active=not
    // args.projects)`):
    //   no `-p`     → every active project + synthetic
    //   `-p NAME…`  → only those, no activity filter
    // Both paths then drop uncloned projects silently; an unknown name
    // surfaces from `select_projects`.
    let candidates: Vec<&Project> = if args.projects.is_empty() {
        let mut acc: Vec<&Project> = Vec::new();
        if loaded_manifest.is_active(&synthetic, &[]) {
            acc.push(&synthetic);
        }
        acc.extend(
            manifest
                .projects
                .iter()
                .filter(|p| loaded_manifest.is_active(p, &[])),
        );
        acc
    } else {
        let manifest_path_str = synthetic.path.to_string_lossy().into_owned();
        let normalized: Vec<String> = args
            .projects
            .iter()
            .map(|s| super::workspace::normalize_project_selector(s, &workspace))
            .collect();
        let (synthetic_hits, leftover): (Vec<_>, Vec<_>) = normalized
            .iter()
            .partition(|s| s.as_str() == select::SYNTHETIC_NAME || s.as_str() == manifest_path_str);
        let mut acc: Vec<&Project> = Vec::new();
        if !synthetic_hits.is_empty() {
            acc.push(&synthetic);
        }
        if !leftover.is_empty() {
            let leftover: Vec<&str> = leftover.iter().map(|s| s.as_str()).collect();
            acc.extend(
                select::select_projects(&loaded_manifest, &leftover, &[])
                    .map_err(|e| GrepError::Manifest(e.to_string()))?,
            );
        }
        acc
    };

    // Silent skip of uncloned projects matches v1's `_cloned_projects`
    // for both selection paths.
    let projects: Vec<&Project> = candidates
        .into_iter()
        .filter(|p| {
            let abs = workspace.join(&p.path);
            super::workspace::is_cloned(vcs.as_ref(), &abs)
        })
        .collect();

    if projects.is_empty() {
        log::warn!("grep: no projects matched");
        return Ok(Outcome::Ok);
    }

    let settings = Settings::from_config(&loaded.config).map_err(GrepError::Config)?;
    // `output.raw` forces serial execution (same gate diff / status /
    // compare / forall use). Useful when the user wants live stdio
    // pass-through rather than per-project buffered drain — and the
    // natural override when N-way interleaving isn't worth it.
    let jobs = if settings.raw {
        1
    } else {
        settings.jobs.min(projects.len())
    };

    // Buffered parallel execution. Order = manifest order (par_iter
    // + collect preserves input order); we don't want completion-
    // order for grep — users scanning matches expect a stable layout.
    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(jobs)
        .build()
        .map_err(|e| GrepError::Config(format!("failed to start worker pool: {e}")))?;

    let outcomes: Vec<GrepOutcome> = pool.install(|| {
        projects
            .par_iter()
            .map(|p| grep_one(p, &workspace, &tool_path, &tool_args))
            .collect()
    });

    // Drain in manifest order. v1 exit-code semantics:
    //   1   → no match: skip entirely (no banner, no body).
    //   0   → match: banner (chrome) to stderr, matched lines to stdout.
    //   else→ tool failure: banner + the tool's stderr to stderr, record.
    // Matched lines on stdout (like `grep -r`) keeps `west grep … > f`
    // free of banner noise.
    let stdout = io::stdout();
    let stderr = io::stderr();
    let mut out_lock = stdout.lock();
    let mut err_lock = stderr.lock();
    // Per-project failures are collected here and logged after the
    // stderr lock is dropped — logging while holding the lock would
    // deadlock against the logger's own writer.
    let mut failed: Vec<(String, String)> = Vec::new();
    for o in outcomes {
        match o.classify() {
            BodyOutcome::Skip => {}
            BodyOutcome::Match => {
                if !settings.quiet {
                    // Flush stdout first so the banner heads its matches
                    // when both streams share a terminal.
                    let _ = out_lock.flush();
                    let _ = writeln!(
                        err_lock,
                        "=== {} ({}):",
                        o.project.name,
                        o.project.path.display()
                    );
                    let _ = err_lock.flush();
                }
                let _ = out_lock.write_all(&o.stdout);
            }
            BodyOutcome::Failure(why) => {
                if !settings.quiet {
                    let _ = out_lock.flush();
                    let _ = writeln!(
                        err_lock,
                        "=== {} ({}):",
                        o.project.name,
                        o.project.path.display()
                    );
                }
                // The tool's own stderr is part of the per-project
                // output block; flush it while we still own the lock
                // so the upcoming `log::error!` line lands below.
                let _ = err_lock.write_all(&o.stderr);
                failed.push((o.project.name.clone(), why));
            }
        }
    }
    drop(out_lock);
    drop(err_lock);

    if !failed.is_empty() {
        for (name, why) in &failed {
            log::error!("grep: {name} failed: {why}");
        }
        let names: Vec<&str> = failed.iter().map(|(n, _)| n.as_str()).collect();
        log::error!(
            "grep failed for {} project{}: {}",
            names.len(),
            if names.len() == 1 { "" } else { "s" },
            names.join(", "),
        );
        return Ok(Outcome::SomeFailed);
    }
    Ok(Outcome::Ok)
}

struct GrepOutcome<'a> {
    project: &'a Project,
    stdout: Vec<u8>,
    stderr: Vec<u8>,
    /// `Some(code)` from the spawned process; `None` when the spawn
    /// itself failed (binary not executable / not found at runtime).
    exit_code: Option<i32>,
    spawn_err: Option<String>,
}

enum BodyOutcome {
    Skip,
    Match,
    Failure(String),
}

impl GrepOutcome<'_> {
    fn classify(&self) -> BodyOutcome {
        if let Some(err) = &self.spawn_err {
            return BodyOutcome::Failure(err.clone());
        }
        match self.exit_code {
            Some(0) => BodyOutcome::Match,
            Some(1) => BodyOutcome::Skip,
            Some(code) => BodyOutcome::Failure(format!("exit code {code}")),
            None => BodyOutcome::Failure("unknown exit status".into()),
        }
    }
}

fn grep_one<'a>(
    project: &'a Project,
    workspace: &Path,
    tool_path: &Path,
    tool_args: &[String],
) -> GrepOutcome<'a> {
    let cwd = workspace.join(&project.path);
    let mut cmd = Command::new(tool_path);
    // stdin → null: ripgrep (and others) detect a readable stdin
    // (pipe/FIFO inherited from a non-TTY parent like CI or `cargo
    // test`) and silently switch from "search cwd recursively" to
    // "search stdin", producing zero matches. Pinning stdin to null
    // makes per-project search behave the same in a TTY, in CI, and
    // when invoked from another tool.
    cmd.args(tool_args)
        .current_dir(&cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    match cmd.spawn().and_then(|c| c.wait_with_output()) {
        Ok(out) => GrepOutcome {
            project,
            stdout: out.stdout,
            stderr: out.stderr,
            exit_code: out.status.code(),
            spawn_err: None,
        },
        Err(e) => GrepOutcome {
            project,
            stdout: Vec::new(),
            stderr: Vec::new(),
            exit_code: None,
            spawn_err: Some(e.to_string()),
        },
    }
}

/// Resolve the grep tool's binary. v1 precedence: explicit
/// `--tool-path` > `grep.{tool}-path` config > tool-specific lookup
/// (`tool.git.binary` for git, `which()` for rg/ripgrep/grep).
fn resolve_tool_path(
    tool: Tool,
    arg_override: Option<&Path>,
    config: &Configuration,
) -> Result<PathBuf, GrepError> {
    if let Some(p) = arg_override {
        return Ok(p.to_path_buf());
    }
    if let Some(s) = config
        .get_str(&format!("grep.{}-path", tool.key()))
        .map_err(|e| GrepError::Config(e.to_string()))?
    {
        return Ok(PathBuf::from(s));
    }
    match tool {
        Tool::GitGrep => {
            // Share the vcs layer's `tool.git.binary` resolution by
            // reading the same key directly. Defaults to `"git"` on
            // PATH if the key isn't set, matching `GitClient::binary`.
            let from_config = config
                .get_str("tool.git.binary")
                .map_err(|e| GrepError::Config(e.to_string()))?;
            Ok(PathBuf::from(from_config.unwrap_or_else(|| "git".into())))
        }
        Tool::Ripgrep => which::which("rg")
            .or_else(|_| which::which("ripgrep"))
            .map_err(|_| GrepError::ToolNotFound { tool: "ripgrep" }),
        Tool::Grep => which::which("grep").map_err(|_| GrepError::ToolNotFound { tool: "grep" }),
    }
}

/// Build the argv for the grep tool, less the binary itself.
/// Shape: `[<git grep prefix>, --color=<X>, <config or default args>, <user extras>]`.
fn build_tool_args(
    tool: Tool,
    arg_color: Option<ColorArg>,
    extras: &[String],
    config: &Configuration,
) -> Result<Vec<String>, GrepError> {
    let mut out: Vec<String> = Vec::new();
    if tool == Tool::GitGrep {
        out.push("grep".into());
    }
    let color = resolve_color(arg_color, config)?;
    out.push(format!("--color={}", color.as_str()));

    let config_args = config
        .get_str(&format!("grep.{}-args", tool.key()))
        .map_err(|e| GrepError::Config(e.to_string()))?;
    if let Some(s) = config_args {
        let parsed = shlex::split(&s).ok_or_else(|| {
            GrepError::Config(format!("grep.{}-args: shlex split failed", tool.key()))
        })?;
        out.extend(parsed);
    } else {
        // Builtin defaults: only `grep` needs `--recursive` to walk
        // the project tree; git-grep and ripgrep recurse by default.
        if matches!(tool, Tool::Grep) {
            out.push("--recursive".into());
        }
    }

    out.extend(extras.iter().cloned());
    Ok(out)
}

fn resolve_color(
    arg_color: Option<ColorArg>,
    config: &Configuration,
) -> Result<ColorArg, GrepError> {
    if let Some(c) = arg_color {
        return Ok(c);
    }
    if let Some(s) = config
        .get_str("grep.color")
        .map_err(|e| GrepError::Config(e.to_string()))?
    {
        return parse_color(&s, "grep.color");
    }
    if let Some(s) = config
        .get_str("color.ui")
        .map_err(|e| GrepError::Config(e.to_string()))?
    {
        // `color.ui` accepts more (e.g. `true`/`false`); map common
        // ones, default to Auto for anything else.
        return Ok(match s.as_str() {
            "always" | "true" => ColorArg::Always,
            "never" | "false" => ColorArg::Never,
            _ => ColorArg::Auto,
        });
    }
    Ok(ColorArg::Auto)
}

fn parse_color(s: &str, key: &str) -> Result<ColorArg, GrepError> {
    match s {
        "always" => Ok(ColorArg::Always),
        "never" => Ok(ColorArg::Never),
        "auto" => Ok(ColorArg::Auto),
        other => Err(GrepError::Config(format!(
            "{key}: unknown value {other:?} (expected always, never, or auto)"
        ))),
    }
}

#[derive(Debug)]
struct Settings {
    jobs: usize,
    raw: bool,
    quiet: bool,
}

impl Settings {
    fn from_config(config: &Configuration) -> Result<Self, String> {
        let jobs = match config.get("grep.jobs").map_err(|e| e.to_string())? {
            None => default_jobs(),
            Some(ConfigValue::Integer(i)) if i >= 1 => i as usize,
            Some(ConfigValue::Integer(i)) => {
                return Err(format!("grep.jobs must be a positive integer (got {i})"));
            }
            Some(other) => {
                return Err(format!("grep.jobs must be an integer (got {other:?})"));
            }
        };
        let raw = config
            .get_bool("output.raw")
            .map_err(|e| e.to_string())?
            .unwrap_or(false);
        let quiet = config
            .get_bool("output.quiet")
            .map_err(|e| e.to_string())?
            .unwrap_or(false);
        // Same `_is_tty` reservation as `forall`'s Settings: kept here
        // so future colour heuristics have a single read site.
        let _is_tty = io::stdout().is_terminal();
        Ok(Self { jobs, raw, quiet })
    }
}

fn splice_flags_into_config(args: &GrepArgs, config: &mut Configuration) -> Result<(), String> {
    if let Some(jobs) = args.jobs {
        super::config::splice_inline(config, "grep.jobs", ConfigValue::Integer(jobs as i64))?;
    }
    Ok(())
}

fn default_jobs() -> usize {
    std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(1)
        .clamp(1, MAX_DEFAULT_JOBS)
}
