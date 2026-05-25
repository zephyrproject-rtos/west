//! `west list` — print one line per project, rendered through a
//! Python-style `--format` template.
//!
//! Filters mirror Python's: default lists only active projects;
//! `-a/--all` includes inactive; `-i/--inactive` lists only inactive.
//! Positional project names bypass the activity filter (same rule as
//! `west update`). `-i` plus positionals is rejected up front.
//!
//! The format string accepts the same `{key:[fill][align][width]}`
//! spec that `str.format()` does, via the `strfmt` crate's
//! [`strfmt::strfmt_map`] entry point. Lazy keys (`{sha}`, `{cloned}`,
//! `{active}`) only run their underlying lookup when the user asked
//! for them — relevant for workspaces with hundreds of projects.

use std::io::{self, Write};
use std::process::ExitCode;

use clap::Args;

use west_core::manifest::Project;
use west_core::vcs;

use super::config::LoadedConfig;
use super::project_format::{self, FormatError, ProjectContext};
use super::select;

const DEFAULT_FORMAT: &str = "{name:12} {path:28} {revision:40} {url}";

#[derive(Args, Debug)]
pub struct ListArgs {
    /// Project names or paths to list. Empty = all (active by default;
    /// see `--all` / `--inactive`).
    #[arg(value_name = "PROJECT")]
    pub projects: Vec<String>,

    /// Include inactive projects.
    #[arg(short = 'a', long, conflicts_with = "inactive")]
    pub all: bool,

    /// List only inactive projects. Cannot be combined with project
    /// names.
    #[arg(short = 'i', long)]
    pub inactive: bool,

    /// Format string. Default: `{name:12} {path:28} {revision:40} {url}`.
    #[arg(short = 'f', long)]
    pub format: Option<String>,

    /// For the synthetic `manifest` project only: render `{path}` /
    /// `{abspath}` / `{posixpath}` using the manifest file's
    /// `self.path` value instead of the workspace's `manifest.path`
    /// config. The two normally agree; they diverge when the user
    /// re-points the workspace at a moved manifest repo.
    ///
    /// The hidden `--manifest-path-from-yaml` alias keeps v1
    /// invocations working — the field exists in TOML and JSON
    /// manifests too, so the format-agnostic name is the canonical
    /// one going forward.
    #[arg(long, alias = "manifest-path-from-yaml")]
    pub manifest_path_from_file: bool,
}

#[derive(Debug, thiserror::Error)]
pub enum ListError {
    #[error("not inside a west workspace (no .west/ found)")]
    NotInWorkspace,
    #[error("{0}")]
    Config(String),
    #[error("{0}")]
    Manifest(String),
    #[error("{0}")]
    Vcs(String),
    #[error("unknown format key: {{{0}}}")]
    UnknownKey(String),
    #[error("format error: {0}")]
    Format(String),
    #[error("project {0:?} is not cloned; cannot resolve {{sha}} (run `west update` first)")]
    UnclonedSha(String),
    #[error("-i cannot be combined with an explicit project list")]
    InactiveWithPositional,
}

impl From<super::workspace::WorkspaceError> for ListError {
    fn from(e: super::workspace::WorkspaceError) -> Self {
        match e {
            super::workspace::WorkspaceError::NotInWorkspace => ListError::NotInWorkspace,
            super::workspace::WorkspaceError::Config(s) => ListError::Config(s),
            super::workspace::WorkspaceError::Manifest(s) => ListError::Manifest(s),
        }
    }
}

impl From<FormatError> for ListError {
    fn from(e: FormatError) -> Self {
        match e {
            FormatError::UnknownKey(k) => ListError::UnknownKey(k),
            FormatError::Format(s) => ListError::Format(s),
            FormatError::UnclonedSha(n) => ListError::UnclonedSha(n),
            FormatError::Vcs(s) => ListError::Vcs(s),
        }
    }
}

pub fn run(args: ListArgs, loaded: &mut LoadedConfig) -> ExitCode {
    match run_inner(args, loaded) {
        Ok(false) => ExitCode::SUCCESS,
        // Some imports were skipped because their projects aren't
        // cloned. The warning has already been printed; signal
        // partial success with a non-zero exit so scripts notice.
        Ok(true) => ExitCode::FAILURE,
        Err(e @ ListError::InactiveWithPositional) => {
            eprintln!("west: {e}");
            ExitCode::from(2)
        }
        Err(e) => {
            eprintln!("west: {e}");
            ExitCode::FAILURE
        }
    }
}

/// `Ok(true)` means rendering succeeded but at least one per-project
/// import was skipped because the importing project wasn't cloned —
/// the listing is incomplete and the caller should signal that with a
/// non-zero exit code.
fn run_inner(args: ListArgs, loaded: &mut LoadedConfig) -> Result<bool, ListError> {
    if args.inactive && !args.projects.is_empty() {
        return Err(ListError::InactiveWithPositional);
    }

    let workspace = super::workspace::resolve_workspace_dir()?;
    let vcs = vcs::from_config(&loaded.config).map_err(|e| ListError::Vcs(e.to_string()))?;
    let source = super::workspace::ReadOnlyImportSource::new(workspace.as_path(), vcs.as_ref());
    let loaded_manifest = super::workspace::load_manifest(&workspace, &loaded.config, &source)?;
    let manifest = &loaded_manifest.manifest;

    // The "manifest project" — a synthetic entry representing the
    // manifest repo itself. Python's `Manifest.projects` exposes one of
    // these at index 0; we inline it here in `west list` so the data
    // layer stays free of synthetic records (commands that shouldn't
    // operate on it, like `west update`, don't need to filter).
    // Default: render the synthetic's `{path}` against the
    // workspace's `manifest.path` config. `--manifest-path-from-file`
    // overrides to the manifest's advisory `self.path`.
    let synthetic_path = if args.manifest_path_from_file {
        manifest.self_.path.clone()
    } else {
        super::workspace::manifest_path_from_config(&loaded.config)?
    };
    let synthetic = select::synthetic_manifest_project(manifest, synthetic_path);

    let projects: Vec<&Project> = if args.projects.is_empty() {
        let mut acc: Vec<&Project> = Vec::new();
        // The manifest project is always considered active; include it
        // unless `--inactive` (which asks for *only* inactive projects).
        if args.all || !args.inactive {
            acc.push(&synthetic);
        }
        acc.extend(manifest.projects.iter().filter(|p| {
            if args.all {
                true
            } else if args.inactive {
                !loaded_manifest.is_active(p, &[])
            } else {
                loaded_manifest.is_active(p, &[])
            }
        }));
        acc
    } else {
        // Normalize positionals first so absolute paths and `./..`
        // forms collapse to the same shape as the partition's
        // comparators (`"manifest"` / synthetic's path) and as
        // `Manifest::resolve_projects` matches against.
        let synthetic_path_str = synthetic.path.to_string_lossy().into_owned();
        let normalized: Vec<String> = args
            .projects
            .iter()
            .map(|s| super::workspace::normalize_project_selector(s, &workspace))
            .collect();
        let (synthetic_hits, leftover): (Vec<_>, Vec<_>) = normalized
            .iter()
            .partition(|s| s.as_str() == select::SYNTHETIC_NAME || s.as_str() == synthetic_path_str);

        let mut acc: Vec<&Project> = Vec::new();
        if !synthetic_hits.is_empty() {
            acc.push(&synthetic);
        }
        if !leftover.is_empty() {
            let leftover: Vec<&str> = leftover.iter().map(|s| s.as_str()).collect();
            acc.extend(
                select::select_projects(&loaded_manifest, &leftover, &[])
                    .map_err(|e| ListError::Manifest(e.to_string()))?,
            );
        }
        acc
    };

    let template = args.format.as_deref().unwrap_or(DEFAULT_FORMAT);

    let stdout = io::stdout();
    let mut lock = stdout.lock();
    for project in projects {
        let ctx = ProjectContext {
            project,
            loaded: &loaded_manifest,
            workspace: workspace.as_path(),
            vcs: vcs.as_ref(),
        };
        let line = project_format::render(template, &ctx)?;
        // A broken pipe (head, |less q) is the natural way for users to
        // truncate output; treat as success and return.
        if let Err(e) = writeln!(lock, "{line}") {
            if e.kind() == io::ErrorKind::BrokenPipe {
                return Ok(false);
            }
            return Err(ListError::Format(e.to_string()));
        }
    }

    // Surface any imports that were skipped because their owning project
    // wasn't cloned. The listing is real but incomplete; warn and
    // signal partial success via a non-zero exit (returned by `run`).
    let mut skipped = source.skipped();
    skipped.sort();
    skipped.dedup();
    if !skipped.is_empty() {
        let plural = if skipped.len() == 1 { "" } else { "s" };
        eprintln!(
            "west: warning: skipped import{plural} from {} uncloned project{plural}: {}",
            skipped.len(),
            skipped.join(", "),
        );
        eprintln!("    run `west update` first to enumerate imported projects");
        return Ok(true);
    }
    Ok(false)
}

// Per-project lookup + format rendering live in
// `commands/project_format.rs`; `list` consumes them and converts
// `FormatError` to `ListError` via the From impl above.

