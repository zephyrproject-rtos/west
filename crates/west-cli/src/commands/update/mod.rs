//! `west update` — bring every (or selected) project to its manifest revision.
//!
//! Architectural notes worth keeping nearby:
//!
//! - **Every CLI flag has a config-key twin** under `update.*` (or, for
//!   fetch/narrow, `tool.git.*`). The `--config` route is the single
//!   source of truth that the rest of the function reads from; flags
//!   just splice into inline config at the top of `run`. Same shape as
//!   `init.rs`.
//!
//! - **Output flows through writers, not return values.** The `Vcs`
//!   trait's progress-producing methods take a `&mut dyn io::Write`.
//!   We give each project's worker its own `Vec<u8>` buffer and ship
//!   the finished transcript to the [`Reporter`]. Serial vs parallel
//!   only changes the [`Reporter`] implementation.
//!
//! - **rayon for parallelism.** Subprocess work is blocking; rayon's
//!   thread pool is the right tool. We build a pool sized to
//!   `update.jobs` and let `par_iter` schedule project-sized work.
//!
//! - **keep-descendants beats rebase.** Both are bools (matching Python's
//!   `--keep-descendants` and `--rebase` flags). When both are set, the
//!   keep-descendants branch is taken when applicable, then we fall back
//!   through `rebase` and finally to detached checkout. Python parity
//!   (project.py:1695).

mod output;
mod select;

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use clap::{Args, ValueEnum};
use rayon::prelude::*;

use west_core::config::{ConfigValue, Configuration};
use west_core::manifest::{GroupFilterEntry, Manifest, Project, Submodules};
use west_core::vcs::{self, CheckoutTarget, FetchSpec, SubmoduleScope, Vcs, VcsError};

use super::config::LoadedConfig;
use output::{BufferingReporter, ProjectReport, Reporter, SerialReporter};

const DEFAULT_MANIFEST_FILE: &str = "west.yml";
/// Hard cap on the auto-default for `update.jobs`. Beyond this, contention on
/// shared resources (network, disk, ssh-agent) outweighs CPU parallelism.
const MAX_DEFAULT_JOBS: usize = 8;

#[derive(Args, Debug)]
pub struct UpdateArgs {
    /// Project names or paths to update. If empty, updates every project
    /// that's active under the combined manifest + CLI group filter.
    #[arg(value_name = "PROJECT")]
    pub projects: Vec<String>,

    /// Maximum projects to update concurrently. `1` disables parallelism.
    /// Equivalent to `--config update.jobs=N`.
    #[arg(short = 'j', long, value_name = "N")]
    pub jobs: Option<usize>,

    /// Append to the manifest's group filter. Format: `+grp,-other`.
    /// Repeatable. Equivalent to appending to `update.group-filter`.
    #[arg(
        long = "group-filter",
        visible_alias = "gf",
        action = clap::ArgAction::Append,
    )]
    pub group_filter: Vec<String>,

    /// Fetch strategy. Equivalent to `--config tool.git.fetch.strategy=...`.
    #[arg(short = 'f', long, value_enum)]
    pub fetch: Option<FetchArg>,

    /// Skip `--tags` in fetch. Equivalent to
    /// `--config tool.git.fetch.tags=false`.
    #[arg(short = 'n', long)]
    pub narrow: bool,

    /// If on a branch that's an ancestor of manifest-rev, keep it
    /// checked out instead of detaching.
    /// Equivalent to `--config update.keep-descendants=true`.
    #[arg(short = 'k', long = "keep-descendants")]
    pub keep_descendants: bool,

    /// Rebase the checked-out branch onto manifest-rev (instead of
    /// detaching). Loses to `--keep-descendants` when both are set.
    /// Equivalent to `--config update.rebase=true`.
    #[arg(short = 'r', long)]
    pub rebase: bool,
}

#[derive(Clone, Copy, Debug, ValueEnum)]
pub enum FetchArg {
    Smart,
    Always,
}

impl FetchArg {
    fn as_config_str(self) -> &'static str {
        match self {
            FetchArg::Smart => "smart",
            FetchArg::Always => "always",
        }
    }
}

pub fn run(args: UpdateArgs, loaded: &mut LoadedConfig) -> ExitCode {
    if let Err(e) = splice_flags_into_config(&args, &mut loaded.config) {
        eprintln!("west: {e}");
        return ExitCode::from(2);
    }

    let workspace = match resolve_workspace_dir() {
        Ok(p) => p,
        Err(e) => {
            eprintln!("west: {e}");
            return ExitCode::FAILURE;
        }
    };

    let manifest = match load_manifest(&workspace, &loaded.config) {
        Ok(m) => m,
        Err(e) => {
            eprintln!("west: {e}");
            return ExitCode::FAILURE;
        }
    };

    let cli_group_filter = match read_cli_group_filter(&loaded.config) {
        Ok(f) => f,
        Err(e) => {
            eprintln!("west: {e}");
            return ExitCode::from(2);
        }
    };

    let projects = match select::select_projects(&manifest, &args.projects, &cli_group_filter) {
        Ok(ps) => ps,
        Err(e) => {
            eprintln!("west: {e}");
            return ExitCode::FAILURE;
        }
    };

    if projects.is_empty() {
        eprintln!("west: no projects to update");
        return ExitCode::SUCCESS;
    }

    let settings = match Settings::from_config(&loaded.config) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("west: {e}");
            return ExitCode::from(2);
        }
    };

    let vcs = match vcs::from_config(&loaded.config) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("west: {e}");
            return ExitCode::FAILURE;
        }
    };

    let reporter: Box<dyn Reporter> = if settings.jobs == 1 {
        Box::new(SerialReporter::new())
    } else {
        Box::new(BufferingReporter::new())
    };

    let pool = match rayon::ThreadPoolBuilder::new()
        .num_threads(settings.jobs)
        .build()
    {
        Ok(p) => p,
        Err(e) => {
            eprintln!("west: failed to start worker pool: {e}");
            return ExitCode::FAILURE;
        }
    };

    let vcs_ref: &dyn Vcs = vcs.as_ref();
    let workspace_ref = workspace.as_path();
    let reporter_ref: &dyn Reporter = reporter.as_ref();

    pool.install(|| {
        projects.par_iter().for_each(|project| {
            let report = run_one_project(project, vcs_ref, workspace_ref, &settings);
            reporter_ref.project_finished(report);
        });
    });

    let summary = reporter.finish();
    if summary.is_empty() {
        ExitCode::SUCCESS
    } else {
        eprintln!("west: {}", summary.render());
        ExitCode::FAILURE
    }
}

// =====================================================================
// Per-project execution
// =====================================================================

#[derive(Debug, Clone, Copy)]
struct Settings {
    jobs: usize,
    rebase: bool,
    keep_descendants: bool,
}

impl Settings {
    fn from_config(config: &Configuration) -> Result<Self, String> {
        let jobs = match config.get("update.jobs").map_err(|e| e.to_string())? {
            None => default_jobs(),
            Some(ConfigValue::Integer(i)) if i >= 1 => i as usize,
            Some(ConfigValue::Integer(i)) => {
                return Err(format!("update.jobs must be a positive integer (got {i})"));
            }
            Some(other) => {
                return Err(format!("update.jobs must be an integer (got {other:?})"));
            }
        };
        let rebase = config
            .get_bool("update.rebase")
            .map_err(|e| e.to_string())?
            .unwrap_or(false);
        let keep_descendants = config
            .get_bool("update.keep-descendants")
            .map_err(|e| e.to_string())?
            .unwrap_or(false);
        Ok(Self {
            jobs,
            rebase,
            keep_descendants,
        })
    }
}

fn run_one_project(
    project: &Project,
    vcs: &dyn Vcs,
    workspace: &Path,
    settings: &Settings,
) -> ProjectReport {
    let repo = workspace.join(&project.path);
    let mut report = ProjectReport::new(project.name.clone(), project.path.clone());
    if let Err(e) = report.write_banner() {
        report.outcome = Err(format!("failed to write banner: {e}"));
        return report;
    }

    if let Err(e) = update_project(vcs, project, &repo, settings, &mut report.captured) {
        // Note: any subprocess output already lives in `report.captured`
        // because we passed it as the writer; we just need to record the
        // failure tag and the error string for the summary.
        let _ = writeln!(report.captured, "ERROR: {e}");
        report.outcome = Err(e);
    }
    report
}

/// The Python parity sequence (project.py:1657–1740). Errors short-circuit
/// the project; later steps don't run.
fn update_project(
    vcs: &dyn Vcs,
    project: &Project,
    repo: &Path,
    settings: &Settings,
    out: &mut Vec<u8>,
) -> Result<(), String> {
    // 1. Ensure cloned.
    let already_cloned = repo.exists() && vcs.is_repo(repo).unwrap_or(false);
    if !already_cloned {
        if let Some(parent) = repo.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|e| format!("failed to create {}: {e}", parent.display()))?;
        }
        vcs.clone(
            &project.url,
            repo,
            Some(&project.revision),
            Some(&project.remote_name),
            out,
        )
        .map_err(stringify)?;
    }

    // 2. Fetch (smart-skip / depth / tags / force all live in GitClient).
    vcs.fetch(
        repo,
        &FetchSpec {
            remote: &project.remote_name,
            revision: Some(&project.revision),
        },
        out,
    )
    .map_err(stringify)?;

    // 3. Resolve the new manifest-rev sha. After an active fetch git's
    //    FETCH_HEAD points at the just-fetched tip, which is what we want
    //    even when `revision` is a branch name (git doesn't write the
    //    matching `refs/heads/<branch>` on plain fetch). On smart-skip no
    //    fetch ran, so FETCH_HEAD may be absent or stale — fall back to
    //    resolving `revision` locally, which by definition is current
    //    when smart-skip kicked in.
    let sha = match vcs.sha(repo, "FETCH_HEAD") {
        Ok(s) => s,
        Err(_) => vcs.sha(repo, &project.revision).map_err(stringify)?,
    };

    // 4. Record manifest-rev.
    let reason = format!("west update: moving to {}", project.revision);
    vcs.set_manifest_rev(repo, &sha, Some(&reason))
        .map_err(stringify)?;

    // 5. Decide strategy.
    let head_branch = vcs.head_branch(repo).map_err(stringify)?;
    let detach = match (
        settings.keep_descendants,
        settings.rebase,
        head_branch.as_deref(),
    ) {
        (true, _, Some(branch)) => {
            // keep_descendants: keep current branch checked out only if the
            // new sha is already an ancestor of it.
            let is_ancestor = vcs.is_ancestor(repo, &sha, branch).map_err(stringify)?;
            if is_ancestor {
                let _ = writeln!(
                    out,
                    "keeping branch {branch:?} (manifest-rev is an ancestor)"
                );
                false
            } else {
                // Fall through to rebase if requested, else detach.
                if settings.rebase {
                    vcs.rebase(repo, "refs/heads/manifest-rev", out)
                        .map_err(stringify)?;
                    false
                } else {
                    true
                }
            }
        }
        (false, true, Some(_)) => {
            // rebase the current branch onto manifest-rev.
            vcs.rebase(repo, "refs/heads/manifest-rev", out)
                .map_err(stringify)?;
            false
        }
        _ => true,
    };

    if detach {
        vcs.checkout(repo, &CheckoutTarget::Detached(&sha))
            .map_err(stringify)?;
    }

    // 6. Submodules.
    let scope = submodules_scope(&project.submodules);
    if !matches!(scope, ScopeOwned::Skip) {
        run_submodules(vcs, repo, &scope, out)?;
    }

    Ok(())
}

/// Owned counterpart of [`SubmoduleScope`] so we can keep the path strings
/// alive while building the borrowed `&[&str]` passed to the vcs call.
enum ScopeOwned {
    All,
    Specific(Vec<String>),
    Skip,
}

fn submodules_scope(s: &Submodules) -> ScopeOwned {
    match s {
        Submodules::All => ScopeOwned::All,
        Submodules::None => ScopeOwned::Skip,
        Submodules::Specific(items) => ScopeOwned::Specific(
            items
                .iter()
                .map(|s| s.path.to_string_lossy().into_owned())
                .collect(),
        ),
    }
}

fn run_submodules(
    vcs: &dyn Vcs,
    repo: &Path,
    scope: &ScopeOwned,
    out: &mut dyn Write,
) -> Result<(), String> {
    match scope {
        ScopeOwned::All => vcs
            .update_submodules(repo, &SubmoduleScope::All, out)
            .map_err(stringify),
        ScopeOwned::Skip => Ok(()),
        ScopeOwned::Specific(strings) => {
            let refs: Vec<&str> = strings.iter().map(String::as_str).collect();
            vcs.update_submodules(repo, &SubmoduleScope::Specific(&refs), out)
                .map_err(stringify)
        }
    }
}

fn stringify(e: VcsError) -> String {
    e.to_string()
}

// =====================================================================
// Config plumbing
// =====================================================================

fn splice_flags_into_config(args: &UpdateArgs, config: &mut Configuration) -> Result<(), String> {
    if let Some(jobs) = args.jobs {
        config
            .set_inline("update.jobs", ConfigValue::Integer(jobs as i64))
            .map_err(|e| e.to_string())?;
    }
    if args.rebase {
        config
            .set_inline("update.rebase", ConfigValue::Bool(true))
            .map_err(|e| e.to_string())?;
    }
    if args.keep_descendants {
        config
            .set_inline("update.keep-descendants", ConfigValue::Bool(true))
            .map_err(|e| e.to_string())?;
    }
    if args.narrow {
        config
            .set_inline("tool.git.fetch.tags", ConfigValue::Bool(false))
            .map_err(|e| e.to_string())?;
    }
    if let Some(f) = args.fetch {
        config
            .set_inline(
                "tool.git.fetch.strategy",
                ConfigValue::String(f.as_config_str().to_owned()),
            )
            .map_err(|e| e.to_string())?;
    }
    if !args.group_filter.is_empty() {
        // Append to whatever is already present in `update.group-filter`.
        let mut combined: Vec<ConfigValue> = match config
            .get("update.group-filter")
            .map_err(|e| e.to_string())?
        {
            None => Vec::new(),
            Some(ConfigValue::List(items)) => items,
            Some(other) => {
                return Err(format!("update.group-filter must be a list, got {other:?}"));
            }
        };
        for raw in &args.group_filter {
            combined.push(ConfigValue::String(raw.clone()));
        }
        config
            .set_inline("update.group-filter", ConfigValue::List(combined))
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

fn read_cli_group_filter(config: &Configuration) -> Result<Vec<GroupFilterEntry>, String> {
    let raw = match config
        .get("update.group-filter")
        .map_err(|e| e.to_string())?
    {
        None => return Ok(Vec::new()),
        Some(ConfigValue::List(items)) => items,
        Some(other) => {
            return Err(format!("update.group-filter must be a list, got {other:?}"));
        }
    };
    let mut strings: Vec<String> = Vec::with_capacity(raw.len());
    for item in raw {
        match item {
            ConfigValue::String(s) => strings.push(s),
            other => {
                return Err(format!(
                    "update.group-filter entries must be strings, got {other:?}"
                ));
            }
        }
    }
    west_core::manifest::parse_cli_group_filter(&strings).map_err(|e| e.to_string())
}

// =====================================================================
// Workspace + manifest loading
// =====================================================================

fn resolve_workspace_dir() -> Result<PathBuf, String> {
    let cwd = std::env::current_dir().map_err(|e| format!("cannot get current directory: {e}"))?;
    west_core::topdir::topdir(&cwd)
        .map_err(|_| "not inside a west workspace (no .west/ found)".to_string())
}

fn load_manifest(workspace: &Path, config: &Configuration) -> Result<Manifest, String> {
    let manifest_path: PathBuf = config
        .get_str("manifest.path")
        .map_err(|e| e.to_string())?
        .map(PathBuf::from)
        .ok_or_else(|| "manifest.path is not set in workspace config".to_string())?;
    let manifest_file: PathBuf = config
        .get_str("manifest.file")
        .map_err(|e| e.to_string())?
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(DEFAULT_MANIFEST_FILE));
    let full = workspace.join(&manifest_path).join(&manifest_file);
    // Lenient: tolerate `import:` directives by warning instead of erroring,
    // so update can do useful work on the directly-defined projects even
    // while full import resolution remains unimplemented.
    Manifest::from_path_lenient(&full)
        .map_err(|e| format!("manifest {}: {e}", full.display()))
}

fn default_jobs() -> usize {
    std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(1)
        .clamp(1, MAX_DEFAULT_JOBS)
}
