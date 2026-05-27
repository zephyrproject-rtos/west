//! `west diff` — show per-project diffs across the workspace.
//!
//! Iterates cloned projects (active by default, all with `--all`),
//! delegates the actual diff to [`Vcs::diff`] (so future Mercurial /
//! Sapling clients drop in without touching this module), and emits
//! per-project banner + body. Parallel runs buffer each project's
//! body into a `Vec<u8>` and drain in workspace order so output
//! stays interleave-free.
//!
//! UX improvements over v1:
//!
//! - Per-project `=== diff in …` banners go to **stderr**; only the
//!   diff bodies hit stdout. `west diff > out.patch` therefore yields
//!   an appliable patch instead of one polluted with banner lines.
//!   v1 emitted both on stdout.
//! - `--exit-code` returns 1 when any project has a non-empty diff,
//!   for CI / scripting. v1 always exited 0.
//! - `--color {always,never,auto}` (default `auto`) replaces the
//!   config-only `color.ui` gate. `auto` checks the real stdout's
//!   TTY-ness (we resolve once at command start because workers
//!   capture into pipes and would otherwise force `never`).
//! - `-q/--quiet` (top-level, global) suppresses all chrome —
//!   per-project banners AND the tail "Empty diff in N projects."
//!   line. The diff bodies themselves are unaffected. Lives on
//!   `Cli` rather than `DiffArgs` so `west -q diff` and
//!   `west diff -q` behave identically; `lib.rs` splices the
//!   choice into `output.quiet` for subcommands to read.
//! - `-j/--jobs N` runs per-project diffs in parallel via rayon;
//!   per-project bodies buffer and drain in workspace order so
//!   parallel output stays contiguous.
//! - `--` forwards extra args to the underlying tool (`west diff
//!   -- --stat` runs `git diff --stat` per project).
//!
//! Iteration / cloned-only filter / synthetic-manifest-project
//! handling all mirror `forall`'s shape.

use std::io::{self, IsTerminal, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use clap::{Args, ValueEnum};
use console::Style;
use rayon::prelude::*;

use west_core::config::{ConfigValue, Configuration};
use west_core::manifest::Project;
use west_core::vcs::{self, ColorMode, DiffOutcome, DiffSpec, RevSpec, Vcs, VcsError};

use super::config::LoadedConfig;
use super::select;

const MAX_DEFAULT_JOBS: usize = 8;

#[derive(Args, Debug)]
pub struct DiffArgs {
    /// Project names or paths. Empty = every active cloned project.
    #[arg(value_name = "PROJECT")]
    pub projects: Vec<String>,

    /// Include inactive projects.
    #[arg(short, long)]
    pub all: bool,

    /// Compare against `manifest-rev` instead of HEAD.
    #[arg(short = 'm', long)]
    pub manifest: bool,

    /// Exit 1 if any project has a non-empty diff. Mirrors
    /// `git diff --exit-code`; v1 didn't expose this.
    #[arg(long = "exit-code")]
    pub exit_code: bool,

    /// Colorize diff output. `auto` (default) emits color when
    /// stdout is a TTY.
    #[arg(long, value_enum, default_value_t = ColorArg::Auto)]
    pub color: ColorArg,

    /// Maximum projects to diff concurrently. Twin of `diff.jobs`
    /// config key; defaults to `min(num_cpus, 8)`.
    #[arg(short = 'j', long, value_name = "N")]
    pub jobs: Option<usize>,

    /// Extra arguments forwarded verbatim to the VCS's diff
    /// command. The `--` separator is required because `PROJECT`
    /// (variadic) takes everything before it: `west diff -- --stat`.
    #[arg(last = true, allow_hyphen_values = true)]
    pub extra: Vec<String>,
}

#[derive(ValueEnum, Clone, Copy, Debug)]
pub enum ColorArg {
    Always,
    Never,
    Auto,
}

#[derive(Debug, thiserror::Error)]
enum DiffError {
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

impl From<super::workspace::WorkspaceError> for DiffError {
    fn from(e: super::workspace::WorkspaceError) -> Self {
        match e {
            super::workspace::WorkspaceError::NotInWorkspace => DiffError::NotInWorkspace,
            super::workspace::WorkspaceError::Config(s) => DiffError::Config(s),
            super::workspace::WorkspaceError::Manifest(s) => DiffError::Manifest(s),
        }
    }
}

pub fn run(args: DiffArgs, loaded: &mut LoadedConfig) -> ExitCode {
    if let Err(e) = splice_flags_into_config(&args, &mut loaded.config) {
        log::error!("{e}");
        return ExitCode::from(2);
    }

    match run_inner(args, loaded) {
        Ok(Outcome::AllEmpty) => ExitCode::SUCCESS,
        Ok(Outcome::SomeNonEmpty { exit_code_flag: false }) => ExitCode::SUCCESS,
        Ok(Outcome::SomeNonEmpty { exit_code_flag: true }) => ExitCode::from(1),
        Ok(Outcome::Failures) => ExitCode::FAILURE,
        Err(e @ DiffError::UnclonedPositional { .. }) => {
            log::error!("{e}");
            ExitCode::from(2)
        }
        Err(e) => {
            log::error!("{e}");
            ExitCode::FAILURE
        }
    }
}

/// Top-level outcome — drives the dispatched ExitCode.
enum Outcome {
    /// Every project's diff was empty (or no projects matched).
    AllEmpty,
    /// At least one project had a non-empty diff. `exit_code_flag`
    /// carries the user's `--exit-code` choice so the caller can
    /// translate to ExitCode::from(1) when set.
    SomeNonEmpty { exit_code_flag: bool },
    /// At least one project's diff call failed (binary crash, repo
    /// missing, etc.). End-of-run summary already printed.
    Failures,
}

fn run_inner(args: DiffArgs, loaded: &mut LoadedConfig) -> Result<Outcome, DiffError> {
    let workspace = super::workspace::resolve_workspace_dir()?;
    let vcs = vcs::from_config(&loaded.config).map_err(|e| DiffError::Vcs(e.to_string()))?;

    let source = super::workspace::ReadOnlyImportSource::new(workspace.as_path(), vcs.as_ref());
    let loaded_manifest = super::workspace::load_manifest(&workspace, &loaded.config, &source)?;
    let manifest = &loaded_manifest.manifest;

    let synthetic_path = super::workspace::manifest_path_from_config(&loaded.config)?;
    let synthetic = select::synthetic_manifest_project(manifest, synthetic_path);

    // Candidate set: same shape as forall, with one exception —
    // when `--manifest` is set, the synthetic manifest project is
    // excluded automatically. The self-project tree isn't visited
    // by `west update` (the manifest is the source of truth, not
    // a target), so `refs/heads/manifest-rev` never exists there
    // and a diff against it would error.
    let candidates: Vec<&Project> = if args.projects.is_empty() {
        let mut acc: Vec<&Project> = Vec::new();
        if !args.manifest && (args.all || loaded_manifest.is_active(&synthetic, &[])) {
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
        // See the empty-positionals branch: `--manifest` excludes
        // the synthetic project unconditionally.
        if !synthetic_hits.is_empty() && !args.manifest {
            acc.push(&synthetic);
        }
        if !leftover.is_empty() {
            let leftover: Vec<&str> = leftover.iter().map(|s| s.as_str()).collect();
            acc.extend(
                select::select_projects(&loaded_manifest, &leftover, &[])
                    .map_err(|e| DiffError::Manifest(e.to_string()))?,
            );
        }
        acc
    };

    // Cloned-only filter; named positionals that point at uncloned
    // projects error out (matches forall).
    let mut uncloned_positional: Vec<String> = Vec::new();
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
        return Err(DiffError::UnclonedPositional {
            names: uncloned_positional.join(", "),
        });
    }

    if projects.is_empty() {
        log::warn!("diff: no projects matched");
        return Ok(Outcome::AllEmpty);
    }

    let settings = Settings::from_config(&loaded.config).map_err(DiffError::Config)?;
    let parallel = !settings.raw && settings.jobs > 1 && projects.len() > 1;
    let jobs = if parallel { settings.jobs } else { 1 };

    // Resolve color once: workers capture into pipes, so git's own
    // TTY heuristic would always pick "never". We force either
    // Always or Never explicitly based on the real stdout.
    let resolved_color = match args.color {
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

    let from_rev: Option<RevSpec<'_>> = if args.manifest { Some(RevSpec::ManifestRev) } else { None };

    // Per-project work. par_iter().map().collect() preserves input
    // order so the drain below sees workspace order regardless of
    // completion order.
    let outcomes: Vec<ProjectOutcome> = if parallel {
        let pool = rayon::ThreadPoolBuilder::new()
            .num_threads(jobs)
            .build()
            .map_err(|e| DiffError::Config(format!("failed to start worker pool: {e}")))?;
        pool.install(|| {
            projects
                .par_iter()
                .map(|p| {
                    diff_one(
                        p,
                        &workspace,
                        vcs.as_ref(),
                        from_rev,
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
                diff_one(
                    p,
                    &workspace,
                    vcs.as_ref(),
                    from_rev,
                    resolved_color,
                    &args.extra,
                )
            })
            .collect()
    };

    // Drain: per-project banner (chrome) to stderr, diff body
    // (result) to stdout, so `west diff > out.patch` yields an
    // appliable patch — the `=== diff in …` banners aren't valid
    // diff syntax and would break `git apply` if mixed into stdout.
    let mut stdout = io::stdout().lock();
    let mut stderr = io::stderr().lock();
    let mut empty_count: usize = 0;
    let mut had_nonempty = false;
    let mut failures: Vec<(String, String)> = Vec::new();
    // Bright green + bold matches python v1's banner palette
    // (`colorama.Fore.LIGHTGREEN_EX`, plus a bold modifier for
    // extra prominence at the row-density of multi-project runs).
    // The banner now lands on stderr, so `auto` follows stderr's
    // TTY-ness (`for_stderr`); `--color always/never` force the
    // choice via `force_styling`. The diff *body* colour is a
    // separate decision (`resolved_color`, keyed off stdout) since
    // it's what gets redirected.
    let banner_style = match args.color {
        ColorArg::Always => Style::new().green().bright().bold().force_styling(true),
        ColorArg::Never => Style::new().force_styling(false),
        ColorArg::Auto => Style::new().green().bright().bold().for_stderr(),
    };
    for o in outcomes {
        match o.result {
            Ok(DiffOutcome::Empty) => empty_count += 1,
            Ok(DiffOutcome::NonEmpty) => {
                had_nonempty = true;
                if !settings.quiet {
                    // Flush stdout first so the banner prints above
                    // the body it heads when both share a terminal.
                    let _ = stdout.flush();
                    let _ = writeln!(
                        stderr,
                        "{}",
                        banner_style.apply_to(format!(
                            "=== diff in {} ({})",
                            o.name,
                            o.path.display(),
                        )),
                    );
                    let _ = stderr.flush();
                }
                let _ = stdout.write_all(&o.body);
            }
            Err(e) => failures.push((o.name, e.to_string())),
        }
    }
    // Suppress the chrome under `--quiet`: banner is already gated
    // above, and the tail summary line ("Empty diff in N
    // projects.") is the only other chrome `west diff` produces.
    // It's chrome, so it joins the banners on stderr.
    if !settings.quiet && had_nonempty && empty_count > 0 {
        let _ = stdout.flush();
        let _ = writeln!(
            stderr,
            "Empty diff in {empty_count} project{}.",
            if empty_count == 1 { "" } else { "s" }
        );
    }
    drop(stdout);
    drop(stderr);

    if !failures.is_empty() {
        for (name, msg) in &failures {
            log::error!("diff failed for {name}: {msg}");
        }
        let names: Vec<&str> = failures.iter().map(|(n, _)| n.as_str()).collect();
        log::error!(
            "diff failed for {} project{}: {}",
            names.len(),
            if names.len() == 1 { "" } else { "s" },
            names.join(", "),
        );
        return Ok(Outcome::Failures);
    }

    if had_nonempty {
        Ok(Outcome::SomeNonEmpty {
            exit_code_flag: args.exit_code,
        })
    } else {
        Ok(Outcome::AllEmpty)
    }
}

struct ProjectOutcome {
    name: String,
    path: PathBuf,
    body: Vec<u8>,
    result: Result<DiffOutcome, VcsError>,
}

fn diff_one(
    project: &Project,
    workspace: &Path,
    vcs: &dyn Vcs,
    from_rev: Option<RevSpec<'_>>,
    color: ColorMode,
    extra: &[String],
) -> ProjectOutcome {
    let abspath = workspace.join(&project.path);
    let path_prefix = project.path.to_string_lossy().into_owned();
    let spec = DiffSpec {
        from_rev,
        to_rev: None,
        color,
        path_prefix: Some(&path_prefix),
        extra_args: extra,
    };
    let mut body = Vec::new();
    let result = vcs.diff(&abspath, &spec, &mut body);
    ProjectOutcome {
        name: project.name.clone(),
        path: project.path.clone(),
        body,
        result,
    }
}

// =========================================================================
// Settings + workspace + manifest (mirrors forall.rs)
// =========================================================================

#[derive(Debug)]
struct Settings {
    jobs: usize,
    raw: bool,
    /// True when the top-level `-q/--quiet` was given (spliced
    /// into `output.quiet` by `lib.rs::run`). Subcommands read
    /// from one canonical place so `-q` works at any position —
    /// `west -q diff` and `west diff -q` are equivalent.
    quiet: bool,
}

impl Settings {
    fn from_config(config: &Configuration) -> Result<Self, String> {
        let jobs = match config.get("diff.jobs").map_err(|e| e.to_string())? {
            None => default_jobs(),
            Some(ConfigValue::Integer(i)) if i >= 1 => i as usize,
            Some(ConfigValue::Integer(i)) => {
                return Err(format!("diff.jobs must be a positive integer (got {i})"));
            }
            Some(other) => {
                return Err(format!("diff.jobs must be an integer (got {other:?})"));
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

fn splice_flags_into_config(args: &DiffArgs, config: &mut Configuration) -> Result<(), String> {
    if let Some(jobs) = args.jobs {
        super::config::splice_inline(config, "diff.jobs", ConfigValue::Integer(jobs as i64))?;
    }
    Ok(())
}

fn default_jobs() -> usize {
    std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(1)
        .clamp(1, MAX_DEFAULT_JOBS)
}

