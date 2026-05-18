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
use west_core::manifest::{GroupFilterEntry, Manifest, Project, Submodules};
use west_core::vcs::{
    self, CheckoutTarget, CloneSpec, CommitSummary, FetchSpec, Output, SubmoduleScope, Vcs,
};

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

    // The vcs has to exist before we load the manifest: per-project
    // imports need it to fetch + read each importing project's manifest
    // file at its manifest revision.
    let vcs = match vcs::from_config(&loaded.config) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("west: {e}");
            return ExitCode::FAILURE;
        }
    };

    // Decide whether to drive an indicatif progress bar for any
    // import-resolution clones the manifest load triggers. Same rule
    // the main worker pool uses below: TTY + non-raw → indicatif;
    // otherwise (raw or non-TTY) git stdio attaches natively to the
    // parent's terminal. Reading `output.raw` direct here keeps us
    // from having to hoist `Settings::from_config`, which validates
    // a bunch of other update-specific knobs we don't need yet.
    let raw_for_import = loaded
        .config
        .get_bool("output.raw")
        .ok()
        .flatten()
        .unwrap_or(false);
    let import_progress = (!raw_for_import && io::stderr().is_terminal())
        .then(import_source::ImportProgress::new);

    let manifest = match load_manifest(
        &workspace,
        &loaded.config,
        vcs.as_ref(),
        import_progress.as_ref(),
    ) {
        Ok(m) => m,
        Err(e) => {
            eprintln!("west: {e}");
            return ExitCode::FAILURE;
        }
    };
    // Drop the import-progress MultiProgress before the main worker
    // pool creates its own — keeps the two from fighting over the
    // terminal.
    drop(import_progress);

    let cli_group_filter = match read_cli_group_filter(&loaded.config) {
        Ok(f) => f,
        Err(e) => {
            eprintln!("west: {e}");
            return ExitCode::from(2);
        }
    };
    // Workspace-permanent `manifest.group-filter` (e.g. `+optional`)
    // applies on top of the manifest's own `group-filter:` regardless
    // of which command is running. Compose with `update`'s own
    // CLI/config filter before handing to selection.
    let mut effective_filter = match super::select::read_manifest_group_filter(&loaded.config) {
        Ok(f) => f,
        Err(e) => {
            eprintln!("west: {e}");
            return ExitCode::from(2);
        }
    };
    effective_filter.extend(cli_group_filter);

    let projects =
        match super::select::select_projects(&manifest, &args.projects, &effective_filter) {
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
            eprintln!("west: failed to start worker pool: {e}");
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
        eprintln!("west: {}", summary.render());
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
        eprintln!("=== updating {} ({})", project.name, project.path.display());
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
    // 1. Ensure cloned. We deliberately don't pass `revision` here:
    //    git clone --branch only accepts branches/tags, but manifests
    //    routinely pin projects at bare commit SHAs (zephyr does this
    //    for every project). Clone the remote's default branch and let
    //    the subsequent fetch + detached checkout land us on the right
    //    commit.
    //
    // If a cache flag matched, the clone source is the cache directory
    // (a local path) instead of the project URL. The auto-cache branch
    // populates / refreshes the cache first so the workspace clone
    // never hits the network. After a cache-driven clone the recorded
    // origin URL is flipped back to the project URL — subsequent fetches
    // go to the real remote.
    let cache_source = cache::resolve_cache_source(project, settings);
    // Keep the auto-cache fresh on every run — not just when we're
    // about to clone the workspace from it. A workspace might already
    // be cloned, but the cache should still advance to the latest
    // upstream so subsequent fresh clones (or other workspaces sharing
    // the same auto-cache root) hit a current mirror.
    if let Some(cache::CacheSource::Auto(path)) = &cache_source {
        ensure_auto_cache(vcs, project, path, out)?;
    }
    let already_cloned = repo.exists() && vcs.is_repo(repo).unwrap_or(false);
    if !already_cloned {
        if let Some(parent) = repo.parent() {
            std::fs::create_dir_all(parent).map_err(|source| UpdateError::CreateParent {
                path: parent.to_path_buf(),
                source,
            })?;
        }
        let clone_url = match cache_source.as_ref() {
            Some(src) => src
                .path()
                .to_str()
                .ok_or_else(|| UpdateError::NonUtf8CachePath(src.path().to_path_buf()))?,
            None => &project.url,
        };
        vcs.clone(
            &CloneSpec {
                url: clone_url,
                dest: repo,
                revision: None,
                origin: Some(&project.remote_name),
                mirror: false,
            },
            out,
        )
        .map_err(|source| UpdateError::Clone {
            url: clone_url.to_owned(),
            source,
        })?;
        // Cache-cloned: rewrite the origin URL to the real upstream so
        // the fetch step (and every subsequent `west update`) pulls
        // from the network, not the local cache directory.
        if cache_source.is_some() {
            vcs.set_remote_url(repo, &project.remote_name, &project.url)
                .map_err(UpdateError::SetRemoteUrl)?;
        }
    }

    // 2. Fetch. `Vcs::fetch` returns the sha that the requested revision
    //    now resolves to — `FETCH_HEAD^{commit}` after an active fetch, or
    //    the locally-resolved revision on smart-skip. Don't sniff
    //    `FETCH_HEAD` directly: it persists across runs, so on smart-skip
    //    it's stale from a previous fetch and would point at the wrong
    //    commit.
    let sha = vcs
        .fetch(
            repo,
            &FetchSpec {
                remote: &project.remote_name,
                revision: Some(&project.revision),
            },
            out,
        )
        .map_err(|source| UpdateError::Fetch {
            remote: project.remote_name.clone(),
            source,
        })?;

    // 3. Record manifest-rev.
    let reason = format!("west update: moving to {}", project.revision);
    vcs.set_manifest_rev(repo, &sha, Some(&reason))
        .map_err(UpdateError::SetManifestRev)?;

    // 5. Decide strategy.
    let head_branch = vcs.head_branch(repo).map_err(UpdateError::HeadBranch)?;
    let detach = match (
        settings.keep_descendants,
        settings.rebase,
        head_branch.as_deref(),
    ) {
        (true, _, Some(branch)) => {
            // keep_descendants: keep current branch checked out only if the
            // new sha is already an ancestor of it.
            let is_ancestor = vcs
                .is_ancestor(repo, &sha, branch)
                .map_err(UpdateError::IsAncestor)?;
            if is_ancestor {
                note(
                    out,
                    &format!("keeping branch {branch:?} (manifest-rev is an ancestor)"),
                );
                false
            } else if settings.rebase {
                vcs.rebase(repo, "refs/heads/manifest-rev", out)
                    .map_err(UpdateError::Rebase)?;
                false
            } else {
                true
            }
        }
        (false, true, Some(_)) => {
            // rebase the current branch onto manifest-rev.
            vcs.rebase(repo, "refs/heads/manifest-rev", out)
                .map_err(UpdateError::Rebase)?;
            false
        }
        _ => true,
    };

    if detach {
        vcs.checkout(repo, &CheckoutTarget::Detached(&sha), out)
            .map_err(|source| UpdateError::Checkout {
                sha: sha.clone(),
                source,
            })?;
    }

    // 6. Submodules. If the parent project was cache-cloned and the
    //    cache also contains the submodule sub-tree, pass it as
    //    `--reference` to `git submodule update` so the submodule
    //    init reuses the cached objects too.
    let scope = submodules_scope(&project.submodules);
    if !matches!(scope, ScopeOwned::Skip) {
        run_submodules(vcs, repo, &scope, cache_source.as_ref(), out)?;
    }

    // 7. One-line snapshot of where HEAD landed, for the reporter to
    //    surface in its success line / per-project transcript.
    vcs.commit_summary(repo, "HEAD")
        .map_err(UpdateError::CommitSummary)
}

/// Auto-cache populator: bare-mirror-clone when missing; refresh-fetch
/// when present.
///
/// SHA-like revisions (and tags) are immutable, so a populated cache
/// already has them — we smart-skip in that case to keep the offline
/// path working (cache → workspace clone with no network at all).
/// Branch revisions, on the other hand, move under the user; we always
/// run `git fetch origin` against the mirror so the cache picks up
/// upstream commits between runs. On a `--mirror` clone the configured
/// refspec is `+refs/*:refs/*`, so a single fetch updates everything.
fn ensure_auto_cache(
    vcs: &dyn Vcs,
    project: &Project,
    cache_path: &Path,
    out: &mut Output<'_>,
) -> Result<(), UpdateError> {
    if cache_path.exists() && vcs.is_repo(cache_path).unwrap_or(false) {
        // Smart-skip path: only when the manifest pinned a SHA-like
        // revision and the cache already has it. The fetch we'd
        // otherwise run is `revision: None`, which can't smart-skip.
        if looks_like_sha(&project.revision) && vcs.sha(cache_path, &project.revision).is_ok() {
            return Ok(());
        }
        vcs.fetch(
            cache_path,
            &FetchSpec {
                remote: "origin",
                revision: None,
            },
            out,
        )
        .map_err(|source| UpdateError::CacheRefresh {
            path: cache_path.to_path_buf(),
            source,
        })?;
        return Ok(());
    }
    if let Some(parent) = cache_path.parent() {
        std::fs::create_dir_all(parent).map_err(|source| UpdateError::CreateParent {
            path: parent.to_path_buf(),
            source,
        })?;
    }
    vcs.clone(
        &CloneSpec {
            url: &project.url,
            dest: cache_path,
            revision: None,
            origin: None,
            mirror: true,
        },
        out,
    )
    .map_err(|source| UpdateError::CachePopulate {
        url: project.url.clone(),
        source,
    })
}

/// Best-effort SHA detector: 4–40 hex chars. Mirrors python's
/// `_maybe_sha`. Used by `ensure_auto_cache` to decide whether the
/// revision is immutable (skip refresh if cached) or mutable (always
/// refresh). False positives are harmless: a tag like `v1` doesn't
/// match (`v` isn't hex); only ambiguous all-hex names like `abcd`
/// would, and those are pathological.
fn looks_like_sha(rev: &str) -> bool {
    let len = rev.len();
    (4..=40).contains(&len) && rev.chars().all(|c| c.is_ascii_hexdigit())
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
    cache_source: Option<&cache::CacheSource>,
    out: &mut Output<'_>,
) -> Result<(), UpdateError> {
    match scope {
        ScopeOwned::All => vcs
            // `--reference` is per-call and applies to every submodule
            // git initialises. We don't enumerate submodules ourselves
            // for the All scope (matches python), so cache reference
            // doesn't apply here — pass `None`.
            .update_submodules(repo, &SubmoduleScope::All, None, out)
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
                    reference.as_deref(),
                    out,
                )
                .map_err(UpdateError::Submodules)?;
            }
            Ok(())
        }
    }
}

/// Emit a non-vcs note from the worker. Native mode prints to stderr;
/// Stream mode pushes a `Line` event into the sink so it interleaves
/// with the captured transcript at the right point.
fn note(out: &mut Output<'_>, msg: &str) {
    match out {
        Output::Native => eprintln!("{msg}"),
        Output::Stream(sink) => sink.event(west_core::vcs::ProgressEvent::Line(msg)),
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
    if args.narrow {
        splice_inline(config, "tool.git.fetch.tags", ConfigValue::Bool(false))?;
    }
    if let Some(f) = args.fetch {
        splice_inline(
            config,
            "tool.git.fetch.strategy",
            ConfigValue::String(f.as_config_str().to_owned()),
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

fn load_manifest(
    workspace: &Path,
    config: &Configuration,
    vcs: &dyn Vcs,
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
    let mut source = import_source::WorkspaceImportSource::new(workspace, vcs);
    if let Some(p) = import_progress {
        source = source.with_progress(p);
    }
    Manifest::from_path_with_imports(&full, &manifest_repo_root, &source)
        .map_err(|e| format!("manifest {}: {e}", full.display()))
}

fn default_jobs() -> usize {
    std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(1)
        .clamp(1, MAX_DEFAULT_JOBS)
}
