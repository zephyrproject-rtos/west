//! `west compare` — surface per-project state that diverges from
//! the manifest's recorded baseline.
//!
//! Built on the same scaffold as `diff` / `status`: workspace
//! module for setup, per-project iteration with parallel
//! buffering, `--exit-code` for CI gating, global `-q` from the
//! Cli verbosity primitive.
//!
//! Decision per project — print output if ANY of:
//!
//! - **HEAD ≠ manifest-rev** — the working tree is at a different
//!   commit than the last `west update` recorded. Skipped for the
//!   synthetic manifest self-project, which doesn't have a
//!   `refs/heads/manifest-rev`.
//! - **Working tree is dirty** — uncommitted changes (any line of
//!   `git status --porcelain=v1`).
//! - **A local branch is checked out** — `--ignore-branches`
//!   (or `compare.ignore-branches = true`) suppresses this signal.
//!   Mirrors v1's behaviour: a fresh `west update` leaves the
//!   working tree detached at `manifest-rev`, so being on a
//!   branch IS a divergence signal unless the user opted out.
//!
//! Per-project output, when shown:
//!
//! ```text
//! === <name> (<path>):
//! --- manifest-rev: a1b2c3d <subject>
//!             HEAD: e4f5g6h <subject>
//! --- status:
//!     <git status body, indented 4 spaces>
//! ```
//!
//! The colon-aligned `manifest-rev:` / `HEAD:` labels match v1's
//! layout. The body indentation is python v1's
//! `textwrap.indent(..., ' ' * 4)`.
//!
//! The `=== <name> (<path>):` banner is chrome and goes to stderr;
//! only the comparison body (and the `-f` machine-readable lines)
//! reach stdout, matching `diff` / `status` / `forall`.

use std::io::{self, IsTerminal, Write};
use std::path::Path;
use std::process::ExitCode;

use clap::{Args, ValueEnum};
use console::Style;
use rayon::prelude::*;

use west_core::config::{ConfigValue, Configuration};
use west_core::manifest::Project;
use west_core::vcs::{self, ColorMode, CommitSummary, RevSpec, StatusMode, StatusSpec, Vcs, VcsError};

use super::config::LoadedConfig;
use super::select;

const MAX_DEFAULT_JOBS: usize = 8;

#[derive(Args, Debug)]
pub struct CompareArgs {
    /// Project names or paths. Empty = every active cloned project.
    #[arg(value_name = "PROJECT")]
    pub projects: Vec<String>,

    /// Include inactive projects.
    #[arg(short, long)]
    pub all: bool,

    /// Exit 1 if any project produced output. Useful for CI
    /// gating ("fail if any project diverges from the manifest").
    #[arg(long = "exit-code")]
    pub exit_code: bool,

    /// Suppress output for projects whose only divergence signal
    /// is "a branch is checked out" (i.e. HEAD matches
    /// manifest-rev, working tree clean, on a branch). Composes
    /// with `compare.ignore-branches` config and the matching
    /// `--no-ignore-branches` flag: between the two flags, the
    /// LAST one on the command line wins (matches python
    /// `argparse.BooleanOptionalAction`); when neither is given,
    /// the config option supplies the default.
    #[arg(
        long = "ignore-branches",
        action = clap::ArgAction::SetTrue,
        overrides_with = "no_ignore_branches",
    )]
    pub ignore_branches: bool,

    /// Opposite of `--ignore-branches`. See its doc for the
    /// last-wins composition rule.
    #[arg(
        long = "no-ignore-branches",
        action = clap::ArgAction::SetTrue,
        overrides_with = "ignore_branches",
    )]
    pub no_ignore_branches: bool,

    /// Colorize output. `auto` (default) emits color when stdout
    /// is a TTY.
    #[arg(long, value_enum, default_value_t = ColorArg::Auto)]
    pub color: ColorArg,

    /// Maximum projects to inspect concurrently. Twin of
    /// `compare.jobs` config key; defaults to
    /// `min(num_cpus, 8)`.
    #[arg(short = 'j', long, value_name = "N")]
    pub jobs: Option<usize>,

    /// Format string. When set, replaces the human-readable
    /// per-project block (`=== name ...`) with one line per dirty
    /// project rendered through this template. Same keys as
    /// `west list -f` (`{name}`, `{path}`, `{sha}`, `{groups}`, …).
    /// Designed for machine-readable output.
    #[arg(short = 'f', long, value_name = "FMT")]
    pub format: Option<String>,
}

#[derive(ValueEnum, Clone, Copy, Debug)]
pub enum ColorArg {
    Always,
    Never,
    Auto,
}

#[derive(Debug, thiserror::Error)]
enum CompareError {
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
    /// Embeds [`super::project_format::FormatError`] transparently —
    /// keeps each `FormatError` variant addressable by `match`
    /// without duplicating them here, and preserves the user-facing
    /// message verbatim (no extra "manifest:" prefix).
    #[error(transparent)]
    Format(#[from] super::project_format::FormatError),
}

impl From<super::workspace::WorkspaceError> for CompareError {
    fn from(e: super::workspace::WorkspaceError) -> Self {
        match e {
            super::workspace::WorkspaceError::NotInWorkspace => CompareError::NotInWorkspace,
            super::workspace::WorkspaceError::Config(s) => CompareError::Config(s),
            super::workspace::WorkspaceError::Manifest(s) => CompareError::Manifest(s),
        }
    }
}

pub fn run(args: CompareArgs, loaded: &mut LoadedConfig) -> ExitCode {
    if let Err(e) = splice_flags_into_config(&args, &mut loaded.config) {
        eprintln!("west: {e}");
        return ExitCode::from(2);
    }
    match run_inner(args, loaded) {
        Ok(Outcome::AllAligned) => ExitCode::SUCCESS,
        Ok(Outcome::SomePrinted { exit_code_flag: false }) => ExitCode::SUCCESS,
        Ok(Outcome::SomePrinted { exit_code_flag: true }) => ExitCode::from(1),
        Ok(Outcome::Failures) => ExitCode::FAILURE,
        Err(e @ CompareError::UnclonedPositional { .. }) => {
            eprintln!("west: {e}");
            ExitCode::from(2)
        }
        Err(e) => {
            eprintln!("west: {e}");
            ExitCode::FAILURE
        }
    }
}

enum Outcome {
    /// No project produced output. Clean workspace, nothing to report.
    AllAligned,
    /// At least one project produced output. `exit_code_flag`
    /// carries the user's `--exit-code` choice.
    SomePrinted { exit_code_flag: bool },
    /// At least one project's inspection failed (vcs error). The
    /// per-project failure list was already printed.
    Failures,
}

fn run_inner(args: CompareArgs, loaded: &mut LoadedConfig) -> Result<Outcome, CompareError> {
    let workspace = super::workspace::resolve_workspace_dir()?;
    let vcs = vcs::from_config(&loaded.config).map_err(|e| CompareError::Vcs(e.to_string()))?;

    let source = super::workspace::ReadOnlyImportSource::new(workspace.as_path(), vcs.as_ref());
    let loaded_manifest = super::workspace::load_manifest(&workspace, &loaded.config, &source)?;
    let manifest = &loaded_manifest.manifest;

    let synthetic_path = super::workspace::manifest_path_from_config(&loaded.config)?;
    let synthetic = select::synthetic_manifest_project(manifest, synthetic_path);

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
                    .map_err(|e| CompareError::Manifest(e.to_string()))?,
            );
        }
        acc
    };

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
        return Err(CompareError::UnclonedPositional {
            names: uncloned_positional.join(", "),
        });
    }

    if projects.is_empty() {
        eprintln!("west: compare: no projects matched");
        return Ok(Outcome::AllAligned);
    }

    let settings = Settings::from_config(&loaded.config).map_err(CompareError::Config)?;
    let parallel = !settings.raw && settings.jobs > 1 && projects.len() > 1;
    let jobs = if parallel { settings.jobs } else { 1 };

    // `overrides_with` on the CLI pair guarantees clap leaves at
    // most one of `ignore_branches` / `no_ignore_branches` set
    // to `true` (the one that appeared LAST in argv — same shape
    // as python's `argparse.BooleanOptionalAction`). If neither
    // is true, the user didn't pass either flag and the config
    // option supplies the default.
    let ignore_branches = if args.ignore_branches {
        true
    } else if args.no_ignore_branches {
        false
    } else {
        loaded
            .config
            .get_bool("compare.ignore-branches")
            .map_err(|e| CompareError::Config(e.to_string()))?
            .unwrap_or(false)
    };

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

    // Identify the synthetic manifest project by pointer equality
    // against the `synthetic` local. Skipping the divergence
    // check for it is the right call because the self-tree
    // doesn't have `refs/heads/manifest-rev` (it's never `west
    // update`-ed) and we don't want to check for branches either
    // (the manifest repo is the user's own workflow).
    let outcomes: Vec<ProjectOutcome> = if parallel {
        let pool = rayon::ThreadPoolBuilder::new()
            .num_threads(jobs)
            .build()
            .map_err(|e| CompareError::Config(format!("failed to start worker pool: {e}")))?;
        pool.install(|| {
            projects
                .par_iter()
                .map(|p| {
                    let is_synthetic = std::ptr::eq(*p, &synthetic);
                    compare_one(
                        p,
                        &workspace,
                        vcs.as_ref(),
                        ignore_branches,
                        resolved_color,
                        is_synthetic,
                    )
                })
                .collect()
        })
    } else {
        projects
            .iter()
            .map(|p| {
                let is_synthetic = std::ptr::eq(*p, &synthetic);
                compare_one(
                    p,
                    &workspace,
                    vcs.as_ref(),
                    ignore_branches,
                    resolved_color,
                    is_synthetic,
                )
            })
            .collect()
    };

    // Drain in workspace order. Per-project banner (chrome) goes to
    // stderr; the comparison body (result) to stdout, matching
    // `diff` / `status` / `forall`. The `-f` machine-readable mode
    // is itself the result, so its lines stay on stdout.
    let mut stdout = io::stdout().lock();
    let mut stderr = io::stderr().lock();
    // Banner lands on stderr, so `auto` follows stderr's TTY-ness;
    // `--color always/never` force the choice. (`resolved_color`,
    // keyed off stdout, colours the embedded git status body.)
    let banner_style = match args.color {
        ColorArg::Always => Style::new().green().bright().bold().force_styling(true),
        ColorArg::Never => Style::new().force_styling(false),
        ColorArg::Auto => Style::new().green().bright().bold().for_stderr(),
    };
    let mut printed_any = false;
    let mut failures: Vec<(String, String)> = Vec::new();
    for o in outcomes {
        match o.result {
            Ok(None) => {} // Aligned project, no output.
            Ok(Some(body)) => {
                printed_any = true;
                if let Some(template) = args.format.as_deref() {
                    // Machine-readable mode: no banner, no body —
                    // just one line per dirty project rendered
                    // through the shared format engine.
                    let ctx = super::project_format::ProjectContext {
                        project: o.project,
                        loaded: &loaded_manifest,
                        workspace: workspace.as_path(),
                        vcs: vcs.as_ref(),
                    };
                    let line = super::project_format::render(template, &ctx)?;
                    let _ = writeln!(stdout, "{line}");
                } else {
                    if !settings.quiet {
                        // Flush stdout first so the banner heads its
                        // body when both share a terminal.
                        let _ = stdout.flush();
                        let _ = writeln!(
                            stderr,
                            "{}",
                            banner_style.apply_to(format!(
                                "=== {} ({}):",
                                o.project.name,
                                o.project.path.display(),
                            )),
                        );
                        let _ = stderr.flush();
                    }
                    let _ = stdout.write_all(&body);
                }
            }
            Err(e) => failures.push((o.project.name.clone(), e.to_string())),
        }
    }
    drop(stdout);
    drop(stderr);

    if !failures.is_empty() {
        for (name, msg) in &failures {
            eprintln!("west: compare failed for {name}: {msg}");
        }
        let names: Vec<&str> = failures.iter().map(|(n, _)| n.as_str()).collect();
        eprintln!(
            "west: compare failed for {} project{}: {}",
            names.len(),
            if names.len() == 1 { "" } else { "s" },
            names.join(", "),
        );
        return Ok(Outcome::Failures);
    }

    if printed_any {
        Ok(Outcome::SomePrinted {
            exit_code_flag: args.exit_code,
        })
    } else {
        Ok(Outcome::AllAligned)
    }
}

struct ProjectOutcome<'a> {
    project: &'a Project,
    /// `Ok(None)` = aligned (no output);
    /// `Ok(Some(body))` = output collected (without project
    /// banner — the drain loop adds that);
    /// `Err` = vcs failure during inspection.
    result: Result<Option<Vec<u8>>, VcsError>,
}

fn compare_one<'a>(
    project: &'a Project,
    workspace: &Path,
    vcs: &dyn Vcs,
    ignore_branches: bool,
    color: ColorMode,
    is_synthetic: bool,
) -> ProjectOutcome<'a> {
    let abspath = workspace.join(&project.path);
    let result = compare_one_inner(&abspath, vcs, ignore_branches, color, is_synthetic);
    ProjectOutcome { project, result }
}

fn compare_one_inner(
    repo: &Path,
    vcs: &dyn Vcs,
    ignore_branches: bool,
    color: ColorMode,
    is_synthetic: bool,
) -> Result<Option<Vec<u8>>, VcsError> {
    // For the synthetic manifest project: skip the HEAD-vs-manifest-rev
    // check (the self-tree has no `refs/heads/manifest-rev` since
    // it's never `west update`-ed), and skip the branch check. Only
    // the dirty signal triggers output.
    let mut head_summary: Option<CommitSummary> = None;
    let mut manifest_rev_summary: Option<CommitSummary> = None;
    let mut has_diverged = false;
    let mut on_branch = false;

    if !is_synthetic {
        let head_sha = vcs.sha(repo, RevSpec::Head)?;
        // manifest_rev() returns Ok(None) when the ref doesn't
        // exist — for a freshly-init'd workspace that hasn't been
        // updated. Treat as "no divergence to report".
        let manifest_rev_sha = vcs.manifest_rev(repo)?;
        if let Some(mr) = manifest_rev_sha.as_deref() {
            has_diverged = mr != head_sha;
            // Resolve commit summaries once so we don't run git
            // again at render time.
            head_summary = Some(vcs.commit_summary(repo, RevSpec::Named(&head_sha))?);
            manifest_rev_summary = Some(vcs.commit_summary(repo, RevSpec::Named(mr))?);
        }

        if !ignore_branches {
            on_branch = vcs.head_branch(repo)?.is_some();
        }
    }

    // `is_clean` is the simplest dirty check. For the synthetic
    // project this is the only signal we look at; for regular
    // projects it composes with the divergence + branch checks.
    let is_clean = vcs.is_clean(repo)?;

    let should_print = !is_clean || has_diverged || on_branch;
    if !should_print {
        return Ok(None);
    }

    let mut body: Vec<u8> = Vec::new();
    if let (Some(head), Some(mr)) = (head_summary.as_ref(), manifest_rev_summary.as_ref()) {
        // The 12-space indent before "HEAD:" aligns the colons
        // with `--- manifest-rev:` above (matches python v1).
        let _ = writeln!(body, "--- manifest-rev: {} {}", mr.short_sha, mr.subject);
        let _ = writeln!(body, "            HEAD: {} {}", head.short_sha, head.subject);
    }

    let _ = writeln!(body, "--- status:");
    // Status body: use Long mode so the user sees branch info +
    // detailed change descriptions, indented 4 spaces (matches
    // python v1's `textwrap.indent(..., ' ' * 4)`).
    let mut status_body: Vec<u8> = Vec::new();
    let status_spec = StatusSpec {
        mode: StatusMode::Long,
        color,
        extra_args: &[],
    };
    let _ = vcs.status(repo, &status_spec, &mut status_body)?;
    for line in String::from_utf8_lossy(&status_body).lines() {
        let _ = writeln!(body, "    {line}");
    }

    Ok(Some(body))
}

// =========================================================================
// Settings (mirrors diff.rs / status.rs)
// =========================================================================

#[derive(Debug)]
struct Settings {
    jobs: usize,
    raw: bool,
    quiet: bool,
}

impl Settings {
    fn from_config(config: &Configuration) -> Result<Self, String> {
        let jobs = match config.get("compare.jobs").map_err(|e| e.to_string())? {
            None => default_jobs(),
            Some(ConfigValue::Integer(i)) if i >= 1 => i as usize,
            Some(ConfigValue::Integer(i)) => {
                return Err(format!("compare.jobs must be a positive integer (got {i})"));
            }
            Some(other) => {
                return Err(format!("compare.jobs must be an integer (got {other:?})"));
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

fn splice_flags_into_config(args: &CompareArgs, config: &mut Configuration) -> Result<(), String> {
    if let Some(jobs) = args.jobs {
        super::config::splice_inline(config, "compare.jobs", ConfigValue::Integer(jobs as i64))?;
    }
    Ok(())
}

fn default_jobs() -> usize {
    std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(1)
        .clamp(1, MAX_DEFAULT_JOBS)
}
