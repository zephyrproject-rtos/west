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
//! - **keep-descendants beats rebase.** Both are independently-settable
//!   booleans. When both are on, the keep-descendants branch is taken
//!   when applicable; otherwise we fall back through `rebase`, and
//!   finally to detached checkout. The "keep-descendants wins" rule is
//!   load-bearing: a user who sets both expects the safer of the two.

mod cache;
mod error;
mod import_source;
mod indicatif_reporter;
mod output;

use std::io::{self, IsTerminal};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use clap::{Args, ValueEnum};
use rayon::prelude::*;

use west_core::config::{ConfigValue, Configuration};
use west_core::loaded::{LoadedManifest, ProjectFilter};
use west_core::manifest::{GroupFilterEntry, ImportPolicy, Manifest, Project, Submodules};
use west_core::vcs::{
    self, CheckoutTarget, CommitSummary, FetchSpec, Output, RevSpec, SubmoduleScope,
    SubmoduleStrategy, Vcs,
};

use super::color::ColorArg;
use super::config::LoadedConfig;
use error::UpdateError;
use indicatif_reporter::IndicatifReporter;
use output::{BufferingReporter, Reporter, SerialReporter};

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

    /// Fetch just the requested revision: skip tags and fetch the
    /// revision directly (may fail for a SHA on some hosts). Equivalent
    /// to `--config tool.git.fetch.narrow=true`.
    #[arg(short = 'n', long)]
    pub narrow: bool,

    /// Extra option to pass through to `git fetch` (e.g.
    /// `-o=--depth=1`). Repeatable. Appends to
    /// `tool.git.fetch.extra-args`.
    #[arg(short = 'o', long = "fetch-opt", value_name = "OPT", action = clap::ArgAction::Append)]
    pub fetch_opt: Vec<String>,

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

    /// Look up cached repos at `<DIR>/<project.name>` to avoid
    /// network round-trips. Highest priority of the three cache
    /// flags. Equivalent to `--config update.name-cache=DIR`.
    #[arg(long = "name-cache", value_name = "DIR")]
    pub name_cache: Option<PathBuf>,

    /// Look up cached repos at `<DIR>/<project.path>`. Lower priority
    /// than `--name-cache`. Equivalent to `--config update.path-cache=DIR`.
    #[arg(long = "path-cache", value_name = "DIR")]
    pub path_cache: Option<PathBuf>,

    /// Maintain auto-populated bare-mirror caches under `<DIR>`.
    /// Lowest priority but the only mode west populates and refreshes
    /// itself. Equivalent to `--config update.auto-cache=DIR`.
    #[arg(long = "auto-cache", value_name = "DIR")]
    pub auto_cache: Option<PathBuf>,

    /// Colorize the per-project banner (only emitted on the native-stdio
    /// path — the indicatif path renders bars instead). Unset, the
    /// resolver consults `color.ui` before falling back to TTY-aware
    /// `auto`.
    #[arg(long, value_enum)]
    pub color: Option<ColorArg>,
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
        log::error!("{e}");
        return ExitCode::from(2);
    }

    let workspace = match resolve_workspace_dir() {
        Ok(p) => p,
        Err(e) => {
            log::error!("{e}");
            return ExitCode::FAILURE;
        }
    };

    // The vcs has to exist before we load the manifest: per-project
    // imports need it to fetch + read each importing project's manifest
    // file at its manifest revision.
    let vcs = match vcs::from_config(&loaded.config) {
        Ok(v) => v,
        Err(e) => {
            log::error!("{e}");
            return ExitCode::FAILURE;
        }
    };

    // Hoisted so [`load_manifest`] can hand `Settings` to the import
    // source — the auto-cache lives in `update.auto-cache` and any
    // import-resolution clones must route through the same cache the
    // main worker pool uses, so a single network transfer per
    // imported project covers both phases. Validation errors here
    // surface before any clone happens.
    let mut settings = match Settings::from_config(&loaded.config) {
        Ok(s) => s,
        Err(e) => {
            log::error!("{e}");
            return ExitCode::from(2);
        }
    };
    settings.color = match super::color::resolve(args.color, &loaded.config, None) {
        Ok(c) => c,
        Err(e) => {
            log::error!("{e}");
            return ExitCode::from(2);
        }
    };

    // Decide whether to drive an indicatif progress bar for any
    // import-resolution clones the manifest load triggers. Same rule
    // the main worker pool uses below: TTY + non-raw → indicatif;
    // otherwise (raw or non-TTY) git stdio attaches natively to the
    // parent's terminal.
    let import_progress =
        (!settings.raw && io::stderr().is_terminal()).then(import_source::ImportProgress::new);

    // Selector-driven scope (v1 contract): when the user passes
    // explicit project selectors, only the manifest repo + its self/
    // top-level imports are resolved — per-project `import:` bodies
    // are NOT chased. This serves two purposes:
    //
    //   1. Validates that each selector names a project the manifest
    //      directly knows about (anything reachable only via a
    //      project import would silently make us clone the parent
    //      project to discover it; reject those selectors instead).
    //   2. Skips the network entirely for project imports the user
    //      didn't ask for — `west update net-tools` against a
    //      manifest with `zephyr: import: true` no longer clones
    //      zephyr just to learn what its body would have contributed.
    //
    // Bare `west update` (no selectors) keeps the full-resolve path
    // because the user explicitly asked for everything.
    let normalized_selectors: Vec<String> = args
        .projects
        .iter()
        .map(|s| super::workspace::normalize_project_selector(s, &workspace))
        .collect();
    let manifest = if normalized_selectors.is_empty() {
        match load_manifest(
            &workspace,
            &loaded.config,
            vcs.as_ref(),
            &settings,
            import_progress.as_ref(),
        ) {
            Ok(m) => m,
            Err(e) => {
                log::error!("{e}");
                return ExitCode::FAILURE;
            }
        }
    } else {
        match manifest_repo_only_view(&workspace, &loaded.config) {
            Ok(m) => {
                if let Err(e) = reject_selectors_needing_project_imports(
                    &m,
                    &normalized_selectors,
                    &loaded.config,
                ) {
                    log::error!("{e}");
                    return ExitCode::FAILURE;
                }
                m
            }
            Err(e) => {
                log::error!("{e}");
                return ExitCode::FAILURE;
            }
        }
    };
    // Drop the import-progress MultiProgress before the main worker
    // pool creates its own — keeps the two from fighting over the
    // terminal.
    drop(import_progress);

    let cli_group_filter = match read_cli_group_filter(&loaded.config) {
        Ok(f) => f,
        Err(e) => {
            log::error!("{e}");
            return ExitCode::from(2);
        }
    };
    // Wrap the raw Manifest with the workspace-derived filters
    // (`manifest.group-filter`, `manifest.project-filter`) so every
    // activity check inside `select_projects` honors them. The CLI's
    // own `--group-filter` is layered on top via the `cli_filter`
    // argument to `select_projects`.
    let config_group_filter = match super::select::read_manifest_group_filter(&loaded.config) {
        Ok(f) => f,
        Err(e) => {
            log::error!("{e}");
            return ExitCode::from(2);
        }
    };
    let project_filter = match ProjectFilter::from_config(&loaded.config) {
        Ok(f) => f,
        Err(e) => {
            log::error!("{e}");
            return ExitCode::from(2);
        }
    };
    let loaded_manifest = LoadedManifest::new(manifest, config_group_filter, project_filter);

    let projects = match super::select::select_projects(
        &loaded_manifest,
        &normalized_selectors,
        &cli_group_filter,
    ) {
        Ok(ps) => ps,
        Err(e) => {
            log::error!("{e}");
            return ExitCode::FAILURE;
        }
    };

    if projects.is_empty() {
        log::warn!("no projects to update");
        return ExitCode::SUCCESS;
    }

    // Pick the reporter (and the worker's `Output` mode) by (raw, tty,
    // parallel). `raw` forces serial + native stdio (interleaved native
    // git output across N projects is unreadable). On a TTY without
    // raw, indicatif drives a per-project bar regardless of `-j` (a
    // `-j 1` run still gets the nice single-bar experience). Off-TTY
    // and parallel: BufferingReporter dumps transcripts in arrival
    // order. Off-TTY and serial without raw: SerialReporter + native,
    // same as today's CI flow.
    let raw = settings.raw;
    let stderr_is_tty = io::stderr().is_terminal();
    let parallel = !raw && settings.jobs > 1;
    let use_stream = !raw && (stderr_is_tty || parallel);
    let jobs = if raw { 1 } else { settings.jobs };

    let reporter: Box<dyn Reporter> = if raw {
        Box::new(SerialReporter::new())
    } else if stderr_is_tty {
        Box::new(IndicatifReporter::new(projects.len()))
    } else if parallel {
        Box::new(BufferingReporter::new())
    } else {
        Box::new(SerialReporter::new())
    };

    let pool = match rayon::ThreadPoolBuilder::new().num_threads(jobs).build() {
        Ok(p) => p,
        Err(e) => {
            log::error!("failed to start worker pool: {e}");
            return ExitCode::FAILURE;
        }
    };

    let vcs_ref: &dyn Vcs = vcs.as_ref();
    let workspace_ref = workspace.as_path();
    let reporter_ref: &dyn Reporter = reporter.as_ref();

    pool.install(|| {
        projects.par_iter().for_each(|project| {
            let outcome = run_one_project(
                project,
                vcs_ref,
                workspace_ref,
                &settings,
                reporter_ref,
                use_stream,
            );
            reporter_ref.project_finished(&project.name, outcome);
        });
    });

    let summary = reporter.finish();
    if summary.is_empty() {
        ExitCode::SUCCESS
    } else {
        log::error!("{}", summary.render());
        ExitCode::FAILURE
    }
}

// =====================================================================
// Per-project execution
// =====================================================================

#[derive(Debug)]
pub(super) struct Settings {
    jobs: usize,
    rebase: bool,
    keep_descendants: bool,
    raw: bool,
    pub(super) name_cache: Option<PathBuf>,
    pub(super) path_cache: Option<PathBuf>,
    pub(super) auto_cache: Option<PathBuf>,
    /// Resolved `--color` choice for the per-project banner on the
    /// native-stdio path. Populated by `run()` after splice; not derived
    /// from a config key (update has no `update.color` today), so kept
    /// out of `from_config` and assigned post-hoc.
    pub(super) color: ColorArg,
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
        let raw = config
            .get_bool("output.raw")
            .map_err(|e| e.to_string())?
            .unwrap_or(false);
        let name_cache = read_cache_dir(config, "update.name-cache")?;
        let path_cache = read_cache_dir(config, "update.path-cache")?;
        let auto_cache = read_cache_dir(config, "update.auto-cache")?;
        Ok(Self {
            jobs,
            rebase,
            keep_descendants,
            raw,
            name_cache,
            path_cache,
            auto_cache,
            color: ColorArg::Auto,
        })
    }
}

fn read_cache_dir(config: &Configuration, key: &str) -> Result<Option<PathBuf>, String> {
    Ok(config
        .get_str(key)
        .map_err(|e| e.to_string())?
        .map(PathBuf::from))
}

fn run_one_project(
    project: &Project,
    vcs: &dyn Vcs,
    workspace: &Path,
    settings: &Settings,
    reporter: &dyn Reporter,
    use_stream: bool,
) -> Result<CommitSummary, UpdateError> {
    let repo = workspace.join(&project.path);
    if use_stream {
        let mut sink = reporter.sink_for_project(&project.name);
        let mut out = Output::Stream(sink.as_mut());
        run_project_steps(vcs, project, &repo, settings, &mut out)
    } else {
        // Native stdio: banner via stderr (the underlying tool's own
        // progress lands directly on the terminal that follows).
        //
        // Raw `eprintln!` rather than `log::info!`: this arm runs
        // only when the indicatif reporter wasn't chosen (serial
        // `-j1` or `--raw`), so no bar is live on the global
        // MultiProgress and we don't need `suspend()`. A future
        // parallel-non-TTY path would need revisiting if it grew
        // live output that races with these per-project banners.
        let banner_style = super::style::banner(settings.color);
        eprintln!(
            "{}",
            banner_style.apply_to(format!(
                "=== updating {} ({})",
                project.name,
                project.path.display()
            ))
        );
        let mut out = Output::Native;
        run_project_steps(vcs, project, &repo, settings, &mut out)
    }
}

/// Per-project sequence: ensure cloned, fetch, resolve manifest-rev,
/// record manifest-rev, choose checkout strategy (keep-descendants /
/// rebase / detach), then submodules. Errors short-circuit the project;
/// later steps don't run.
fn run_project_steps(
    vcs: &dyn Vcs,
    project: &Project,
    repo: &Path,
    settings: &Settings,
    out: &mut Output<'_>,
) -> Result<CommitSummary, UpdateError> {
    // 1. Ensure the repo exists. `cache::materialize` inits an empty
    //    repo (no cache) or seeds it with a managed clone from a local
    //    cache; either way it leaves no working revision and no stray
    //    branch. The subsequent fetch + detached checkout (steps 2 &
    //    5) land the exact manifest revision — which is why we never
    //    need a clone --branch here (manifests routinely pin bare SHAs
    //    that --branch wouldn't accept anyway).
    //
    // If a cache flag matched, the clone source is the cache directory
    // (a local path) instead of the project URL. The auto-cache branch
    // populates / refreshes the cache first so the workspace clone
    // never hits the network. After a cache-driven clone the recorded
    // origin URL is flipped back to the project URL — subsequent fetches
    // go to the real remote.
    //
    // Auto-cache refresh runs unconditionally (not just when we'd
    // clone from it): a workspace might already be cloned, but the
    // cache should still advance to the latest upstream so other
    // workspaces sharing the same auto-cache root hit a current
    // mirror. The `WorkspaceImportSource` flow (import-resolution
    // clone of import-providing projects) calls the same
    // [`cache::clone_via_cache`] helper, so a project that gets
    // touched by both phases hits the network exactly once.
    let cache_source = cache::resolve_cache_source(project, settings);
    if let Some(cache::CacheSource::Auto(path)) = &cache_source {
        cache::ensure_auto_cache(vcs, project, path, out)?;
    }
    let already_cloned = repo.exists() && vcs.is_repo(repo).unwrap_or(false);
    if !already_cloned {
        cache::materialize(vcs, project, settings, repo, out)?;
    }

    // 2. Fetch. `Vcs::fetch` returns the sha that the requested revision
    //    now resolves to — `FETCH_HEAD^{commit}` after an active fetch, or
    //    the locally-resolved revision on smart-skip. Don't sniff
    //    `FETCH_HEAD` directly: it persists across runs, so on smart-skip
    //    it's stale from a previous fetch and would point at the wrong
    //    commit.
    // Fetch by URL, not by configured git-remote name — v1 contract.
    // The `[remote "<name>"]` set up by clone is a user convenience
    // (so they can `git fetch <name>` directly); west itself never
    // depends on it, so an `as_yaml()` round-trip that drops
    // `remote-name:` doesn't break the next `west update`.
    let sha = vcs
        .fetch(
            repo,
            &FetchSpec {
                remote: &project.url,
                revision: Some(&project.revision),
            },
            out,
        )
        .map_err(|source| UpdateError::Fetch {
            remote: project.url.clone(),
            source,
        })?;

    // 3. Record manifest-rev.
    let reason = format!("west update: moving to {}", project.revision);
    vcs.set_manifest_rev(repo, &sha, Some(&reason))
        .map_err(UpdateError::SetManifestRev)?;

    // 5. Decide strategy. Mirrors v1's decide_update_strategy +
    //    post_checkout_help: compute `is_ancestor` once (is the new
    //    manifest-rev already contained in the checked-out branch?),
    //    then keep-descendants beats rebase beats detached checkout.
    let head_branch = vcs.head_branch(repo).map_err(UpdateError::HeadBranch)?;
    let is_ancestor = match head_branch.as_deref() {
        Some(branch) => vcs
            .is_ancestor(repo, RevSpec::Named(&sha), RevSpec::Named(branch))
            .map_err(UpdateError::IsAncestor)?,
        None => false,
    };
    let detach = if let Some(branch) = head_branch.as_deref() {
        if settings.keep_descendants && is_ancestor {
            // The branch already contains manifest-rev: leave it checked
            // out (keep-descendants takes priority over --rebase).
            log::info!(
                "{}: left descendant branch {branch:?} checked out",
                project.name,
            );
            false
        } else if settings.rebase {
            log::info!("{}: rebasing to manifest-rev {sha}", project.name);
            vcs.rebase(repo, RevSpec::ManifestRev, out)
                .map_err(UpdateError::Rebase)?;
            false
        } else {
            true
        }
    } else {
        // Already detached — nothing to keep or rebase.
        true
    };

    if detach {
        vcs.checkout(repo, &CheckoutTarget::Detached(&sha), out)
            .map_err(|source| UpdateError::Checkout {
                sha: sha.clone(),
                source,
            })?;
        // A branch was checked out before this detach: tell the user
        // it's been left behind and how to get back to it (v1's
        // post_checkout_help). WARN, so it shows by default.
        if let Some(branch) = head_branch.as_deref() {
            post_checkout_help(project, repo, branch, &sha, is_ancestor);
        }
    }

    // 6. Submodules. If the parent project was cache-cloned and the
    //    cache also contains the submodule sub-tree, pass it as
    //    `--reference` to `git submodule update` so the submodule
    //    init reuses the cached objects too.
    let scope = submodules_scope(&project.submodules);
    if !matches!(scope, ScopeOwned::Skip) {
        let sub_strategy = if settings.rebase {
            SubmoduleStrategy::Rebase
        } else {
            SubmoduleStrategy::Checkout
        };
        run_submodules(vcs, repo, &scope, sub_strategy, cache_source.as_ref(), out)?;
    }

    // 7. One-line snapshot of where HEAD landed, for the reporter to
    //    surface in its success line / per-project transcript.
    vcs.commit_summary(repo, RevSpec::Head)
        .map_err(UpdateError::CommitSummary)
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
    strategy: SubmoduleStrategy,
    cache_source: Option<&cache::CacheSource>,
    out: &mut Output<'_>,
) -> Result<(), UpdateError> {
    match scope {
        ScopeOwned::All => vcs
            // `--reference` is per-call and applies to every submodule
            // git initialises. We don't enumerate submodules ourselves
            // for the All scope (matches python), so cache reference
            // doesn't apply here — pass `None`.
            .update_submodules(repo, &SubmoduleScope::All, strategy, None, out)
            .map_err(UpdateError::Submodules),
        ScopeOwned::Skip => Ok(()),
        ScopeOwned::Specific(strings) => {
            // Per-submodule loop so each one gets its own `--reference`
            // probe: <cache>/<sub.path> if present, else None. Single-
            // submodule git invocations are slightly more expensive than
            // batching, but `git submodule update --reference` applies
            // once per call so batching wouldn't allow per-submodule
            // refs anyway.
            for sub_path in strings {
                let single = [sub_path.as_str()];
                let reference = cache_source
                    .map(|src| src.path().join(sub_path))
                    .filter(|p| p.is_dir());
                vcs.update_submodules(
                    repo,
                    &SubmoduleScope::Specific(&single),
                    strategy,
                    reference.as_deref(),
                    out,
                )
                .map_err(UpdateError::Submodules)?;
            }
            Ok(())
        }
    }
}

/// Warn that a detached checkout left a local branch behind, and show
/// the exact command to get back to it — fast-forward when the branch
/// already contains the new manifest-rev, rebase otherwise. Mirrors
/// v1's `post_checkout_help`; emitted at WARN so it shows by default,
/// with the "automate this" pointer at DEBUG.
///
/// Routed through `log::` (not the per-project transcript) on purpose:
/// it fires on a *successful* update, and the indicatif reporter only
/// replays a project's transcript on failure — so a transcript note
/// would be invisible here. The Phase-2 logger suspends the live bars
/// to print it above them.
fn post_checkout_help(project: &Project, repo: &Path, branch: &str, sha: &str, is_ancestor: bool) {
    let path = repo.display();
    if is_ancestor {
        log::warn!(
            "left behind {} branch {branch:?}; to switch back to it (fast forward):\n  git -C {path} checkout {branch}",
            project.name,
        );
        log::debug!(
            "(To do this automatically in the future, use \"west update --keep-descendants\".)"
        );
    } else {
        log::warn!(
            "left behind {} branch {branch:?}; to rebase onto the new HEAD:\n  git -C {path} rebase {sha} {branch}",
            project.name,
        );
        log::debug!("(To do this automatically in the future, use \"west update --rebase\".)");
    }
}

// =====================================================================
// Config plumbing
// =====================================================================

fn splice_flags_into_config(args: &UpdateArgs, config: &mut Configuration) -> Result<(), String> {
    use super::config::splice_inline;

    if let Some(jobs) = args.jobs {
        splice_inline(config, "update.jobs", ConfigValue::Integer(jobs as i64))?;
    }
    if args.rebase {
        splice_inline(config, "update.rebase", ConfigValue::Bool(true))?;
    }
    if args.keep_descendants {
        splice_inline(config, "update.keep-descendants", ConfigValue::Bool(true))?;
    }
    // `--narrow` (CLI) OR `update.narrow` (config) enables the git
    // layer's narrow fetch: skip tags AND fetch the exact revision
    // directly (no all-branches scratch refspec, even for a SHA —
    // which may fail on some hosts, narrow's documented trade-off).
    // The git client reads `tool.git.fetch.narrow`; map either source
    // onto it. The CLI flag wins when set; otherwise honor the
    // persisted config option.
    let narrow = args.narrow
        || config
            .get_bool("update.narrow")
            .map_err(|e| e.to_string())?
            .unwrap_or(false);
    if narrow {
        splice_inline(config, "tool.git.fetch.narrow", ConfigValue::Bool(true))?;
    }
    if let Some(f) = args.fetch {
        splice_inline(
            config,
            "tool.git.fetch.strategy",
            ConfigValue::String(f.as_config_str().to_owned()),
        )?;
    }
    if !args.fetch_opt.is_empty() {
        // Append to whatever is already present in
        // `tool.git.fetch.extra-args`.
        let mut combined: Vec<ConfigValue> = match config
            .get("tool.git.fetch.extra-args")
            .map_err(|e| e.to_string())?
        {
            None => Vec::new(),
            Some(ConfigValue::List(items)) => items,
            Some(other) => {
                return Err(format!(
                    "tool.git.fetch.extra-args must be a list, got {other:?}"
                ));
            }
        };
        for raw in &args.fetch_opt {
            combined.push(ConfigValue::String(raw.clone()));
        }
        splice_inline(
            config,
            "tool.git.fetch.extra-args",
            ConfigValue::List(combined),
        )?;
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
        splice_inline(config, "update.group-filter", ConfigValue::List(combined))?;
    }
    if let Some(p) = args.name_cache.as_deref() {
        splice_inline(
            config,
            "update.name-cache",
            ConfigValue::String(p.to_string_lossy().into_owned()),
        )?;
    }
    if let Some(p) = args.path_cache.as_deref() {
        splice_inline(
            config,
            "update.path-cache",
            ConfigValue::String(p.to_string_lossy().into_owned()),
        )?;
    }
    if let Some(p) = args.auto_cache.as_deref() {
        splice_inline(
            config,
            "update.auto-cache",
            ConfigValue::String(p.to_string_lossy().into_owned()),
        )?;
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

/// Preflight: reject user selectors that can't be resolved against
/// the manifest-repo-only view (`mr_only`). A selector is acceptable
/// if it matches a project's name OR its path. Selectors that would
/// only resolve after running per-project imports — which would
/// require cloning the parent project to read its manifest body —
/// land in `offenders` and abort the run. Unknown selectors hit the
/// same path: not in the view ⇒ no. The synthetic manifest project
/// (`SYNTHETIC_NAME` or the configured `manifest.path`) is rejected
/// up front with a dedicated message — it's not in `mr_only.projects`
/// either, but the underlying reason is different from "behind an
/// import" and the user-facing fix is also different.
fn reject_selectors_needing_project_imports(
    mr_only: &Manifest,
    selectors: &[String],
    config: &Configuration,
) -> Result<(), String> {
    let manifest_path = config
        .get_str("manifest.path")
        .map_err(|e| e.to_string())?
        .unwrap_or_default();
    let manifest_offenders: Vec<&str> = selectors
        .iter()
        .filter(|s| {
            s.as_str() == super::select::SYNTHETIC_NAME
                || (!manifest_path.is_empty() && s.as_str() == manifest_path)
        })
        .map(|s| s.as_str())
        .collect();
    if !manifest_offenders.is_empty() {
        return Err(format!(
            "cannot update {}: the manifest project itself is not a \
             west update target — use `west init` to change it",
            manifest_offenders.join(", "),
        ));
    }

    let names: std::collections::HashSet<&str> =
        mr_only.projects.iter().map(|p| p.name.as_str()).collect();
    let paths: std::collections::HashSet<String> = mr_only
        .projects
        .iter()
        .map(|p| p.path.to_string_lossy().into_owned())
        .collect();
    let offenders: Vec<&str> = selectors
        .iter()
        .filter(|s| !names.contains(s.as_str()) && !paths.contains(s.as_str()))
        .map(|s| s.as_str())
        .collect();
    if offenders.is_empty() {
        return Ok(());
    }
    Err(format!(
        "cannot update {}: project name not found in the manifest repo \
         (reachable only via a per-project `import:`, or unknown); \
         clone the parent project or run `west update` with no arguments first",
        offenders.join(", "),
    ))
}

/// Parse the manifest with `ImportPolicy::SKIP_PROJECTS` to get the
/// set of projects reachable from the manifest repo + its self/top-
/// level imports alone (no per-project import resolution). Cheap —
/// no network, no clones — because the per-project site short-
/// circuits before any `ImportSource` call.
fn manifest_repo_only_view(workspace: &Path, config: &Configuration) -> Result<Manifest, String> {
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
    let manifest_repo_root = workspace.join(&manifest_path);
    let full = manifest_repo_root.join(&manifest_file);
    Manifest::from_path_with(
        &full,
        Some(&manifest_repo_root),
        None,
        ImportPolicy::SKIP_PROJECTS,
    )
    .map_err(|e| format!("manifest {}: {e}", full.display()))
}

fn load_manifest(
    workspace: &Path,
    config: &Configuration,
    vcs: &dyn Vcs,
    settings: &Settings,
    import_progress: Option<&import_source::ImportProgress>,
) -> Result<Manifest, String> {
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
    let manifest_repo_root = workspace.join(&manifest_path);
    let full = manifest_repo_root.join(&manifest_file);
    let mut source =
        import_source::WorkspaceImportSource::new(workspace, vcs).with_settings(settings);
    if let Some(p) = import_progress {
        source = source.with_progress(p);
    }
    Manifest::from_path_with(
        &full,
        Some(&manifest_repo_root),
        Some(&source),
        ImportPolicy::RESOLVE_ALL,
    )
    .map_err(|e| format!("manifest {}: {e}", full.display()))
}

fn default_jobs() -> usize {
    std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(1)
        .clamp(1, MAX_DEFAULT_JOBS)
}
