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
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::Mutex;

use clap::Args;

use west_core::config::Configuration;
use west_core::manifest::{ImportSource, ImportSourceError, Manifest, Project, Submodules};
use west_core::vcs::{self, Vcs};

use super::config::LoadedConfig;
use super::select;

const DEFAULT_FORMAT: &str = "{name:12} {path:28} {revision:40} {url}";
const DEFAULT_MANIFEST_FILE: &str = "west.yml";

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
    #[error("--inactive cannot be combined with project names")]
    InactiveWithPositional,
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

    let workspace = resolve_workspace_dir()?;
    let vcs = vcs::from_config(&loaded.config).map_err(|e| ListError::Vcs(e.to_string()))?;
    let source = ReadOnlyImportSource {
        workspace: workspace.as_path(),
        vcs: vcs.as_ref(),
        skipped: Mutex::new(Vec::new()),
    };
    let manifest = load_manifest(&workspace, &loaded.config, &source)?;
    // `manifest.group-filter` is the workspace-permanent filter that
    // sits on top of the manifest's own `group-filter:`. Every command
    // that gates by activity has to apply it; without this, an inactive
    // project re-enabled by the user via `+optional` (etc.) would still
    // be filtered out.
    let cfg_filter =
        select::read_manifest_group_filter(&loaded.config).map_err(ListError::Config)?;

    // The "manifest project" — a synthetic entry representing the
    // manifest repo itself. Python's `Manifest.projects` exposes one of
    // these at index 0; we inline it here in `west list` so the data
    // layer stays free of synthetic records (commands that shouldn't
    // operate on it, like `west update`, don't need to filter).
    let synthetic = synthetic_manifest_project(&manifest);

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
                !manifest.is_active(p, &cfg_filter)
            } else {
                manifest.is_active(p, &cfg_filter)
            }
        }));
        acc
    } else {
        // Pull positional matches for the synthetic out before falling
        // through to `select_projects` (which only knows about the
        // resolved real projects). `west list manifest` matches by name;
        // `west list <self.path>` matches by path — same as Python.
        let manifest_path_str = manifest.self_.path.to_string_lossy().into_owned();
        let (synthetic_hits, leftover): (Vec<_>, Vec<_>) = args
            .projects
            .iter()
            .partition(|s| s.as_str() == "manifest" || s.as_str() == manifest_path_str);

        let mut acc: Vec<&Project> = Vec::new();
        if !synthetic_hits.is_empty() {
            acc.push(&synthetic);
        }
        if !leftover.is_empty() {
            let leftover: Vec<&str> = leftover.iter().map(|s| s.as_str()).collect();
            acc.extend(
                select::select_projects(&manifest, &leftover, &[])
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
            manifest: &manifest,
            workspace: workspace.as_path(),
            vcs: vcs.as_ref(),
            cfg_filter: &cfg_filter,
        };
        let line = render(template, &ctx)?;
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
    let mut skipped = source
        .skipped
        .into_inner()
        .expect("ReadOnlyImportSource skipped mutex poisoned");
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

// =====================================================================
// Per-project lookup
// =====================================================================

struct ProjectContext<'a> {
    project: &'a Project,
    manifest: &'a Manifest,
    workspace: &'a Path,
    vcs: &'a dyn Vcs,
    cfg_filter: &'a [west_core::manifest::GroupFilterEntry],
}

impl ProjectContext<'_> {
    fn lookup(&self, key: &str) -> Result<String, ListError> {
        match key {
            "name" => Ok(self.project.name.clone()),
            "description" => Ok(self
                .project
                .description
                .clone()
                .unwrap_or_else(|| "None".into())),
            // Empty url/revision → "N/A". Real projects are validated to
            // have a non-empty url and a revision, so this fallback only
            // fires for the synthetic manifest project (matches Python's
            // `project.url or 'N/A'` rendering in `_format_project`).
            "url" => Ok(or_na(&self.project.url)),
            "path" => Ok(self.project.path.to_string_lossy().into_owned()),
            "abspath" => Ok(self
                .workspace
                .join(&self.project.path)
                .to_string_lossy()
                .into_owned()),
            "posixpath" => Ok(self
                .workspace
                .join(&self.project.path)
                .to_string_lossy()
                .replace('\\', "/")),
            "revision" => Ok(or_na(&self.project.revision)),
            "remote" => Ok(self.project.remote_name.clone()),
            "clone_depth" => Ok(self
                .project
                .clone_depth
                .map(|n| n.to_string())
                .unwrap_or_else(|| "None".into())),
            "groups" => Ok(self.project.groups.join(",")),
            "active" => Ok(if self.manifest.is_active(self.project, self.cfg_filter) {
                "active".into()
            } else {
                "inactive".into()
            }),
            "cloned" => Ok(if self.is_cloned() {
                "cloned".into()
            } else {
                "not-cloned".into()
            }),
            "sha" => self.compute_sha(),
            other => Err(ListError::UnknownKey(other.to_owned())),
        }
    }

    fn repo_path(&self) -> PathBuf {
        self.workspace.join(&self.project.path)
    }

    fn is_cloned(&self) -> bool {
        let repo = self.repo_path();
        repo.exists() && self.vcs.is_repo(&repo).unwrap_or(false)
    }

    fn compute_sha(&self) -> Result<String, ListError> {
        if !self.is_cloned() {
            return Err(ListError::UnclonedSha(self.project.name.clone()));
        }
        self.vcs
            .sha(&self.repo_path(), "HEAD")
            .map_err(|e| ListError::Vcs(e.to_string()))
    }
}

/// Build the synthetic project record for the manifest repo itself.
/// Mirrors Python's `ManifestProject` (index 0 in `Manifest.projects`):
/// name `"manifest"` (a reserved name no real project can use),
/// revision `"HEAD"`, no url. Path is the manifest repo's `self.path`.
fn synthetic_manifest_project(manifest: &Manifest) -> Project {
    Project {
        name: "manifest".into(),
        url: String::new(),
        revision: "HEAD".into(),
        path: manifest.self_.path.clone(),
        description: None,
        groups: Vec::new(),
        clone_depth: None,
        west_commands: manifest.self_.west_commands.clone(),
        remote_name: String::new(),
        submodules: Submodules::None,
    }
}

fn or_na(s: &str) -> String {
    if s.is_empty() {
        "N/A".into()
    } else {
        s.to_owned()
    }
}

// =====================================================================
// Rendering via `strfmt`
// =====================================================================

fn render(template: &str, ctx: &ProjectContext<'_>) -> Result<String, ListError> {
    strfmt::strfmt_map(template, |mut fmt: strfmt::Formatter<'_, '_>| {
        // Errors flow back through `FmtError::KeyError(String)`; we
        // recover the typed `ListError` after `strfmt_map` returns.
        match ctx.lookup(fmt.key) {
            Ok(value) => fmt.str(&value),
            Err(e) => Err(strfmt::FmtError::KeyError(e.to_string())),
        }
    })
    .map_err(|e| match e {
        strfmt::FmtError::KeyError(msg) => parse_back_listerror(&msg),
        strfmt::FmtError::TypeError(msg) | strfmt::FmtError::Invalid(msg) => ListError::Format(msg),
    })
}

/// Best-effort: when `strfmt` surfaces a `KeyError` that our lookup
/// produced, the message starts with the human-readable form of one of
/// our `ListError` variants. Keep the original text rather than re-typing
/// — losing the typed structure here is OK because run_inner is the only
/// caller and it just prints the message.
fn parse_back_listerror(msg: &str) -> ListError {
    if msg.starts_with("unknown format key:") {
        // Pull the `{key}` substring back out so the diagnostic stays
        // concise.
        if let Some(start) = msg.find('{')
            && let Some(end) = msg[start + 1..].find('}')
        {
            return ListError::UnknownKey(msg[start + 1..start + 1 + end].to_owned());
        }
    }
    if msg.starts_with("project ") && msg.contains("is not cloned") {
        // Synthesise a fresh `UnclonedSha` so callers can recognise it
        // by variant. The name is between the first pair of `"`s.
        let parts: Vec<&str> = msg.split('"').collect();
        if parts.len() >= 2 {
            return ListError::UnclonedSha(parts[1].to_owned());
        }
    }
    ListError::Format(msg.to_owned())
}

// =====================================================================
// Workspace + manifest loading
// =====================================================================

fn resolve_workspace_dir() -> Result<PathBuf, ListError> {
    let cwd = std::env::current_dir().map_err(|e| ListError::Config(e.to_string()))?;
    west_core::topdir::topdir(&cwd).map_err(|_| ListError::NotInWorkspace)
}

fn load_manifest(
    workspace: &Path,
    config: &Configuration,
    source: &ReadOnlyImportSource<'_>,
) -> Result<Manifest, ListError> {
    let manifest_path: PathBuf = config
        .get_str("manifest.path")
        .map_err(|e| ListError::Config(e.to_string()))?
        .map(PathBuf::from)
        .ok_or_else(|| {
            ListError::Config("manifest.path is not set in workspace config".to_owned())
        })?;
    let manifest_file: PathBuf = config
        .get_str("manifest.file")
        .map_err(|e| ListError::Config(e.to_string()))?
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(DEFAULT_MANIFEST_FILE));
    let manifest_repo_root = workspace.join(&manifest_path);
    let full = manifest_repo_root.join(&manifest_file);
    // Read-only resolution: filesystem imports (self/top-level) work
    // naturally; per-project imports only resolve for projects that
    // are *already* cloned, so listing never triggers a fetch. An
    // uncloned project's import is recorded in `source.skipped` for
    // an end-of-run warning.
    Manifest::from_path_with_imports(&full, &manifest_repo_root, source)
        .map_err(|e| ListError::Manifest(format!("manifest {}: {e}", full.display())))
}

/// `ImportSource` that reads project manifests off disk only — never
/// fetches. For a project that isn't cloned yet, returns `Ok(None)`
/// (so the resolver continues with a partial project list) and pushes
/// the project name onto `skipped` so the caller can warn afterwards.
struct ReadOnlyImportSource<'a> {
    workspace: &'a Path,
    vcs: &'a dyn Vcs,
    skipped: Mutex<Vec<String>>,
}

impl ImportSource for ReadOnlyImportSource<'_> {
    fn project_manifest(
        &self,
        project: &Project,
        relative_file: &str,
    ) -> Result<Option<String>, ImportSourceError> {
        let repo = self.workspace.join(&project.path);
        if !repo.exists() || !self.vcs.is_repo(&repo).unwrap_or(false) {
            self.skipped
                .lock()
                .expect("ReadOnlyImportSource skipped mutex poisoned")
                .push(project.name.clone());
            return Ok(None);
        }
        let path = repo.join(relative_file);
        match std::fs::read_to_string(&path) {
            Ok(body) => Ok(Some(body)),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(ImportSourceError::msg(format!(
                "read {}: {e}",
                path.display()
            ))),
        }
    }
}
