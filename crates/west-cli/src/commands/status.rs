//! `west status` — show per-project working-tree status across
//! the workspace.
//!
//! Iterates cloned projects (active by default, all with `--all`),
//! delegates the actual status query to [`Vcs::status`] (so future
//! Mercurial / Sapling clients drop in without touching this
//! module), buffers each project's body, and drains in workspace
//! order for non-interleaved parallel runs.
//!
//! UX improvements over v1:
//!
//! - Per-project `=== status of …` banners and the tail summary go
//!   to **stderr**; only the status bodies hit stdout, so
//!   `west status > out` stays banner-free. v1 emitted both on
//!   stdout.
//! - Default is short / porcelain-v1 format and SKIPS CLEAN
//!   PROJECTS. v1 ran `git status` per project regardless of
//!   state; in a 100-project workspace that emitted 99 copies of
//!   "On branch X / nothing to commit, working tree clean" — pure
//!   noise. The compact default is what users actually want in
//!   the inspect/git-passthrough family of commands.
//! - `--long` keeps v1's bare `git status` shape; it implies
//!   "show all projects, including clean ones" since the long
//!   form's value IS the per-project context.
//! - `--exit-code` mirrors `git diff --exit-code` — exit 1 when
//!   any project is dirty. Useful for CI ("fail if any
//!   uncommitted change exists in the workspace").
//! - `-q/--quiet` (top-level global) suppresses chrome (banners,
//!   tail summary). Same primitive `west diff` uses; `-v` cancels
//!   `-q` via net subtraction.
//! - `-j/--jobs N` parallel iteration; per-project bodies drain
//!   in workspace order so output stays contiguous.
//! - `--` forwards extra args to the underlying tool (both
//!   `Short` and `Long` modes). e.g. `west status -- -u no` to
//!   suppress untracked files.

use std::io::{self, IsTerminal, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use clap::Args;
use console::Style;
use rayon::prelude::*;

use west_core::config::{ConfigValue, Configuration};
use west_core::manifest::Project;
use west_core::vcs::{ColorMode, StatusMode, StatusOutcome, StatusSpec, Vcs, VcsError};

use super::color::ColorArg;
use super::config::LoadedConfig;
use super::select;

const MAX_DEFAULT_JOBS: usize = 8;

#[derive(Args, Debug)]
pub struct StatusArgs {
    /// Project names or paths. Empty = every active cloned project.
    #[arg(value_name = "PROJECT")]
    pub projects: Vec<String>,

    /// Include inactive projects.
    #[arg(short, long)]
    pub all: bool,

    /// Use long-form output (`git status`) instead of short
    /// (porcelain v1). Implies showing every project, including
    /// clean ones — long form's value is the per-project context.
    #[arg(short = 'l', long)]
    pub long: bool,

    /// Exit 1 if any project has uncommitted changes. Mirrors
    /// `git diff --exit-code`; useful for CI gating.
    #[arg(long = "exit-code")]
    pub exit_code: bool,

    /// Colorize output. Unset, the resolver consults `color.ui`
    /// before falling back to TTY-aware `auto`. Only affects
    /// long-form output — porcelain v1 is intentionally colorless.
    #[arg(long, value_enum)]
    pub color: Option<ColorArg>,

    /// Maximum projects to inspect concurrently. Twin of
    /// `status.jobs` config key; defaults to `min(num_cpus, 8)`.
    #[arg(short = 'j', long, value_name = "N")]
    pub jobs: Option<usize>,

    /// Extra arguments forwarded to the VCS's status command.
    /// Use `--` to separate: `west status -- -u no`.
    #[arg(last = true, allow_hyphen_values = true)]
    pub extra: Vec<String>,
}

#[derive(Debug, thiserror::Error)]
enum StatusError {
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
}

impl From<super::workspace::WorkspaceError> for StatusError {
    fn from(e: super::workspace::WorkspaceError) -> Self {
        match e {
            super::workspace::WorkspaceError::NotInWorkspace => StatusError::NotInWorkspace,
            super::workspace::WorkspaceError::Config(s) => StatusError::Config(s),
            super::workspace::WorkspaceError::Manifest(s) => StatusError::Manifest(s),
            super::workspace::WorkspaceError::Vcs(s) => StatusError::Vcs(s),
        }
    }
}

pub fn run(args: StatusArgs, loaded: &mut LoadedConfig) -> ExitCode {
    if let Err(e) = splice_flags_into_config(&args, &mut loaded.config) {
        log::error!("{e}");
        return ExitCode::from(2);
    }
    match run_inner(args, loaded) {
        Ok(Outcome::AllClean) => ExitCode::SUCCESS,
        Ok(Outcome::SomeDirty {
            exit_code_flag: false,
        }) => ExitCode::SUCCESS,
        Ok(Outcome::SomeDirty {
            exit_code_flag: true,
        }) => ExitCode::from(1),
        Ok(Outcome::Failures) => ExitCode::FAILURE,
        Err(e @ StatusError::UnclonedPositional { .. }) => {
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
    AllClean,
    SomeDirty { exit_code_flag: bool },
    Failures,
}

fn run_inner(args: StatusArgs, loaded: &mut LoadedConfig) -> Result<Outcome, StatusError> {
    let workspace = super::workspace::resolve_workspace_dir()?;
    let (loaded_manifest, vcs, _skipped) =
        super::workspace::load_manifest_resolved(workspace.as_path(), &loaded.config)?;
    let manifest = &loaded_manifest.manifest;

    let synthetic_path = super::workspace::manifest_path_from_config(&loaded.config)?;
    let synthetic = select::synthetic_manifest_project(manifest, synthetic_path);

    // Candidate set — same shape as `diff` / `forall`. The
    // synthetic manifest project is included here unconditionally;
    // `git status` works on the self-tree even though
    // `manifest-rev` doesn't exist (status is rev-agnostic).
    let candidates: Vec<&Project> = if args.projects.is_empty() {
        let mut acc: Vec<&Project> = Vec::new();
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
                    .map_err(|e| StatusError::Manifest(e.to_string()))?,
            );
        }
        acc
    };

    let mut uncloned_positional: Vec<String> = Vec::new();
    let projects: Vec<&Project> = candidates
        .into_iter()
        .filter(|p| {
            let abs = workspace.join(&p.path);
            let cloned = super::workspace::is_cloned(vcs.as_ref(), &abs);
            if !cloned && !args.projects.is_empty() {
                uncloned_positional.push(p.name.clone());
            }
            cloned
        })
        .collect();
    if !uncloned_positional.is_empty() {
        return Err(StatusError::UnclonedPositional {
            names: uncloned_positional.join(", "),
        });
    }

    if projects.is_empty() {
        log::warn!("status: no projects matched");
        return Ok(Outcome::AllClean);
    }

    let settings = Settings::from_config(&loaded.config).map_err(StatusError::Config)?;
    let parallel = !settings.raw && settings.jobs > 1 && projects.len() > 1;
    let jobs = if parallel { settings.jobs } else { 1 };

    // --color → color.ui → auto. The long-form body git emits is
    // captured into a pipe, so its own TTY heuristic always picks
    // "never"; force Always/Never here based on stdout's real TTY.
    let color_choice =
        super::color::resolve(args.color, &loaded.config, None).map_err(StatusError::Config)?;
    let resolved_color = match color_choice {
        ColorArg::Always => ColorMode::Always,
        ColorArg::Never => ColorMode::Never,
        ColorArg::Auto => {
            if io::stdout().is_terminal() {
                ColorMode::Always
            } else {
                ColorMode::Never
            }
        }
    };

    let mode = if args.long {
        StatusMode::Long
    } else {
        StatusMode::Short
    };

    let outcomes: Vec<ProjectOutcome> = if parallel {
        let pool = rayon::ThreadPoolBuilder::new()
            .num_threads(jobs)
            .build()
            .map_err(|e| StatusError::Config(format!("failed to start worker pool: {e}")))?;
        pool.install(|| {
            projects
                .par_iter()
                .map(|p| {
                    status_one(
                        p,
                        &workspace,
                        vcs.as_ref(),
                        mode,
                        resolved_color,
                        &args.extra,
                    )
                })
                .collect()
        })
    } else {
        projects
            .iter()
            .map(|p| {
                status_one(
                    p,
                    &workspace,
                    vcs.as_ref(),
                    mode,
                    resolved_color,
                    &args.extra,
                )
            })
            .collect()
    };

    // Drain. In `Short` mode, skip clean projects (their body is
    // empty anyway and the banner would be noise). In `Long`
    // mode, print everything — the user explicitly opted into the
    // verbose shape. Per-project banner (chrome) goes to stderr;
    // the status body (result) goes to stdout, matching `diff` /
    // `forall` so `west status > out` stays banner-free.
    let mut stdout = io::stdout().lock();
    let mut stderr = io::stderr().lock();
    let mut clean_count: usize = 0;
    let mut had_dirty = false;
    let mut failures: Vec<(String, String)> = Vec::new();
    // Bright green + bold matches python v1's banner palette
    // (`colorama.Fore.LIGHTGREEN_EX`). The banner lands on stderr,
    // so `auto` follows stderr's TTY-ness; `--color always/never`
    // force the choice. (`resolved_color`, keyed off stdout, colours
    // the long-form body git emits.)
    let banner_style = match color_choice {
        ColorArg::Always => Style::new().green().bright().bold().force_styling(true),
        ColorArg::Never => Style::new().force_styling(false),
        ColorArg::Auto => Style::new().green().bright().bold().for_stderr(),
    };
    for o in outcomes {
        match o.result {
            Ok(StatusOutcome::Clean) if !args.long => {
                clean_count += 1;
            }
            Ok(outcome) => {
                if matches!(outcome, StatusOutcome::Dirty) {
                    had_dirty = true;
                }
                if !settings.quiet {
                    // Flush stdout first so the banner heads its body
                    // when both share a terminal.
                    let _ = stdout.flush();
                    let _ = writeln!(
                        stderr,
                        "{}",
                        banner_style.apply_to(format!(
                            "=== status of {} ({})",
                            o.name,
                            o.path.display(),
                        )),
                    );
                    let _ = stderr.flush();
                }
                let _ = stdout.write_all(&o.body);
                // `Short` mode's porcelain output has no trailing
                // blank line between projects; insert one for
                // readability when there are multiple non-empty
                // blocks. `Long` mode's output already has its own
                // trailing newline.
                if !args.long {
                    let _ = writeln!(stdout);
                }
            }
            Err(e) => failures.push((o.name, e.to_string())),
        }
    }
    // Tail summary is chrome → stderr alongside the banners.
    if !settings.quiet && had_dirty && clean_count > 0 && !args.long {
        let _ = stdout.flush();
        let _ = writeln!(
            stderr,
            "Clean working tree in {clean_count} project{}.",
            if clean_count == 1 { "" } else { "s" }
        );
    }
    drop(stdout);
    drop(stderr);

    if !failures.is_empty() {
        for (name, msg) in &failures {
            log::error!("status failed for {name}: {msg}");
        }
        let names: Vec<&str> = failures.iter().map(|(n, _)| n.as_str()).collect();
        log::error!(
            "status failed for {} project{}: {}",
            names.len(),
            if names.len() == 1 { "" } else { "s" },
            names.join(", "),
        );
        return Ok(Outcome::Failures);
    }

    if had_dirty {
        Ok(Outcome::SomeDirty {
            exit_code_flag: args.exit_code,
        })
    } else {
        Ok(Outcome::AllClean)
    }
}

struct ProjectOutcome {
    name: String,
    path: PathBuf,
    body: Vec<u8>,
    result: Result<StatusOutcome, VcsError>,
}

fn status_one(
    project: &Project,
    workspace: &Path,
    vcs: &dyn Vcs,
    mode: StatusMode,
    color: ColorMode,
    extra: &[String],
) -> ProjectOutcome {
    let abspath = workspace.join(&project.path);
    let spec = StatusSpec {
        mode,
        color,
        extra_args: extra,
    };
    let mut body = Vec::new();
    let result = vcs.status(&abspath, &spec, &mut body);
    ProjectOutcome {
        name: project.name.clone(),
        path: project.path.clone(),
        body,
        result,
    }
}

// =========================================================================
// Settings (mirrors diff.rs / forall.rs)
// =========================================================================

#[derive(Debug)]
struct Settings {
    jobs: usize,
    raw: bool,
    quiet: bool,
}

impl Settings {
    fn from_config(config: &Configuration) -> Result<Self, String> {
        let jobs = match config.get("status.jobs").map_err(|e| e.to_string())? {
            None => default_jobs(),
            Some(ConfigValue::Integer(i)) if i >= 1 => i as usize,
            Some(ConfigValue::Integer(i)) => {
                return Err(format!("status.jobs must be a positive integer (got {i})"));
            }
            Some(other) => {
                return Err(format!("status.jobs must be an integer (got {other:?})"));
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
        Ok(Self { jobs, raw, quiet })
    }
}

fn splice_flags_into_config(args: &StatusArgs, config: &mut Configuration) -> Result<(), String> {
    if let Some(jobs) = args.jobs {
        super::config::splice_inline(config, "status.jobs", ConfigValue::Integer(jobs as i64))?;
    }
    Ok(())
}

fn default_jobs() -> usize {
    std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(1)
        .clamp(1, MAX_DEFAULT_JOBS)
}
