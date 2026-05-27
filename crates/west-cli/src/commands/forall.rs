//! `west forall` — run a shell command in each project.
//!
//! Per-project context is exposed via six `WEST_PROJECT_*` env vars.
//! Iteration matches the pattern shared with `list` (and future
//! `diff` / `status` / `compare`):
//!
//! - **Empty positionals** → all manifest projects + the synthetic
//!   manifest project, filtered by activity (default = `is_active`,
//!   `--all` includes inactive).
//! - **Non-empty positionals** → exact matches by name or relative
//!   path; `--inactive`-style filtering doesn't apply. Uncloned
//!   positionals error out with a hint.
//! - `-g GROUP` (repeatable, OR-logic) layers on top: keep only
//!   projects whose `groups:` intersects with the requested set.
//!   The synthetic manifest project has no groups, so `-g` excludes
//!   it (matches python's ManifestProject behaviour).
//! - Cloned-only: empty-positionals path silently skips uncloned
//!   projects; named-positionals path errors.
//!
//! The per-project `=== running …` banner is chrome and goes to
//! **stderr**; the command's own stdout stays on stdout. So
//! `west forall -c 'cat VERSION' > out` captures only the commands'
//! output, not the banners. v1 put the banner on stdout.
//!
//! Two stdio modes:
//!
//! - **Serial** (`-j 1` or `output.raw=true`): banner to stderr
//!   then `Stdio::inherit` — output flows live to the user's
//!   terminal. Users who want banner-less stderr too can pass `-q`
//!   (the autouse top-level quiet flag).
//! - **Parallel** (`-j N>1`): `Stdio::piped` + per-project capture;
//!   workers return their banner + captured streams, and the driver
//!   drains them in completion order at the end — banner and the
//!   child's stderr to the parent's stderr, the child's stdout to
//!   the parent's stdout. Keeps parallel runs interleave-free
//!   without an indicatif UI (we don't have phase/tick events from
//!   a generic shell command).

use std::io::{self, IsTerminal, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, ExitStatus, Stdio};
use std::sync::Mutex;

use clap::Args;
use console::Style;
use rayon::prelude::*;

use west_core::config::{ConfigValue, Configuration};
use west_core::manifest::Project;
use west_core::vcs;

use super::config::LoadedConfig;
use super::select;

/// Same default cap as `update`: beyond ~8 concurrent shell jobs the
/// shared resources (disk, terminal output) drown out the gain.
const MAX_DEFAULT_JOBS: usize = 8;

#[derive(Args, Debug)]
pub struct ForallArgs {
    /// Shell command to run in each project. Passed to `sh -c` on
    /// Unix, `cmd /C` on Windows.
    #[arg(short = 'c', long, value_name = "CMD")]
    pub command: String,

    /// Run the command in <DIR> instead of each project's tree.
    #[arg(short = 'C', value_name = "DIR")]
    pub cwd: Option<PathBuf>,

    /// Include inactive projects.
    #[arg(short = 'a', long)]
    pub all: bool,

    /// Only run in projects belonging to one of these groups
    /// (repeatable; OR-logic).
    #[arg(
        short = 'g',
        long = "group",
        value_name = "GROUP",
        action = clap::ArgAction::Append,
    )]
    pub groups: Vec<String>,

    /// Maximum projects to process concurrently. `1` disables
    /// parallelism. Equivalent to `--config forall.jobs=N`.
    #[arg(short = 'j', long, value_name = "N")]
    pub jobs: Option<usize>,

    /// Project names or paths. Empty = all (subject to `--all` / `-g`).
    #[arg(value_name = "PROJECT")]
    pub projects: Vec<String>,
}

#[derive(Debug, thiserror::Error)]
enum ForallError {
    #[error("not inside a west workspace (no .west/ found)")]
    NotInWorkspace,
    #[error("{0}")]
    Config(String),
    #[error("{0}")]
    Manifest(String),
    #[error("{0}")]
    Vcs(String),
    #[error("uncloned {plural}: {names}\n  Hint: run \"west update\" and retry.",
            plural = if names.contains(',') { "projects" } else { "project" })]
    UnclonedPositional { names: String },
    #[error("spawn shell: {0}")]
    Spawn(#[source] io::Error),
}

impl From<super::workspace::WorkspaceError> for ForallError {
    fn from(e: super::workspace::WorkspaceError) -> Self {
        match e {
            super::workspace::WorkspaceError::NotInWorkspace => ForallError::NotInWorkspace,
            super::workspace::WorkspaceError::Config(s) => ForallError::Config(s),
            super::workspace::WorkspaceError::Manifest(s) => ForallError::Manifest(s),
        }
    }
}

pub fn run(args: ForallArgs, loaded: &mut LoadedConfig) -> ExitCode {
    if let Err(e) = splice_flags_into_config(&args, &mut loaded.config) {
        eprintln!("west: {e}");
        return ExitCode::from(2);
    }

    match run_inner(args, loaded) {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::FAILURE,
        Err(e @ ForallError::UnclonedPositional { .. }) => {
            eprintln!("west: {e}");
            ExitCode::from(2)
        }
        Err(e) => {
            eprintln!("west: {e}");
            ExitCode::FAILURE
        }
    }
}

/// `Ok(true)` = every project's shell command exited 0;
/// `Ok(false)` = at least one failed (summary already printed).
fn run_inner(args: ForallArgs, loaded: &mut LoadedConfig) -> Result<bool, ForallError> {
    let workspace = super::workspace::resolve_workspace_dir()?;
    let vcs = vcs::from_config(&loaded.config).map_err(|e| ForallError::Vcs(e.to_string()))?;

    // Read-only manifest resolution: per-project imports for uncloned
    // projects are skipped silently (we'll filter to cloned anyway).
    let source = super::workspace::ReadOnlyImportSource::new(workspace.as_path(), vcs.as_ref());
    let loaded_manifest = super::workspace::load_manifest(&workspace, &loaded.config, &source)?;
    let manifest = &loaded_manifest.manifest;

    let synthetic_path = super::workspace::manifest_path_from_config(&loaded.config)?;
    let synthetic = select::synthetic_manifest_project(manifest, synthetic_path);

    // Step 1: candidate set (positional resolution + activity gate).
    let mut candidates: Vec<&Project> = if args.projects.is_empty() {
        let mut acc: Vec<&Project> = Vec::new();
        // Synthetic manifest project: always-active (no groups), so
        // `--inactive`-only filters don't apply here. Include unless
        // someone later asks for an inactive-only mode.
        if args.all || loaded_manifest.is_active(&synthetic, &[]) {
            acc.push(&synthetic);
        }
        acc.extend(
            manifest
                .projects
                .iter()
                .filter(|p| args.all || loaded_manifest.is_active(p, &[])),
        );
        acc
    } else {
        // Positional path. Pull "manifest" / `<self.path>` matches out
        // for the synthetic; fall through to `select_projects` for
        // the real ones.
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
                    .map_err(|e| ForallError::Manifest(e.to_string()))?,
            );
        }
        acc
    };

    // Step 2: `-g GROUP` (OR-logic). Synthetic has empty groups so it
    // never matches when `-g` is set — matches python.
    if !args.groups.is_empty() {
        candidates.retain(|p| p.groups.iter().any(|g| args.groups.iter().any(|q| q == g)));
    }

    // Step 3: cloned-only filter. Different shape based on positional
    // vs empty-positional path so users get a clear error if they
    // named an uncloned project.
    let mut uncloned_positional = Vec::new();
    let projects: Vec<&Project> = candidates
        .into_iter()
        .filter(|p| {
            let abs = workspace.join(&p.path);
            let cloned = abs.exists() && vcs.is_repo(&abs).unwrap_or(false);
            if !cloned && !args.projects.is_empty() {
                uncloned_positional.push(p.name.clone());
            }
            cloned
        })
        .collect();
    if !uncloned_positional.is_empty() {
        return Err(ForallError::UnclonedPositional {
            names: uncloned_positional.join(", "),
        });
    }

    if projects.is_empty() {
        eprintln!("west: forall: no projects matched");
        return Ok(true);
    }

    // Mode selection. Match `update`'s shape: raw forces serial; -j 1
    // is serial; otherwise parallel up to settings.jobs.
    let settings = Settings::from_config(&loaded.config).map_err(ForallError::Config)?;
    let parallel = !settings.raw && settings.jobs > 1 && projects.len() > 1;
    let jobs = if parallel { settings.jobs } else { 1 };

    if parallel {
        run_parallel(&args, &workspace, &projects, jobs, settings.quiet)
    } else {
        run_serial(&args, &workspace, &projects, settings.quiet)
    }
}

// =========================================================================
// Worker — serial vs parallel
// =========================================================================

fn run_serial(
    args: &ForallArgs,
    workspace: &Path,
    projects: &[&Project],
    quiet: bool,
) -> Result<bool, ForallError> {
    // Bright green + bold matches python v1's banner palette
    // (`colorama.Fore.LIGHTGREEN_EX`). The banner is chrome, so it
    // goes to stderr (auto-detect colour against stderr); the
    // command's own stdout stays clean on stdout. Lets
    // `west forall -c '…' > out` capture only the command output.
    let bold = Style::new().green().bright().bold().for_stderr();
    let mut failed: Vec<String> = Vec::new();
    for project in projects {
        let abspath = workspace.join(&project.path);
        let cwd = args.cwd.as_deref().unwrap_or(&abspath);
        if !quiet {
            eprintln!(
                "{}",
                bold.apply_to(format!(
                    "=== running \"{}\" in {} ({}):",
                    args.command,
                    project.name,
                    project.path.display(),
                ))
            );
        }
        let mut cmd = build_command(args, project, &abspath, cwd);
        // Inherit stdio so output flows live.
        let status = cmd.status().map_err(ForallError::Spawn)?;
        if !status.success() {
            failed.push(project.name.clone());
        }
    }
    summarize(failed)
}

fn run_parallel(
    args: &ForallArgs,
    workspace: &Path,
    projects: &[&Project],
    jobs: usize,
    quiet: bool,
) -> Result<bool, ForallError> {
    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(jobs)
        .build()
        .map_err(|e| ForallError::Config(format!("failed to start worker pool: {e}")))?;

    // Collect outcomes in arrival order. Rayon preserves the input
    // order on `collect_into_vec`, but we want completion order so the
    // user sees fast jobs flush first; use a Mutex<Vec<...>> push.
    let outcomes: Mutex<Vec<BufferedOutcome>> = Mutex::new(Vec::with_capacity(projects.len()));

    pool.install(|| {
        projects.par_iter().for_each(|project| {
            let abspath = workspace.join(&project.path);
            let cwd_owned = args.cwd.clone();
            let cwd: &Path = cwd_owned.as_deref().unwrap_or(&abspath);
            let mut cmd = build_command(args, project, &abspath, cwd);
            cmd.stdout(Stdio::piped()).stderr(Stdio::piped());
            let outcome = match cmd.spawn().and_then(|c| c.wait_with_output()) {
                Ok(out) => BufferedOutcome {
                    name: project.name.clone(),
                    banner: format!(
                        "=== running \"{}\" in {} ({}):",
                        args.command,
                        project.name,
                        project.path.display(),
                    ),
                    stdout: out.stdout,
                    stderr: out.stderr,
                    status: Some(out.status),
                    spawn_err: None,
                },
                Err(e) => BufferedOutcome {
                    name: project.name.clone(),
                    banner: format!(
                        "=== running \"{}\" in {} ({}):",
                        args.command,
                        project.name,
                        project.path.display(),
                    ),
                    stdout: Vec::new(),
                    stderr: Vec::new(),
                    status: None,
                    spawn_err: Some(e.to_string()),
                },
            };
            outcomes
                .lock()
                .unwrap_or_else(|p| p.into_inner())
                .push(outcome);
        });
    });

    let outcomes = outcomes.into_inner().unwrap_or_else(|p| p.into_inner());
    // Bright green + bold matches python v1's banner palette
    // (`colorama.Fore.LIGHTGREEN_EX`). Banner is chrome → stderr
    // (auto-detect colour against stderr); the child's captured
    // stdout/stderr are demultiplexed back onto the parent's
    // matching streams, so a redirect of `west forall` stdout
    // captures only the commands' stdout.
    let bold = Style::new().green().bright().bold().for_stderr();
    let stdout = io::stdout();
    let stderr = io::stderr();
    let mut out_lock = stdout.lock();
    let mut err_lock = stderr.lock();
    let mut failed: Vec<String> = Vec::new();
    for outcome in outcomes {
        if !quiet {
            // Flush stdout so the banner heads this project's output
            // block when both streams share a terminal.
            let _ = out_lock.flush();
            let _ = writeln!(err_lock, "{}", bold.apply_to(&outcome.banner));
            let _ = err_lock.flush();
        }
        let _ = out_lock.write_all(&outcome.stdout);
        let _ = out_lock.flush();
        let _ = err_lock.write_all(&outcome.stderr);
        match outcome.status {
            Some(s) if !s.success() => failed.push(outcome.name),
            None => {
                let _ = writeln!(
                    err_lock,
                    "west: forall: spawn failed for {}: {}",
                    outcome.name,
                    outcome.spawn_err.as_deref().unwrap_or("(unknown)"),
                );
                failed.push(outcome.name);
            }
            _ => {}
        }
    }
    drop(out_lock);
    drop(err_lock);
    summarize(failed)
}

struct BufferedOutcome {
    name: String,
    banner: String,
    stdout: Vec<u8>,
    stderr: Vec<u8>,
    status: Option<ExitStatus>,
    spawn_err: Option<String>,
}

fn summarize(failed: Vec<String>) -> Result<bool, ForallError> {
    if failed.is_empty() {
        return Ok(true);
    }
    let n = failed.len();
    let plural = if n == 1 { "" } else { "s" };
    if n < 20 {
        eprintln!(
            "west: forall failed for {n} project{plural}: {}",
            failed.join(", "),
        );
    } else {
        eprintln!("west: forall failed for {n} projects; see above");
    }
    Ok(false)
}

fn build_command(args: &ForallArgs, project: &Project, abspath: &Path, cwd: &Path) -> Command {
    let (program, flag) = shell_invocation();
    let mut cmd = Command::new(program);
    cmd.arg(flag).arg(&args.command).current_dir(cwd);
    for (k, v) in build_env(project, abspath) {
        cmd.env(k, v);
    }
    cmd
}

fn shell_invocation() -> (&'static str, &'static str) {
    if cfg!(windows) {
        ("cmd", "/C")
    } else {
        ("sh", "-c")
    }
}

fn build_env(project: &Project, abspath: &Path) -> [(&'static str, String); 6] {
    [
        ("WEST_PROJECT_NAME", project.name.clone()),
        (
            "WEST_PROJECT_PATH",
            project.path.to_string_lossy().into_owned(),
        ),
        (
            "WEST_PROJECT_ABSPATH",
            abspath.to_string_lossy().into_owned(),
        ),
        ("WEST_PROJECT_REVISION", project.revision.clone()),
        ("WEST_PROJECT_URL", project.url.clone()),
        ("WEST_PROJECT_REMOTE", project.remote_name.clone()),
    ]
}

// =========================================================================
// Settings + workspace + manifest (same shape as list/update)
// =========================================================================

#[derive(Debug)]
struct Settings {
    jobs: usize,
    raw: bool,
    quiet: bool,
}

impl Settings {
    fn from_config(config: &Configuration) -> Result<Self, String> {
        let jobs = match config.get("forall.jobs").map_err(|e| e.to_string())? {
            None => default_jobs(),
            Some(ConfigValue::Integer(i)) if i >= 1 => i as usize,
            Some(ConfigValue::Integer(i)) => {
                return Err(format!("forall.jobs must be a positive integer (got {i})"));
            }
            Some(other) => {
                return Err(format!("forall.jobs must be an integer (got {other:?})"));
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
        // `_` to silence the read on serial-only platforms; we use the
        // computed value indirectly via `jobs` and `raw`.
        let _is_tty = io::stderr().is_terminal();
        Ok(Self { jobs, raw, quiet })
    }
}

fn splice_flags_into_config(args: &ForallArgs, config: &mut Configuration) -> Result<(), String> {
    if let Some(jobs) = args.jobs {
        super::config::splice_inline(config, "forall.jobs", ConfigValue::Integer(jobs as i64))?;
    }
    Ok(())
}

fn default_jobs() -> usize {
    std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(1)
        .clamp(1, MAX_DEFAULT_JOBS)
}

