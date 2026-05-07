//! Manifest data layer (parse-only, v1).
//!
//! Parses west manifests (YAML or TOML) into typed Rust data and validates
//! them. **No git, no `import:` resolution, no workspace integration.** Those
//! land in follow-up modules; this module stays metadata-only so it can be
//! reused by code that doesn't need (or want) git plumbing.
//!
//! # Format support
//!
//! - YAML via [`serde-saphyr`](https://crates.io/crates/serde-saphyr) — the
//!   maintained successor to `serde_yaml`. Anchors, aliases, and merge keys
//!   are supported.
//! - TOML via `toml_edit::de::from_str`.
//! - JSON via `serde_json::from_str`.
//!
//! # Validation
//!
//! Per-field rules are declared with [`garde::Validate`] derives on private
//! schema types. Cross-field rules (project-name uniqueness, remote
//! resolution, etc.) live in a post-parse [`resolve`] pass. Validation is
//! format-agnostic: the same `validate()` + `resolve()` flow runs whatever
//! parser produced the schema.

use std::collections::{HashMap, HashSet};
use std::error::Error;
use std::ffi::OsStr;
use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};

use garde::Validate;
use serde::Deserialize;

// =====================================================================
// Public types
// =====================================================================

#[derive(Debug, Clone, PartialEq)]
pub struct Manifest {
    pub version: Option<String>,
    pub self_: ManifestRepo,
    pub projects: Vec<Project>,
    pub group_filter: Vec<GroupFilterEntry>,
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct ManifestRepo {
    /// Relative path of the manifest repo within the workspace. Default `"manifest"`.
    pub path: PathBuf,
    /// Relative paths to west-commands YAML files inside the manifest repo.
    pub west_commands: Vec<PathBuf>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Project {
    pub name: String,
    /// Resolved fetch URL.
    pub url: String,
    /// Resolved revision (default: `"master"`).
    pub revision: String,
    /// Relative path within the workspace (default: project `name`).
    pub path: PathBuf,
    pub description: Option<String>,
    pub groups: Vec<String>,
    pub clone_depth: Option<u32>,
    pub west_commands: Vec<PathBuf>,
    /// Git remote name to set up when cloning. Resolved from `project.remote`,
    /// `defaults.remote`, or `"origin"` as a final fallback.
    pub remote_name: String,
    pub submodules: Submodules,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Submodules {
    /// Update all submodules recursively. Equivalent to `submodules: true`.
    All,
    /// No submodules. Default; also `submodules: false`.
    None,
    /// Update only the listed submodules.
    Specific(Vec<Submodule>),
}

#[derive(Debug, Clone, PartialEq)]
pub struct Submodule {
    pub path: PathBuf,
    pub name: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct GroupFilterEntry {
    pub group: String,
    /// `true` for `-foo`, `false` for `+foo`.
    pub disabled: bool,
}

/// Where an `import:` directive lives — used in error diagnostics.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ImportSite {
    /// `manifest.import:` at the manifest root.
    TopLevel,
    /// `manifest.self.import:` on the manifest repo's own self section.
    SelfRepo,
    /// `import:` on a project entry.
    Project,
}

impl fmt::Display for ImportSite {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ImportSite::TopLevel => f.write_str("top-level"),
            ImportSite::SelfRepo => f.write_str("self"),
            ImportSite::Project => f.write_str("project"),
        }
    }
}

/// Bridge from the data layer to the workspace's vcs, called by the
/// import resolver for every project that itself has an `import:`
/// directive. Implementations are expected to ensure `project` is at its
/// manifest revision (clone + fetch + checkout if necessary) and return
/// the requested file's body.
///
/// Returning `Ok(None)` means "the file isn't there"; the import is
/// silently skipped (Python parity — one missing import doesn't break
/// resolution). Returning `Err` means "the source operation itself
/// failed"; the resolver `eprintln!`s a warning and continues with the
/// rest of the projects, surfacing the failure once at the end via
/// [`ManifestError::ImportSourceFailed`] only if the resolver aborts.
pub trait ImportSource {
    fn project_manifest(
        &self,
        project: &Project,
        relative_file: &str,
    ) -> Result<Option<String>, ImportSourceError>;
}

/// Opaque error type returned by [`ImportSource`] implementations. The
/// `String` is rendered into [`ManifestError::ImportSourceFailed`]'s
/// `detail` field.
#[derive(Debug)]
pub struct ImportSourceError(pub String);

impl fmt::Display for ImportSourceError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl Error for ImportSourceError {}

/// Hard cap on import nesting depth — a chain longer than this returns
/// [`ManifestError::ImportTooDeep`] rather than blowing the stack.
pub const MAX_IMPORT_DEPTH: usize = 32;

// =====================================================================
// Errors
// =====================================================================

#[derive(Debug)]
pub enum ManifestError {
    Yaml(serde_saphyr::Error),
    Toml(toml_edit::de::Error),
    Json(serde_json::Error),
    UnsupportedFormat(String),
    Validation(String),
    UnknownRemote {
        project: String,
        remote: String,
    },
    DuplicateProjectName(String),
    DuplicateProjectPath(String),
    ProjectNamedManifest,
    NoUrl(String),
    UrlAndRemote {
        project: String,
    },
    UrlAndRepoPath {
        project: String,
    },
    InvalidGroup {
        project: String,
        group: String,
    },
    InvalidGroupFilter {
        source: String,
        item: String,
        reason: String,
    },
    AbsoluteProjectPath {
        project: String,
        path: String,
    },
    /// Retired: emitted by the strict policy on legacy callers, but kept
    /// in the enum so external `match` arms don't break. New code should
    /// use [`Manifest::from_path_with_imports`] for resolution or
    /// [`Manifest::from_path_lenient`] to skip imports.
    ImportNotSupported {
        context: String,
    },
    /// An import directive (self/top-level/per-project) cycles back to a
    /// file or project that's already on the resolution stack.
    ImportLoop {
        kind: ImportSite,
        target: String,
    },
    /// Nested imports exceeded [`MAX_IMPORT_DEPTH`].
    ImportTooDeep {
        limit: usize,
    },
    /// An [`ImportSource`] callback failed for a per-project import. The
    /// resolver promotes a non-skipped error from the source into this so
    /// callers can distinguish "the source itself failed" from "the
    /// imported file isn't there."
    ImportSourceFailed {
        project: String,
        detail: String,
    },
    /// `resolve_projects` was given a selector that matches no project (by
    /// name or by path).
    UnknownProject(String),
    Io {
        path: PathBuf,
        source: std::io::Error,
    },
}

impl fmt::Display for ManifestError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ManifestError::Yaml(e) => write!(f, "YAML parse error: {e}"),
            ManifestError::Toml(e) => write!(f, "TOML parse error: {e}"),
            ManifestError::Json(e) => write!(f, "JSON parse error: {e}"),
            ManifestError::UnsupportedFormat(ext) => write!(
                f,
                "unsupported manifest format: {ext:?} (expected .yaml/.yml/.toml/.json)"
            ),
            ManifestError::Validation(msg) => write!(f, "validation failed: {msg}"),
            ManifestError::UnknownRemote { project, remote } => {
                write!(f, "project {project:?}: remote {remote:?} is not defined")
            }
            ManifestError::DuplicateProjectName(name) => {
                write!(f, "duplicate project name: {name:?}")
            }
            ManifestError::DuplicateProjectPath(path) => {
                write!(f, "duplicate project path: {path:?}")
            }
            ManifestError::ProjectNamedManifest => {
                write!(f, "no project may be named \"manifest\" (reserved)")
            }
            ManifestError::NoUrl(name) => write!(
                f,
                "project {name:?}: no remote or url and no default remote is set"
            ),
            ManifestError::UrlAndRemote { project } => {
                write!(
                    f,
                    "project {project:?}: cannot specify both `url` and `remote`"
                )
            }
            ManifestError::UrlAndRepoPath { project } => write!(
                f,
                "project {project:?}: cannot specify both `url` and `repo-path`"
            ),
            ManifestError::InvalidGroup { project, group } => write!(
                f,
                "project {project:?}: invalid group name {group:?} (must not be empty, contain whitespace/comma/colon, or start with `+`/`-`)"
            ),
            ManifestError::InvalidGroupFilter {
                source,
                item,
                reason,
            } => {
                write!(f, "{source} group filter: invalid item {item:?}: {reason}")
            }
            ManifestError::AbsoluteProjectPath { project, path } => write!(
                f,
                "project {project:?} has absolute path {path:?}; must be relative to the workspace"
            ),
            ManifestError::ImportNotSupported { context } => {
                write!(f, "manifest imports are not supported (found in {context})")
            }
            ManifestError::ImportLoop { kind, target } => {
                write!(f, "{kind} import cycle detected: {target:?}")
            }
            ManifestError::ImportTooDeep { limit } => {
                write!(f, "manifest imports nested too deeply (limit: {limit})")
            }
            ManifestError::ImportSourceFailed { project, detail } => {
                write!(f, "import source failed for project {project:?}: {detail}")
            }
            ManifestError::UnknownProject(name) => {
                write!(f, "unknown project name or path: {name:?}")
            }
            ManifestError::Io { path, source } => {
                write!(f, "io error on {}: {source}", path.display())
            }
        }
    }
}

impl Error for ManifestError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            ManifestError::Yaml(e) => Some(e),
            ManifestError::Toml(e) => Some(e),
            ManifestError::Json(e) => Some(e),
            ManifestError::Io { source, .. } => Some(source),
            _ => None,
        }
    }
}

// =====================================================================
// Schema types (private)
// =====================================================================

#[derive(Debug, Clone, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
struct ManifestFile {
    #[garde(dive)]
    manifest: ManifestSection,
}

#[derive(Debug, Clone, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
struct ManifestSection {
    #[garde(skip)]
    #[serde(default)]
    version: Option<String>,
    #[garde(dive)]
    #[serde(default)]
    remotes: Vec<RemoteSchema>,
    #[garde(dive)]
    #[serde(default)]
    defaults: Option<DefaultsSchema>,
    #[garde(dive)]
    #[serde(rename = "self", default)]
    self_: Option<SelfSchema>,
    #[garde(skip)]
    #[serde(rename = "group-filter", default)]
    group_filter: Vec<String>,
    #[garde(dive)]
    #[serde(default)]
    projects: Vec<ProjectSchema>,
    #[garde(skip)]
    #[serde(rename = "import", default)]
    import: Option<ImportSchema>,
}

#[derive(Debug, Clone, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
struct RemoteSchema {
    #[garde(length(min = 1))]
    name: String,
    #[garde(length(min = 1))]
    #[serde(rename = "url-base")]
    url_base: String,
}

#[derive(Debug, Clone, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
struct DefaultsSchema {
    #[garde(skip)]
    #[serde(default)]
    remote: Option<String>,
    #[garde(skip)]
    #[serde(default)]
    revision: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
struct SelfSchema {
    #[garde(skip)]
    #[serde(default)]
    path: Option<String>,
    #[garde(skip)]
    #[serde(rename = "west-commands", default)]
    west_commands: Option<OneOrMany<String>>,
    #[garde(skip)]
    #[serde(rename = "import", default)]
    import: Option<ImportSchema>,
}

#[derive(Debug, Clone, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
struct ProjectSchema {
    #[garde(length(min = 1), pattern(r"^[^/\\]+$"))]
    name: String,
    #[garde(skip)]
    #[serde(default)]
    url: Option<String>,
    #[garde(skip)]
    #[serde(default)]
    remote: Option<String>,
    #[garde(skip)]
    #[serde(default)]
    revision: Option<String>,
    #[garde(skip)]
    #[serde(default)]
    path: Option<String>,
    #[garde(skip)]
    #[serde(default)]
    description: Option<String>,
    #[garde(skip)]
    #[serde(default)]
    groups: Vec<String>,
    #[garde(skip)]
    #[serde(rename = "clone-depth", default)]
    clone_depth: Option<u32>,
    #[garde(skip)]
    #[serde(rename = "west-commands", default)]
    west_commands: Option<OneOrMany<String>>,
    #[garde(skip)]
    #[serde(rename = "remote-name", default)]
    remote_name: Option<String>,
    #[garde(skip)]
    #[serde(rename = "repo-path", default)]
    repo_path: Option<String>,
    #[garde(skip)]
    #[serde(default)]
    submodules: Option<SubmodulesSchema>,
    #[garde(skip)]
    #[serde(rename = "import", default)]
    import: Option<ImportSchema>,
    /// Parsed and held only to avoid `deny_unknown_fields` rejecting it. Typed
    /// access deferred until a real consumer exists.
    #[allow(dead_code)]
    #[garde(skip)]
    #[serde(default)]
    userdata: Option<serde::de::IgnoredAny>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(untagged)]
enum SubmodulesSchema {
    All(bool),
    Specific(Vec<SubmoduleSchema>),
}

/// `import:` directive on the manifest, `self`, or any project. Four
/// permitted shapes; see [`ImportMap`] for dict-form fields.
///
/// Variant order matters for serde-untagged: more specific shapes first,
/// `Map` last — otherwise serde-saphyr's leniency for YAML sequences
/// vs mappings can mis-deserialize a sequence as a map.
#[derive(Debug, Clone, Deserialize)]
#[serde(untagged)]
enum ImportSchema {
    /// `import: true` (= west.yml). `import: false` is a no-op.
    Bool(bool),
    /// `import: file.yml`.
    Str(String),
    /// `import: [...]` — a list of any of the above forms.
    List(Vec<ImportSchema>),
    /// `import: { file: ..., name-allowlist: [...] , ... }`.
    Map(ImportMap),
}

/// Dict form of [`ImportSchema`]. All list fields also accept a single
/// string (Python parity); `OneOrMany` handles the deserialization.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "kebab-case")]
struct ImportMap {
    #[serde(default)]
    file: Option<String>,
    #[serde(default)]
    name_allowlist: Option<OneOrMany<String>>,
    #[serde(default)]
    path_allowlist: Option<OneOrMany<String>>,
    #[serde(default)]
    name_blocklist: Option<OneOrMany<String>>,
    #[serde(default)]
    path_blocklist: Option<OneOrMany<String>>,
    #[serde(default)]
    path_prefix: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
struct SubmoduleSchema {
    #[garde(length(min = 1))]
    path: String,
    #[garde(skip)]
    #[serde(default)]
    name: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(untagged)]
enum OneOrMany<T> {
    One(T),
    Many(Vec<T>),
}

// =====================================================================
// Lenient probe — used by `peek_self_path`. Tolerates `import:` keys,
// unknown fields, and partial/incomplete manifests because it doesn't
// derive `Validate` or use `#[serde(deny_unknown_fields)]`.
// =====================================================================

#[derive(Debug, Deserialize)]
struct ManifestProbeFile {
    #[serde(default)]
    manifest: Option<ManifestProbeSection>,
}

#[derive(Debug, Deserialize)]
struct ManifestProbeSection {
    #[serde(default, rename = "self")]
    self_: Option<SelfProbe>,
}

#[derive(Debug, Deserialize)]
struct SelfProbe {
    #[serde(default)]
    path: Option<String>,
}

impl ManifestProbeFile {
    fn into_self_path(self) -> Option<PathBuf> {
        self.manifest
            .and_then(|m| m.self_)
            .and_then(|s| s.path)
            .map(PathBuf::from)
    }
}

impl<T> OneOrMany<T> {
    fn into_vec(self) -> Vec<T> {
        match self {
            OneOrMany::One(t) => vec![t],
            OneOrMany::Many(v) => v,
        }
    }
}

impl<T: Clone> OneOrMany<T> {
    fn to_vec(&self) -> Vec<T> {
        match self {
            OneOrMany::One(t) => vec![t.clone()],
            OneOrMany::Many(v) => v.clone(),
        }
    }
}

// =====================================================================
// Import resolution
// =====================================================================
//
// Mirrors the Python algorithm in src/west/manifest.py around `_load_self`
// / `_load_projects` / `_compose_imap_filters`. Differences from Python
// recorded in trade-offs in the plan:
//
// - Cycle/depth guards are explicit (visited-sets + MAX_IMPORT_DEPTH)
//   rather than relying on stack overflow.
// - Resolution order: each manifest file processes its own
//   directly-defined projects first, then walks self/top-level imports,
//   then per-project imports for projects that have them. First-wins
//   means parent-defined projects beat imported ones with the same name.
// - 0.10+ group-filter semantics only.

const MANIFEST_DEFAULT_FILE: &str = "west.yml";

/// Per-import filter (allowlist/blocklist of project names + paths). The
/// resolver carries a composed filter through the recursion — parent and
/// child rules combine according to Python's `_compose_imap_filters`.
#[derive(Debug, Clone, Default)]
struct ImportFilter {
    name_allowlist: Vec<String>,
    path_allowlist: Vec<String>,
    name_blocklist: Vec<String>,
    path_blocklist: Vec<String>,
}

impl ImportFilter {
    fn from_map(m: &ImportMap) -> Self {
        Self {
            name_allowlist: m
                .name_allowlist
                .as_ref()
                .map(OneOrMany::to_vec)
                .unwrap_or_default(),
            path_allowlist: m
                .path_allowlist
                .as_ref()
                .map(OneOrMany::to_vec)
                .unwrap_or_default(),
            name_blocklist: m
                .name_blocklist
                .as_ref()
                .map(OneOrMany::to_vec)
                .unwrap_or_default(),
            path_blocklist: m
                .path_blocklist
                .as_ref()
                .map(OneOrMany::to_vec)
                .unwrap_or_default(),
        }
    }

    /// Compose `parent` and `child` so a project must pass both gates to
    /// make it through. Allowlists narrow (intersection-on-list,
    /// preserving "empty = no constraint"); blocklists union.
    fn compose(parent: &Self, child: &Self) -> Self {
        Self {
            name_allowlist: combine_allowlists(&parent.name_allowlist, &child.name_allowlist),
            path_allowlist: combine_allowlists(&parent.path_allowlist, &child.path_allowlist),
            name_blocklist: union(&parent.name_blocklist, &child.name_blocklist),
            path_blocklist: union(&parent.path_blocklist, &child.path_blocklist),
        }
    }

    fn allows(&self, project: &Project) -> bool {
        let path_str = project.path.to_string_lossy();
        let name_blocked = self.name_blocklist.iter().any(|n| n == &project.name);
        let path_blocked = self
            .path_blocklist
            .iter()
            .any(|pat| matches_glob(pat, &path_str));
        let name_listed = self.name_allowlist.iter().any(|n| n == &project.name);
        let path_listed = self
            .path_allowlist
            .iter()
            .any(|pat| matches_glob(pat, &path_str));
        let any_allowlist = !self.name_allowlist.is_empty() || !self.path_allowlist.is_empty();
        let in_allowlist = name_listed || path_listed;

        if name_blocked || path_blocked {
            // Blocks override; only explicit allowlist membership rescues.
            any_allowlist && in_allowlist
        } else if any_allowlist {
            in_allowlist
        } else {
            true
        }
    }
}

fn combine_allowlists(parent: &[String], child: &[String]) -> Vec<String> {
    // Python's _combine_allowlists semantics: empty = no constraint, so
    // an empty side returns the other; both empty stays empty; both
    // populated keeps everything from either (the predicate is "in the
    // union").
    if parent.is_empty() {
        child.to_vec()
    } else if child.is_empty() {
        parent.to_vec()
    } else {
        let mut out = parent.to_vec();
        for s in child {
            if !out.contains(s) {
                out.push(s.clone());
            }
        }
        out
    }
}

fn union(a: &[String], b: &[String]) -> Vec<String> {
    let mut out = a.to_vec();
    for s in b {
        if !out.contains(s) {
            out.push(s.clone());
        }
    }
    out
}

/// Tiny shell-glob matcher: handles `*` (any run of non-/) and bare
/// literals. Anything more elaborate is unlikely to come up in
/// allowlist/blocklist patterns and isn't worth a regex/glob crate.
fn matches_glob(pattern: &str, value: &str) -> bool {
    if !pattern.contains('*') {
        return pattern == value;
    }
    // Walk pattern segments separated by '*' against value.
    let parts: Vec<&str> = pattern.split('*').collect();
    let mut idx = 0;
    for (i, part) in parts.iter().enumerate() {
        if part.is_empty() {
            continue;
        }
        if i == 0 {
            if !value[idx..].starts_with(part) {
                return false;
            }
            idx += part.len();
        } else if i == parts.len() - 1 {
            return value[idx..].ends_with(part);
        } else {
            match value[idx..].find(part) {
                Some(p) => idx += p + part.len(),
                None => return false,
            }
        }
    }
    true
}

/// Normalize an `ImportSchema` into a flat list of `(file, ImportMap)`.
/// `bool::true` becomes the default file with an empty filter; `false`
/// drops out entirely.
fn flatten_imports(schema: &ImportSchema) -> Vec<ImportMap> {
    fn walk(s: &ImportSchema, out: &mut Vec<ImportMap>) {
        match s {
            ImportSchema::Bool(false) => {}
            ImportSchema::Bool(true) => out.push(ImportMap::default()),
            ImportSchema::Str(file) => out.push(ImportMap {
                file: Some(file.clone()),
                ..ImportMap::default()
            }),
            ImportSchema::Map(m) => out.push(m.clone()),
            ImportSchema::List(items) => {
                for it in items {
                    walk(it, out);
                }
            }
        }
    }
    let mut out = Vec::new();
    walk(schema, &mut out);
    out
}

struct Resolver<'a> {
    repo_root: &'a Path,
    source: &'a dyn ImportSource,
    projects: Vec<Project>,
    seen_names: HashSet<String>,
    seen_paths: HashSet<String>,
    group_filter_strs: Vec<String>,
    visited_files: HashSet<PathBuf>,
    visited_projects: HashSet<String>,
    depth: usize,
    self_: Option<ManifestRepo>,
    version: Option<String>,
}

impl<'a> Resolver<'a> {
    fn new(repo_root: &'a Path, source: &'a dyn ImportSource) -> Self {
        Self {
            repo_root,
            source,
            projects: Vec::new(),
            seen_names: HashSet::new(),
            seen_paths: HashSet::new(),
            group_filter_strs: Vec::new(),
            visited_files: HashSet::new(),
            visited_projects: HashSet::new(),
            depth: 0,
            self_: None,
            version: None,
        }
    }

    /// Process the root manifest file. Records `self`/`version` (which
    /// imports do not propagate) and recursively walks all imports.
    fn absorb_root(&mut self, file: ManifestFile) -> Result<(), ManifestError> {
        self.version = file.manifest.version.clone();
        let self_section = file.manifest.self_.clone();
        self.self_ = Some(build_self(self_section));
        self.absorb(file, ImportFilter::default(), PathBuf::new())
    }

    fn absorb(
        &mut self,
        file: ManifestFile,
        filter: ImportFilter,
        path_prefix: PathBuf,
    ) -> Result<(), ManifestError> {
        if self.depth > MAX_IMPORT_DEPTH {
            return Err(ManifestError::ImportTooDeep {
                limit: MAX_IMPORT_DEPTH,
            });
        }

        let m = file.manifest;
        let remotes: HashMap<String, &RemoteSchema> =
            m.remotes.iter().map(|r| (r.name.clone(), r)).collect();
        let defaults = m.defaults.as_ref();

        // Collect imported group-filter strings; appended to the resolver's
        // accumulated list at the end so all-imports group-filter merging
        // happens deterministically.
        for s in &m.group_filter {
            self.group_filter_strs.push(s.clone());
        }

        // Phase 1: this file's directly-defined projects (first-wins).
        for ps in &m.projects {
            if ps.name == "manifest" {
                return Err(ManifestError::ProjectNamedManifest);
            }
            for g in &ps.groups {
                if !is_valid_group(g) {
                    return Err(ManifestError::InvalidGroup {
                        project: ps.name.clone(),
                        group: g.clone(),
                    });
                }
            }
            let project = resolve_project(ps.clone(), &remotes, defaults)?;
            let prefixed_path = if path_prefix.as_os_str().is_empty() {
                project.path.clone()
            } else {
                path_prefix.join(&project.path)
            };
            let mut project = project;
            project.path = prefixed_path;
            let path_str = project.path.to_string_lossy().into_owned();
            if Path::new(&path_str).is_absolute()
                || path_str.starts_with('/')
                || path_str.starts_with('\\')
            {
                return Err(ManifestError::AbsoluteProjectPath {
                    project: project.name.clone(),
                    path: path_str,
                });
            }

            if !filter.allows(&project) {
                continue;
            }
            if self.seen_names.contains(&project.name) {
                log::debug!(
                    "manifest import: dropping duplicate project {:?} (first-wins)",
                    project.name
                );
                continue;
            }
            if !self.seen_paths.insert(path_str.clone()) {
                return Err(ManifestError::DuplicateProjectPath(path_str));
            }
            self.seen_names.insert(project.name.clone());
            self.projects.push(project);
        }

        // Phase 2: self / top-level imports (filesystem). Process self
        // first so its projects are merged with first-wins precedence
        // already established by phase 1.
        if let Some(self_section) = &m.self_
            && let Some(import) = &self_section.import
        {
            for imap in flatten_imports(import) {
                self.absorb_filesystem_import(&imap, &filter, &path_prefix, ImportSite::SelfRepo)?;
            }
        }
        if let Some(import) = &m.import {
            for imap in flatten_imports(import) {
                self.absorb_filesystem_import(&imap, &filter, &path_prefix, ImportSite::TopLevel)?;
            }
        }

        // Phase 3: per-project imports. We iterate the same projects again
        // because a project's import is "after this project is fetched";
        // skipping filtered-out projects is correct (no project ⇒ no
        // import to chase).
        for ps in &m.projects {
            let Some(import) = &ps.import else { continue };
            // Find the resolved Project (may be absent if filter dropped it).
            let project_clone = self.projects.iter().find(|p| p.name == ps.name).cloned();
            let Some(project) = project_clone else {
                continue;
            };
            for imap in flatten_imports(import) {
                self.absorb_project_import(&project, &imap, &filter, &path_prefix)?;
            }
        }

        Ok(())
    }

    fn absorb_filesystem_import(
        &mut self,
        imap: &ImportMap,
        parent_filter: &ImportFilter,
        parent_prefix: &Path,
        site: ImportSite,
    ) -> Result<(), ManifestError> {
        let file_name = imap
            .file
            .clone()
            .unwrap_or_else(|| MANIFEST_DEFAULT_FILE.to_owned());
        let abs_path = self.repo_root.join(&file_name);
        let canonical = abs_path.canonicalize().unwrap_or_else(|_| abs_path.clone());
        if !self.visited_files.insert(canonical.clone()) {
            return Err(ManifestError::ImportLoop {
                kind: site,
                target: file_name,
            });
        }

        let body = match fs::read_to_string(&abs_path) {
            Ok(b) => b,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                // Python errors on missing self imports; we match that.
                return Err(ManifestError::Io {
                    path: abs_path,
                    source: e,
                });
            }
            Err(e) => {
                return Err(ManifestError::Io {
                    path: abs_path,
                    source: e,
                });
            }
        };

        let parsed = parse_body_by_extension(&abs_path, &body)?;
        parsed
            .validate()
            .map_err(|r| ManifestError::Validation(r.to_string()))?;

        let composed = ImportFilter::compose(parent_filter, &ImportFilter::from_map(imap));
        let prefix = match imap.path_prefix.as_deref() {
            None | Some("") => parent_prefix.to_path_buf(),
            Some(p) => parent_prefix.join(p),
        };

        self.depth += 1;
        let res = self.absorb(parsed, composed, prefix);
        self.depth -= 1;
        self.visited_files.remove(&canonical);
        res
    }

    fn absorb_project_import(
        &mut self,
        project: &Project,
        imap: &ImportMap,
        parent_filter: &ImportFilter,
        parent_prefix: &Path,
    ) -> Result<(), ManifestError> {
        if !self.visited_projects.insert(project.name.clone()) {
            return Err(ManifestError::ImportLoop {
                kind: ImportSite::Project,
                target: project.name.clone(),
            });
        }

        let file = imap
            .file
            .clone()
            .unwrap_or_else(|| MANIFEST_DEFAULT_FILE.to_owned());
        let body = match self.source.project_manifest(project, &file) {
            Ok(Some(b)) => b,
            Ok(None) => {
                self.visited_projects.remove(&project.name);
                return Ok(());
            }
            Err(ImportSourceError(detail)) => {
                eprintln!(
                    "west: warning: project {:?} import failed: {detail}; skipping",
                    project.name
                );
                self.visited_projects.remove(&project.name);
                return Ok(());
            }
        };

        // Parse with the imported file's extension if explicit, else YAML
        // (default for `west.yml`).
        let pseudo_path = PathBuf::from(&file);
        let parsed = parse_body_by_extension(&pseudo_path, &body)?;
        parsed
            .validate()
            .map_err(|r| ManifestError::Validation(r.to_string()))?;

        let composed = ImportFilter::compose(parent_filter, &ImportFilter::from_map(imap));
        let prefix = match imap.path_prefix.as_deref() {
            None | Some("") => parent_prefix.to_path_buf(),
            Some(p) => parent_prefix.join(p),
        };

        self.depth += 1;
        let res = self.absorb(parsed, composed, prefix);
        self.depth -= 1;
        self.visited_projects.remove(&project.name);
        res
    }

    fn into_manifest(self) -> Result<Manifest, ManifestError> {
        let group_filter = parse_group_filter(&self.group_filter_strs, "manifest")?;
        Ok(Manifest {
            version: self.version,
            self_: self.self_.unwrap_or_default(),
            projects: self.projects,
            group_filter,
        })
    }
}

fn parse_body_by_extension(path: &Path, body: &str) -> Result<ManifestFile, ManifestError> {
    match path.extension().and_then(OsStr::to_str) {
        Some("yaml") | Some("yml") | None => {
            serde_saphyr::from_str(body).map_err(ManifestError::Yaml)
        }
        Some("toml") => toml_edit::de::from_str(body).map_err(ManifestError::Toml),
        Some("json") => serde_json::from_str(body).map_err(ManifestError::Json),
        Some(ext) => Err(ManifestError::UnsupportedFormat(ext.to_owned())),
    }
}

// =====================================================================
// Public entry points
// =====================================================================

impl Manifest {
    pub fn from_yaml_str(s: &str) -> Result<Self, ManifestError> {
        let file: ManifestFile = serde_saphyr::from_str(s).map_err(ManifestError::Yaml)?;
        validate_and_resolve(file, ImportPolicy::Strict)
    }

    pub fn from_toml_str(s: &str) -> Result<Self, ManifestError> {
        let file: ManifestFile = toml_edit::de::from_str(s).map_err(ManifestError::Toml)?;
        validate_and_resolve(file, ImportPolicy::Strict)
    }

    pub fn from_json_str(s: &str) -> Result<Self, ManifestError> {
        let file: ManifestFile = serde_json::from_str(s).map_err(ManifestError::Json)?;
        validate_and_resolve(file, ImportPolicy::Strict)
    }

    /// Sniff `.yaml` / `.yml` / `.toml` / `.json` from the path's extension.
    pub fn from_path(path: &Path) -> Result<Self, ManifestError> {
        Self::from_path_with(path, ImportPolicy::Strict)
    }

    /// Like [`Manifest::from_path`] but treats `import:` directives as a
    /// **warning** rather than an error: the directly-defined projects are
    /// still returned, with a `log::warn!` noting that imported entries
    /// will not be resolved. Useful for commands like `west update` that
    /// can do something useful with the locally-defined projects even
    /// while full import resolution remains unimplemented.
    pub fn from_path_lenient(path: &Path) -> Result<Self, ManifestError> {
        Self::from_path_with(path, ImportPolicy::WarnAndStrip)
    }

    /// Parse a manifest file and resolve all imports (top-level, self,
    /// per-project) into a single flat project list with first-wins
    /// precedence.
    ///
    /// `manifest_repo_root` is the directory the manifest lives in,
    /// used to resolve relative paths in self/top-level imports.
    /// `source` provides per-project manifest bodies on demand and is
    /// expected to ensure the project is at its manifest revision before
    /// returning the file body. See [`ImportSource`] for the contract.
    pub fn from_path_with_imports(
        path: &Path,
        manifest_repo_root: &Path,
        source: &dyn ImportSource,
    ) -> Result<Self, ManifestError> {
        let body = fs::read_to_string(path).map_err(|e| ManifestError::Io {
            path: path.to_owned(),
            source: e,
        })?;
        let file = parse_body_by_extension(path, &body)?;
        file.validate()
            .map_err(|r| ManifestError::Validation(r.to_string()))?;
        let mut resolver = Resolver::new(manifest_repo_root, source);
        // Mark the root file as visited so a self-import that names the
        // root file produces a clean ImportLoop diagnostic.
        if let Ok(canon) = path.canonicalize() {
            resolver.visited_files.insert(canon);
        }
        resolver.absorb_root(file)?;
        resolver.into_manifest()
    }

    fn from_path_with(path: &Path, policy: ImportPolicy) -> Result<Self, ManifestError> {
        let body = fs::read_to_string(path).map_err(|e| ManifestError::Io {
            path: path.to_owned(),
            source: e,
        })?;
        let file: ManifestFile = match path.extension().and_then(OsStr::to_str) {
            Some("yaml") | Some("yml") => {
                serde_saphyr::from_str(&body).map_err(ManifestError::Yaml)?
            }
            Some("toml") => toml_edit::de::from_str(&body).map_err(ManifestError::Toml)?,
            Some("json") => serde_json::from_str(&body).map_err(ManifestError::Json)?,
            other => {
                return Err(ManifestError::UnsupportedFormat(
                    other.unwrap_or("").to_owned(),
                ));
            }
        };
        validate_and_resolve(file, policy)
    }

    /// Read just `manifest.self.path` from a manifest file, tolerating
    /// `import:` directives and unknown fields. Useful at workspace bootstrap
    /// time, when we want to discover where the manifest repo should live
    /// without yet committing to a full validation pass.
    ///
    /// Returns `Ok(None)` when the file is parseable but has no `self.path`.
    /// Returns the same parse errors as the strict loaders for malformed
    /// input.
    pub fn peek_self_path(path: &Path) -> Result<Option<PathBuf>, ManifestError> {
        let body = fs::read_to_string(path).map_err(|e| ManifestError::Io {
            path: path.to_owned(),
            source: e,
        })?;
        match path.extension().and_then(OsStr::to_str) {
            Some("yaml") | Some("yml") => {
                let probe: ManifestProbeFile =
                    serde_saphyr::from_str(&body).map_err(ManifestError::Yaml)?;
                Ok(probe.into_self_path())
            }
            Some("toml") => {
                let probe: ManifestProbeFile =
                    toml_edit::de::from_str(&body).map_err(ManifestError::Toml)?;
                Ok(probe.into_self_path())
            }
            Some("json") => {
                let probe: ManifestProbeFile =
                    serde_json::from_str(&body).map_err(ManifestError::Json)?;
                Ok(probe.into_self_path())
            }
            other => Err(ManifestError::UnsupportedFormat(
                other.unwrap_or("").to_owned(),
            )),
        }
    }

    /// Look up a project by name. O(n); switch to a HashMap if profiling demands.
    pub fn project(&self, name: &str) -> Option<&Project> {
        self.projects.iter().find(|p| p.name == name)
    }

    /// Resolve user-facing selectors to projects. Each selector is matched
    /// first by project name, then by relative path equality. An unmatched
    /// selector returns [`ManifestError::UnknownProject`] — callers that
    /// want lenient matching should iterate manually.
    pub fn resolve_projects<I, S>(&self, selectors: I) -> Result<Vec<&Project>, ManifestError>
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let mut out = Vec::new();
        for sel in selectors {
            let s = sel.as_ref();
            if let Some(p) = self.project(s) {
                out.push(p);
                continue;
            }
            let as_path = std::path::Path::new(s);
            if let Some(p) = self.projects.iter().find(|p| p.path == as_path) {
                out.push(p);
                continue;
            }
            return Err(ManifestError::UnknownProject(s.to_owned()));
        }
        Ok(out)
    }

    /// Whether `project` is "active" under the manifest's `group-filter`
    /// combined with `extra_filter` (e.g. CLI `--group-filter` entries).
    ///
    /// Algorithm: a project with no `groups:` is always active. Otherwise,
    /// walk the combined filter in order maintaining a "disabled groups"
    /// set — `-foo` adds, `+foo` removes. The project is active iff at
    /// least one of its groups is *not* in the disabled set. (This
    /// matches Python's "last matching ± entry wins" behavior because a
    /// later `+foo` clears `foo` from the set, and a later `-foo` adds it.)
    pub fn is_active(&self, project: &Project, extra_filter: &[GroupFilterEntry]) -> bool {
        if project.groups.is_empty() {
            return true;
        }
        let mut disabled: HashSet<&str> = HashSet::new();
        for entry in self.group_filter.iter().chain(extra_filter.iter()) {
            if entry.disabled {
                disabled.insert(&entry.group);
            } else {
                disabled.remove(entry.group.as_str());
            }
        }
        project
            .groups
            .iter()
            .any(|g| !disabled.contains(g.as_str()))
    }
}

/// Parse user-supplied group-filter strings (CLI flag `--group-filter`).
///
/// Each input may itself be comma-separated (`west update --gf +a,-b -gf +c`).
/// Empty pieces between commas are ignored; whitespace around items is
/// trimmed. Each non-empty piece must begin with `+` or `-` and name a
/// valid group, identical to manifest-side validation.
pub fn parse_cli_group_filter(items: &[String]) -> Result<Vec<GroupFilterEntry>, ManifestError> {
    let mut split: Vec<String> = Vec::new();
    for raw in items {
        for piece in raw.split(',') {
            let trimmed = piece.trim();
            if !trimmed.is_empty() {
                split.push(trimmed.to_owned());
            }
        }
    }
    parse_group_filter(&split, "command line")
}

// =====================================================================
// Validation + resolution
// =====================================================================

/// How to handle `import:` directives during validation. `Strict` errors;
/// `WarnAndStrip` logs a `log::warn!` and continues with the directly-defined
/// projects.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ImportPolicy {
    Strict,
    WarnAndStrip,
}

fn validate_and_resolve(
    file: ManifestFile,
    policy: ImportPolicy,
) -> Result<Manifest, ManifestError> {
    file.validate()
        .map_err(|r| ManifestError::Validation(r.to_string()))?;
    resolve(file, policy)
}

fn resolve(file: ManifestFile, policy: ImportPolicy) -> Result<Manifest, ManifestError> {
    let m = file.manifest;

    if m.import.is_some() {
        match policy {
            ImportPolicy::Strict => {
                return Err(ManifestError::ImportNotSupported {
                    context: "top-level".into(),
                });
            }
            ImportPolicy::WarnAndStrip => {
                eprintln!(
                    "west: warning: manifest top-level `import:` is unsupported and \
                     will be ignored; projects pulled in by the import will not be updated"
                );
            }
        }
    }

    if let Some(self_) = &m.self_
        && self_.import.is_some()
    {
        match policy {
            ImportPolicy::Strict => {
                return Err(ManifestError::ImportNotSupported {
                    context: "self".into(),
                });
            }
            ImportPolicy::WarnAndStrip => {
                eprintln!(
                    "west: warning: manifest `self.import:` is unsupported and will be ignored"
                );
            }
        }
    }

    let remotes: HashMap<String, &RemoteSchema> =
        m.remotes.iter().map(|r| (r.name.clone(), r)).collect();
    let defaults = m.defaults.as_ref();

    let mut projects: Vec<Project> = Vec::with_capacity(m.projects.len());
    let mut seen_names: HashSet<String> = HashSet::new();
    let mut seen_paths: HashSet<String> = HashSet::new();

    for ps in m.projects {
        if ps.name == "manifest" {
            return Err(ManifestError::ProjectNamedManifest);
        }
        if !seen_names.insert(ps.name.clone()) {
            return Err(ManifestError::DuplicateProjectName(ps.name.clone()));
        }
        if ps.import.is_some() {
            match policy {
                ImportPolicy::Strict => {
                    return Err(ManifestError::ImportNotSupported {
                        context: format!("project {:?}", ps.name),
                    });
                }
                ImportPolicy::WarnAndStrip => {
                    eprintln!(
                        "west: warning: project {:?}: `import:` is unsupported and will be \
                         ignored; projects from {:?}'s manifest will not be updated",
                        ps.name, ps.name
                    );
                }
            }
        }
        for g in &ps.groups {
            if !is_valid_group(g) {
                return Err(ManifestError::InvalidGroup {
                    project: ps.name.clone(),
                    group: g.clone(),
                });
            }
        }

        let project = resolve_project(ps, &remotes, defaults)?;

        // Path uniqueness + safety.
        let path_str = project.path.to_string_lossy().into_owned();
        if !seen_paths.insert(path_str.clone()) {
            return Err(ManifestError::DuplicateProjectPath(path_str));
        }
        if Path::new(&path_str).is_absolute()
            || path_str.starts_with('/')
            || path_str.starts_with('\\')
        {
            return Err(ManifestError::AbsoluteProjectPath {
                project: project.name.clone(),
                path: path_str,
            });
        }

        projects.push(project);
    }

    let group_filter = parse_group_filter(&m.group_filter, "manifest")?;
    let self_ = build_self(m.self_);

    Ok(Manifest {
        version: m.version,
        self_,
        projects,
        group_filter,
    })
}

fn resolve_project(
    ps: ProjectSchema,
    remotes: &HashMap<String, &RemoteSchema>,
    defaults: Option<&DefaultsSchema>,
) -> Result<Project, ManifestError> {
    // URL resolution. `url`, `remote`, and `repo-path` are mutually
    // constrained because each describes the fetch URL differently and
    // letting them combine would silently pick one and ignore the others.
    if ps.url.is_some() && ps.remote.is_some() {
        return Err(ManifestError::UrlAndRemote {
            project: ps.name.clone(),
        });
    }
    if ps.url.is_some() && ps.repo_path.is_some() {
        return Err(ManifestError::UrlAndRepoPath {
            project: ps.name.clone(),
        });
    }

    // The remote that picks the URL base (and feeds remote_name).
    let url_remote_name: Option<String> = ps
        .remote
        .clone()
        .or_else(|| defaults.and_then(|d| d.remote.clone()));

    let url = if let Some(u) = ps.url.clone() {
        u
    } else if let Some(rn) = &url_remote_name {
        let remote = remotes
            .get(rn)
            .ok_or_else(|| ManifestError::UnknownRemote {
                project: ps.name.clone(),
                remote: rn.clone(),
            })?;
        let suffix = ps.repo_path.clone().unwrap_or_else(|| ps.name.clone());
        format!("{}/{}", remote.url_base, suffix)
    } else {
        return Err(ManifestError::NoUrl(ps.name.clone()));
    };

    let revision = ps
        .revision
        .or_else(|| defaults.and_then(|d| d.revision.clone()))
        .unwrap_or_else(|| "master".to_owned());

    let path = PathBuf::from(ps.path.unwrap_or_else(|| ps.name.clone()));

    // The git remote name written into the cloned repo. Prefer the explicit
    // `remote-name` field; fall back to the manifest remote that supplied the
    // URL base; default to "origin" when only a bare `url` was given.
    let remote_name = ps
        .remote_name
        .or(url_remote_name)
        .unwrap_or_else(|| "origin".to_owned());

    let west_commands = ps
        .west_commands
        .map(OneOrMany::into_vec)
        .unwrap_or_default()
        .into_iter()
        .map(PathBuf::from)
        .collect();

    let submodules = match ps.submodules {
        None => Submodules::None,
        Some(SubmodulesSchema::All(true)) => Submodules::All,
        Some(SubmodulesSchema::All(false)) => Submodules::None,
        Some(SubmodulesSchema::Specific(items)) => Submodules::Specific(
            items
                .into_iter()
                .map(|s| Submodule {
                    path: PathBuf::from(s.path),
                    name: s.name,
                })
                .collect(),
        ),
    };

    Ok(Project {
        name: ps.name,
        url,
        revision,
        path,
        description: ps.description,
        groups: ps.groups,
        clone_depth: ps.clone_depth,
        west_commands,
        remote_name,
        submodules,
    })
}

fn build_self(s: Option<SelfSchema>) -> ManifestRepo {
    let s = s.unwrap_or(SelfSchema {
        path: None,
        west_commands: None,
        import: None,
    });
    ManifestRepo {
        path: PathBuf::from(s.path.unwrap_or_else(|| "manifest".to_owned())),
        west_commands: s
            .west_commands
            .map(OneOrMany::into_vec)
            .unwrap_or_default()
            .into_iter()
            .map(PathBuf::from)
            .collect(),
    }
}

fn parse_group_filter(
    raw: &[String],
    source: &str,
) -> Result<Vec<GroupFilterEntry>, ManifestError> {
    let mut out = Vec::with_capacity(raw.len());
    for item in raw {
        if item.is_empty() {
            return Err(ManifestError::InvalidGroupFilter {
                source: source.to_owned(),
                item: item.clone(),
                reason: "must begin with `+` or `-`".into(),
            });
        }
        let (disabled, group) = match item.as_bytes()[0] {
            b'+' => (false, &item[1..]),
            b'-' => (true, &item[1..]),
            _ => {
                return Err(ManifestError::InvalidGroupFilter {
                    source: source.to_owned(),
                    item: item.clone(),
                    reason: "must begin with `+` or `-`".into(),
                });
            }
        };
        if !is_valid_group(group) {
            return Err(ManifestError::InvalidGroupFilter {
                source: source.to_owned(),
                item: item.clone(),
                reason: format!("{group:?} is not a valid group name"),
            });
        }
        out.push(GroupFilterEntry {
            group: group.to_owned(),
            disabled,
        });
    }
    Ok(out)
}

/// A valid group name is non-empty, contains no whitespace/comma/colon, and
/// does not begin with `+` or `-`. Whitespace and commas would break common
/// group-list serializations; colons clash with potential future namespacing;
/// the leading `+`/`-` are reserved for `group-filter` enable/disable syntax.
fn is_valid_group(g: &str) -> bool {
    if g.is_empty() {
        return false;
    }
    let first = g.as_bytes()[0];
    if first == b'+' || first == b'-' {
        return false;
    }
    !g.chars().any(|c| c.is_whitespace() || c == ',' || c == ':')
}

#[cfg(test)]
mod tests {
    use super::*;

    fn yaml(s: &str) -> Result<Manifest, ManifestError> {
        Manifest::from_yaml_str(s)
    }

    #[test]
    fn parse_minimal_yaml() {
        let m = yaml(
            r#"
manifest:
  remotes:
    - name: u
      url-base: https://example.com/u
  projects:
    - name: p
      remote: u
"#,
        )
        .unwrap();
        assert_eq!(m.projects.len(), 1);
        let p = &m.projects[0];
        assert_eq!(p.name, "p");
        assert_eq!(p.url, "https://example.com/u/p");
        assert_eq!(p.revision, "master");
        assert_eq!(p.path, PathBuf::from("p"));
        assert_eq!(p.remote_name, "u");
        assert!(matches!(p.submodules, Submodules::None));
    }

    #[test]
    fn parse_full_yaml_fixture() {
        let body = include_str!("../tests/fixtures/zephyr-like.yml");
        let m = Manifest::from_yaml_str(body).unwrap();
        assert_eq!(m.version.as_deref(), Some("1.2"));
        assert_eq!(m.self_.path, PathBuf::from("manifest"));
        assert_eq!(
            m.self_.west_commands,
            vec![PathBuf::from("scripts/west-commands.yml")]
        );
        assert_eq!(m.projects.len(), 6);
        assert_eq!(m.group_filter.len(), 2);
        assert_eq!(m.group_filter[0].group, "optional");
        assert!(m.group_filter[0].disabled);
        assert_eq!(m.group_filter[1].group, "core");
        assert!(!m.group_filter[1].disabled);
    }

    #[test]
    fn parse_yaml_toml_json_equivalent() {
        let yaml_body = include_str!("../tests/fixtures/zephyr-like.yml");
        let toml_body = include_str!("../tests/fixtures/zephyr-like.toml");
        let json_body = include_str!("../tests/fixtures/zephyr-like.json");
        let from_yaml = Manifest::from_yaml_str(yaml_body).unwrap();
        let from_toml = Manifest::from_toml_str(toml_body).unwrap();
        let from_json = Manifest::from_json_str(json_body).unwrap();
        assert_eq!(from_yaml, from_toml);
        assert_eq!(from_yaml, from_json);
    }

    #[test]
    fn from_path_dispatches_by_extension() {
        let dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures");
        let from_yml = Manifest::from_path(&dir.join("zephyr-like.yml")).unwrap();
        let from_toml = Manifest::from_path(&dir.join("zephyr-like.toml")).unwrap();
        let from_json = Manifest::from_path(&dir.join("zephyr-like.json")).unwrap();
        assert_eq!(from_yml, from_toml);
        assert_eq!(from_yml, from_json);

        // Unsupported extension.
        let tmp = tempfile::NamedTempFile::with_suffix(".xml").unwrap();
        std::fs::write(tmp.path(), "<x/>").unwrap();
        assert!(matches!(
            Manifest::from_path(tmp.path()),
            Err(ManifestError::UnsupportedFormat(_))
        ));
    }

    #[test]
    fn parse_minimal_json() {
        let m = Manifest::from_json_str(
            r#"{
                "manifest": {
                    "remotes": [{"name": "u", "url-base": "https://example.com/u"}],
                    "projects": [{"name": "p", "remote": "u"}]
                }
            }"#,
        )
        .unwrap();
        assert_eq!(m.projects.len(), 1);
        assert_eq!(m.projects[0].url, "https://example.com/u/p");
    }

    #[test]
    fn json_validation_rejects_unknown_field() {
        let res = Manifest::from_json_str(
            r#"{"manifest": {"projects": [{"name": "p", "url": "x", "gibberish": true}]}}"#,
        );
        assert!(matches!(res, Err(ManifestError::Json(_))), "got {res:?}");
    }

    #[test]
    fn unknown_field_rejected() {
        let res = yaml(
            r#"
manifest:
  remotes:
    - name: u
      url-base: https://example.com
  projects:
    - name: p
      remote: u
      gibberish: yes
"#,
        );
        assert!(matches!(res, Err(ManifestError::Yaml(_))), "got {res:?}");
    }

    #[test]
    fn validation_invalid_project_name_slash() {
        let res = yaml(
            r#"
manifest:
  remotes:
    - name: u
      url-base: x
  projects:
    - name: bad/name
      remote: u
"#,
        );
        assert!(matches!(res, Err(ManifestError::Validation(_))));
    }

    #[test]
    fn validation_unique_project_names() {
        let res = yaml(
            r#"
manifest:
  remotes:
    - name: u
      url-base: x
  projects:
    - name: dup
      remote: u
    - name: dup
      remote: u
"#,
        );
        assert!(matches!(res, Err(ManifestError::DuplicateProjectName(n)) if n == "dup"));
    }

    #[test]
    fn validation_unique_project_paths() {
        let res = yaml(
            r#"
manifest:
  remotes:
    - name: u
      url-base: x
  projects:
    - name: a
      remote: u
      path: same
    - name: b
      remote: u
      path: same
"#,
        );
        assert!(matches!(res, Err(ManifestError::DuplicateProjectPath(_))));
    }

    #[test]
    fn validation_unknown_remote() {
        let res = yaml(
            r#"
manifest:
  remotes:
    - name: u
      url-base: x
  projects:
    - name: p
      remote: missing
"#,
        );
        assert!(matches!(
            res,
            Err(ManifestError::UnknownRemote { project, remote })
                if project == "p" && remote == "missing"
        ));
    }

    #[test]
    fn validation_no_url_no_remote_no_default() {
        let res = yaml(
            r#"
manifest:
  remotes:
    - name: u
      url-base: x
  projects:
    - name: orphan
"#,
        );
        assert!(matches!(res, Err(ManifestError::NoUrl(n)) if n == "orphan"));
    }

    #[test]
    fn validation_url_and_remote_conflict() {
        let res = yaml(
            r#"
manifest:
  remotes:
    - name: u
      url-base: x
  projects:
    - name: p
      remote: u
      url: https://example.com/p
"#,
        );
        assert!(matches!(res, Err(ManifestError::UrlAndRemote { .. })));
    }

    #[test]
    fn validation_url_and_repo_path_conflict() {
        let res = yaml(
            r#"
manifest:
  projects:
    - name: p
      url: https://example.com/p
      repo-path: foo
"#,
        );
        assert!(matches!(res, Err(ManifestError::UrlAndRepoPath { .. })));
    }

    #[test]
    fn url_resolution_explicit_url() {
        let m = yaml(
            r#"
manifest:
  projects:
    - name: p
      url: https://example.com/p
"#,
        )
        .unwrap();
        assert_eq!(m.projects[0].url, "https://example.com/p");
        assert_eq!(m.projects[0].remote_name, "origin");
    }

    #[test]
    fn url_resolution_via_remote_and_repo_path() {
        let m = yaml(
            r#"
manifest:
  remotes:
    - name: u
      url-base: https://example.com/base
  projects:
    - name: p
      remote: u
      repo-path: actual-repo
"#,
        )
        .unwrap();
        assert_eq!(m.projects[0].url, "https://example.com/base/actual-repo");
        assert_eq!(m.projects[0].remote_name, "u");
    }

    #[test]
    fn url_resolution_via_default_remote() {
        let m = yaml(
            r#"
manifest:
  remotes:
    - name: u
      url-base: https://example.com
  defaults:
    remote: u
    revision: main
  projects:
    - name: p
"#,
        )
        .unwrap();
        assert_eq!(m.projects[0].url, "https://example.com/p");
        assert_eq!(m.projects[0].revision, "main");
        assert_eq!(m.projects[0].remote_name, "u");
    }

    #[test]
    fn default_revision_master() {
        let m = yaml(
            r#"
manifest:
  projects:
    - name: p
      url: https://example.com/p
"#,
        )
        .unwrap();
        assert_eq!(m.projects[0].revision, "master");
    }

    #[test]
    fn peek_self_path_returns_path_on_yaml() {
        let dir = tempfile::TempDir::new().unwrap();
        let p = dir.path().join("m.yml");
        std::fs::write(
            &p,
            r#"
manifest:
  self:
    path: my-manifest
  projects:
    - name: x
      url: https://example.com/x
"#,
        )
        .unwrap();
        let got = Manifest::peek_self_path(&p).unwrap();
        assert_eq!(got, Some(PathBuf::from("my-manifest")));
    }

    #[test]
    fn peek_self_path_tolerates_imports_and_unknown_fields() {
        // Mirrors zephyrproject-rtos/example-application's shape: project-level
        // `import:` plus `name-allowlist`, plus an unknown top-level key. The
        // strict loader rejects all of these; `peek_self_path` must not.
        let dir = tempfile::TempDir::new().unwrap();
        let p = dir.path().join("m.yml");
        std::fs::write(
            &p,
            r#"
manifest:
  self:
    path: example-application
  unknown-future-key: 42
  remotes:
    - name: r
      url-base: https://example.com
  projects:
    - name: zephyr
      remote: r
      revision: main
      import:
        name-allowlist: [cmsis]
"#,
        )
        .unwrap();
        let got = Manifest::peek_self_path(&p).unwrap();
        assert_eq!(got, Some(PathBuf::from("example-application")));
    }

    #[test]
    fn peek_self_path_returns_none_when_unset() {
        let dir = tempfile::TempDir::new().unwrap();
        let p = dir.path().join("m.yml");
        std::fs::write(
            &p,
            r#"
manifest:
  projects:
    - name: x
      url: https://example.com/x
"#,
        )
        .unwrap();
        assert_eq!(Manifest::peek_self_path(&p).unwrap(), None);
    }

    #[test]
    fn peek_self_path_propagates_parse_errors() {
        let dir = tempfile::TempDir::new().unwrap();
        let p = dir.path().join("m.yml");
        std::fs::write(&p, "manifest: [not, valid").unwrap();
        assert!(matches!(
            Manifest::peek_self_path(&p),
            Err(ManifestError::Yaml(_))
        ));
    }

    #[test]
    fn import_key_top_level_rejected() {
        let res = yaml(
            r#"
manifest:
  projects:
    - name: p
      url: https://x
  import: foo.yml
"#,
        );
        assert!(matches!(
            res,
            Err(ManifestError::ImportNotSupported { context }) if context == "top-level"
        ));
    }

    #[test]
    fn import_key_self_rejected() {
        let res = yaml(
            r#"
manifest:
  self:
    import: foo.yml
  projects:
    - name: p
      url: https://x
"#,
        );
        assert!(matches!(
            res,
            Err(ManifestError::ImportNotSupported { context }) if context == "self"
        ));
    }

    #[test]
    fn import_key_project_rejected() {
        let res = yaml(
            r#"
manifest:
  projects:
    - name: p
      url: https://x
      import: foo.yml
"#,
        );
        assert!(matches!(
            res,
            Err(ManifestError::ImportNotSupported { context }) if context.contains("p")
        ));
    }

    #[test]
    fn from_path_lenient_strips_project_imports() {
        // Mirror of zephyrproject-rtos/example-application: one project with
        // an `import:` key. Strict loader rejects; lenient loader returns
        // the project (without the imported entries it would have pulled in).
        let dir = tempfile::TempDir::new().unwrap();
        let path = dir.path().join("west.yml");
        std::fs::write(
            &path,
            r#"
manifest:
  remotes:
    - name: zephyrproject-rtos
      url-base: https://github.com/zephyrproject-rtos
  projects:
    - name: zephyr
      remote: zephyrproject-rtos
      revision: main
      import:
        name-allowlist:
          - cmsis
"#,
        )
        .unwrap();
        // Strict still errors.
        assert!(matches!(
            Manifest::from_path(&path),
            Err(ManifestError::ImportNotSupported { .. })
        ));
        // Lenient succeeds and returns the directly-defined project.
        let m = Manifest::from_path_lenient(&path).unwrap();
        assert_eq!(m.projects.len(), 1);
        assert_eq!(m.projects[0].name, "zephyr");
    }

    #[test]
    fn from_path_lenient_strips_top_level_import() {
        let dir = tempfile::TempDir::new().unwrap();
        let path = dir.path().join("west.yml");
        std::fs::write(
            &path,
            r#"
manifest:
  import: extras.yml
  projects:
    - name: p
      url: https://x
"#,
        )
        .unwrap();
        let m = Manifest::from_path_lenient(&path).unwrap();
        assert_eq!(m.projects.len(), 1);
        assert_eq!(m.projects[0].name, "p");
    }

    #[test]
    fn submodules_all_via_bool_true() {
        let m = yaml(
            r#"
manifest:
  projects:
    - name: p
      url: https://x
      submodules: true
"#,
        )
        .unwrap();
        assert!(matches!(m.projects[0].submodules, Submodules::All));
    }

    #[test]
    fn submodules_none_via_bool_false() {
        let m = yaml(
            r#"
manifest:
  projects:
    - name: p
      url: https://x
      submodules: false
"#,
        )
        .unwrap();
        assert!(matches!(m.projects[0].submodules, Submodules::None));
    }

    #[test]
    fn submodules_specific_list() {
        let m = yaml(
            r#"
manifest:
  projects:
    - name: p
      url: https://x
      submodules:
        - path: a
        - path: b
          name: bee
"#,
        )
        .unwrap();
        match &m.projects[0].submodules {
            Submodules::Specific(items) => {
                assert_eq!(items.len(), 2);
                assert_eq!(items[0].path, PathBuf::from("a"));
                assert!(items[0].name.is_none());
                assert_eq!(items[1].path, PathBuf::from("b"));
                assert_eq!(items[1].name.as_deref(), Some("bee"));
            }
            other => panic!("expected Specific, got {other:?}"),
        }
    }

    #[test]
    fn group_filter_invalid_no_sign() {
        let res = yaml(
            r#"
manifest:
  group-filter: [foo]
  projects:
    - name: p
      url: https://x
"#,
        );
        assert!(matches!(res, Err(ManifestError::InvalidGroupFilter { .. })));
    }

    #[test]
    fn group_invalid_starts_with_plus() {
        let res = yaml(
            r#"
manifest:
  projects:
    - name: p
      url: https://x
      groups: ["+badname"]
"#,
        );
        assert!(matches!(res, Err(ManifestError::InvalidGroup { .. })));
    }

    #[test]
    fn project_name_manifest_rejected() {
        let res = yaml(
            r#"
manifest:
  projects:
    - name: manifest
      url: https://x
"#,
        );
        assert!(matches!(res, Err(ManifestError::ProjectNamedManifest)));
    }

    #[test]
    fn west_commands_string_or_list() {
        let m1 = yaml(
            r#"
manifest:
  projects:
    - name: p1
      url: https://x
      west-commands: single.yml
"#,
        )
        .unwrap();
        assert_eq!(
            m1.projects[0].west_commands,
            vec![PathBuf::from("single.yml")]
        );

        let m2 = yaml(
            r#"
manifest:
  projects:
    - name: p2
      url: https://x
      west-commands: [a.yml, b.yml]
"#,
        )
        .unwrap();
        assert_eq!(
            m2.projects[0].west_commands,
            vec![PathBuf::from("a.yml"), PathBuf::from("b.yml")]
        );
    }

    #[test]
    fn project_lookup() {
        let m = yaml(
            r#"
manifest:
  projects:
    - name: a
      url: https://x
    - name: b
      url: https://y
"#,
        )
        .unwrap();
        assert!(m.project("a").is_some());
        assert!(m.project("b").is_some());
        assert!(m.project("c").is_none());
    }

    #[test]
    fn resolve_projects_by_name_and_path() {
        let m = yaml(
            r#"
manifest:
  projects:
    - name: a
      url: https://x
      path: aa/inner
    - name: b
      url: https://y
"#,
        )
        .unwrap();
        let resolved = m.resolve_projects(["a", "aa/inner", "b"]).unwrap();
        let names: Vec<&str> = resolved.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, vec!["a", "a", "b"]);
    }

    #[test]
    fn resolve_projects_unknown_errors() {
        let m = yaml(
            r#"
manifest:
  projects:
    - name: a
      url: https://x
"#,
        )
        .unwrap();
        let err = m.resolve_projects(["nope"]).unwrap_err();
        assert!(matches!(err, ManifestError::UnknownProject(s) if s == "nope"));
    }

    #[test]
    fn is_active_no_groups_is_always_active() {
        let m = yaml(
            r#"
manifest:
  group-filter: [-experimental]
  projects:
    - name: a
      url: https://x
"#,
        )
        .unwrap();
        let p = m.project("a").unwrap();
        assert!(m.is_active(p, &[]));
    }

    #[test]
    fn is_active_disabled_when_only_group_disabled() {
        let m = yaml(
            r#"
manifest:
  group-filter: [-experimental]
  projects:
    - name: a
      url: https://x
      groups: [experimental]
"#,
        )
        .unwrap();
        let p = m.project("a").unwrap();
        assert!(!m.is_active(p, &[]));
    }

    #[test]
    fn is_active_extra_filter_re_enables() {
        let m = yaml(
            r#"
manifest:
  group-filter: [-experimental]
  projects:
    - name: a
      url: https://x
      groups: [experimental]
"#,
        )
        .unwrap();
        let p = m.project("a").unwrap();
        let extra = parse_cli_group_filter(&["+experimental".to_owned()]).unwrap();
        assert!(m.is_active(p, &extra));
    }

    #[test]
    fn is_active_active_when_any_group_enabled() {
        let m = yaml(
            r#"
manifest:
  group-filter: [-debug]
  projects:
    - name: a
      url: https://x
      groups: [debug, prod]
"#,
        )
        .unwrap();
        let p = m.project("a").unwrap();
        // prod is not in disabled set → project is active.
        assert!(m.is_active(p, &[]));
    }

    #[test]
    fn parse_cli_group_filter_handles_comma_split() {
        let parsed = parse_cli_group_filter(&["+a,-b".to_owned(), "+c".to_owned()]).unwrap();
        assert_eq!(parsed.len(), 3);
        assert_eq!(parsed[0].group, "a");
        assert!(!parsed[0].disabled);
        assert_eq!(parsed[1].group, "b");
        assert!(parsed[1].disabled);
        assert_eq!(parsed[2].group, "c");
        assert!(!parsed[2].disabled);
    }

    #[test]
    fn parse_cli_group_filter_rejects_bare_name() {
        let err = parse_cli_group_filter(&["foo".to_owned()]).unwrap_err();
        assert!(matches!(err, ManifestError::InvalidGroupFilter { .. }));
    }

    // =================================================================
    // Import resolution
    // =================================================================

    use std::cell::RefCell;
    use std::collections::BTreeMap;

    /// Test double for [`ImportSource`] that maps project names to YAML
    /// bodies. Returns `Ok(None)` for unmapped projects (silently
    /// skipped, matching Python's "missing import file" behaviour).
    struct StaticImportSource {
        manifests: BTreeMap<String, String>,
        calls: RefCell<Vec<(String, String)>>,
    }

    impl StaticImportSource {
        fn new() -> Self {
            Self {
                manifests: BTreeMap::new(),
                calls: RefCell::new(Vec::new()),
            }
        }
        fn with(mut self, project: &str, body: &str) -> Self {
            self.manifests.insert(project.to_owned(), body.to_owned());
            self
        }
    }

    impl ImportSource for StaticImportSource {
        fn project_manifest(
            &self,
            project: &Project,
            file: &str,
        ) -> Result<Option<String>, ImportSourceError> {
            self.calls
                .borrow_mut()
                .push((project.name.clone(), file.to_owned()));
            Ok(self.manifests.get(&project.name).cloned())
        }
    }

    /// Write `body` to `<dir>/<name>` and return the path.
    fn write_yaml(dir: &Path, name: &str, body: &str) -> PathBuf {
        let path = dir.join(name);
        std::fs::write(&path, body).unwrap();
        path
    }

    #[test]
    fn import_self_string_pulls_in_extra_projects() {
        let dir = tempfile::TempDir::new().unwrap();
        let root = write_yaml(
            dir.path(),
            "west.yml",
            r#"
manifest:
  self:
    import: extras.yml
  projects:
    - name: p
      url: https://x
"#,
        );
        write_yaml(
            dir.path(),
            "extras.yml",
            r#"
manifest:
  projects:
    - name: q
      url: https://y
"#,
        );
        let source = StaticImportSource::new();
        let m = Manifest::from_path_with_imports(&root, dir.path(), &source).unwrap();
        let names: Vec<&str> = m.projects.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, vec!["p", "q"]);
    }

    #[test]
    fn import_self_bool_true_means_west_yml() {
        // self.import: true should look for `west.yml` next to the root,
        // which IS the root; that should produce ImportLoop.
        let dir = tempfile::TempDir::new().unwrap();
        let root = write_yaml(
            dir.path(),
            "west.yml",
            r#"
manifest:
  self:
    import: true
  projects:
    - name: p
      url: https://x
"#,
        );
        let source = StaticImportSource::new();
        let err = Manifest::from_path_with_imports(&root, dir.path(), &source).unwrap_err();
        assert!(
            matches!(
                err,
                ManifestError::ImportLoop {
                    kind: ImportSite::SelfRepo,
                    ..
                }
            ),
            "got: {err:?}"
        );
    }

    #[test]
    fn import_self_list_loads_each_file_in_order() {
        let dir = tempfile::TempDir::new().unwrap();
        let root = write_yaml(
            dir.path(),
            "west.yml",
            r#"
manifest:
  self:
    import:
      - a.yml
      - b.yml
  projects:
    - name: p
      url: https://x
"#,
        );
        write_yaml(
            dir.path(),
            "a.yml",
            r#"
manifest:
  projects:
    - name: q
      url: https://y
"#,
        );
        write_yaml(
            dir.path(),
            "b.yml",
            r#"
manifest:
  projects:
    - name: r
      url: https://z
"#,
        );
        let source = StaticImportSource::new();
        let m = Manifest::from_path_with_imports(&root, dir.path(), &source).unwrap();
        let names: Vec<&str> = m.projects.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, vec!["p", "q", "r"]);
    }

    #[test]
    fn import_self_map_name_allowlist_filters() {
        let dir = tempfile::TempDir::new().unwrap();
        let root = write_yaml(
            dir.path(),
            "west.yml",
            r#"
manifest:
  self:
    import:
      file: extras.yml
      name-allowlist: [keepme]
  projects:
    - name: p
      url: https://x
"#,
        );
        write_yaml(
            dir.path(),
            "extras.yml",
            r#"
manifest:
  projects:
    - name: keepme
      url: https://y
    - name: dropme
      url: https://z
"#,
        );
        let source = StaticImportSource::new();
        let m = Manifest::from_path_with_imports(&root, dir.path(), &source).unwrap();
        let names: Vec<&str> = m.projects.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, vec!["p", "keepme"]);
    }

    #[test]
    fn import_self_map_path_blocklist_rejects() {
        let dir = tempfile::TempDir::new().unwrap();
        let root = write_yaml(
            dir.path(),
            "west.yml",
            r#"
manifest:
  self:
    import:
      file: extras.yml
      path-blocklist: ["lib/*"]
  projects:
    - name: p
      url: https://x
"#,
        );
        write_yaml(
            dir.path(),
            "extras.yml",
            r#"
manifest:
  projects:
    - name: a
      url: https://y
      path: lib/a
    - name: b
      url: https://z
      path: bin/b
"#,
        );
        let source = StaticImportSource::new();
        let m = Manifest::from_path_with_imports(&root, dir.path(), &source).unwrap();
        let names: Vec<&str> = m.projects.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, vec!["p", "b"]);
    }

    #[test]
    fn import_self_map_path_prefix_applied() {
        let dir = tempfile::TempDir::new().unwrap();
        let root = write_yaml(
            dir.path(),
            "west.yml",
            r#"
manifest:
  self:
    import:
      file: extras.yml
      path-prefix: vendor
  projects:
    - name: p
      url: https://x
"#,
        );
        write_yaml(
            dir.path(),
            "extras.yml",
            r#"
manifest:
  projects:
    - name: q
      url: https://y
"#,
        );
        let source = StaticImportSource::new();
        let m = Manifest::from_path_with_imports(&root, dir.path(), &source).unwrap();
        let q = m.project("q").unwrap();
        assert_eq!(q.path, PathBuf::from("vendor/q"));
    }

    #[test]
    fn import_first_wins_silently_drops_duplicate() {
        let dir = tempfile::TempDir::new().unwrap();
        let root = write_yaml(
            dir.path(),
            "west.yml",
            r#"
manifest:
  self:
    import: extras.yml
  projects:
    - name: p
      url: https://parent
"#,
        );
        write_yaml(
            dir.path(),
            "extras.yml",
            r#"
manifest:
  projects:
    - name: p
      url: https://imported
"#,
        );
        let source = StaticImportSource::new();
        let m = Manifest::from_path_with_imports(&root, dir.path(), &source).unwrap();
        assert_eq!(m.projects.len(), 1);
        // Parent wins.
        assert_eq!(m.projects[0].url, "https://parent");
    }

    #[test]
    fn import_self_cycle_errors_with_loop_diagnostic() {
        let dir = tempfile::TempDir::new().unwrap();
        let root = write_yaml(
            dir.path(),
            "west.yml",
            r#"
manifest:
  self:
    import: a.yml
  projects:
    - name: p
      url: https://x
"#,
        );
        write_yaml(
            dir.path(),
            "a.yml",
            r#"
manifest:
  self:
    import: west.yml
"#,
        );
        let source = StaticImportSource::new();
        let err = Manifest::from_path_with_imports(&root, dir.path(), &source).unwrap_err();
        assert!(
            matches!(
                err,
                ManifestError::ImportLoop {
                    kind: ImportSite::SelfRepo,
                    ..
                }
            ),
            "got: {err:?}"
        );
    }

    #[test]
    fn import_project_resolves_via_source() {
        let dir = tempfile::TempDir::new().unwrap();
        let root = write_yaml(
            dir.path(),
            "west.yml",
            r#"
manifest:
  projects:
    - name: zephyr
      url: https://example.com/zephyr
      import: true
"#,
        );
        let source = StaticImportSource::new().with(
            "zephyr",
            r#"
manifest:
  projects:
    - name: cmsis
      url: https://example.com/cmsis
"#,
        );
        let m = Manifest::from_path_with_imports(&root, dir.path(), &source).unwrap();
        let names: Vec<&str> = m.projects.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, vec!["zephyr", "cmsis"]);
        assert_eq!(source.calls.borrow().len(), 1);
        assert_eq!(source.calls.borrow()[0].0, "zephyr");
    }

    #[test]
    fn import_project_with_name_blocklist_skips() {
        let dir = tempfile::TempDir::new().unwrap();
        let root = write_yaml(
            dir.path(),
            "west.yml",
            r#"
manifest:
  projects:
    - name: zephyr
      url: https://example.com/zephyr
      import:
        name-blocklist: [hal_nordic]
"#,
        );
        let source = StaticImportSource::new().with(
            "zephyr",
            r#"
manifest:
  projects:
    - name: cmsis
      url: https://example.com/cmsis
    - name: hal_nordic
      url: https://example.com/hal_nordic
"#,
        );
        let m = Manifest::from_path_with_imports(&root, dir.path(), &source).unwrap();
        let names: Vec<&str> = m.projects.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, vec!["zephyr", "cmsis"]);
    }

    #[test]
    fn import_project_missing_file_silently_skipped() {
        let dir = tempfile::TempDir::new().unwrap();
        let root = write_yaml(
            dir.path(),
            "west.yml",
            r#"
manifest:
  projects:
    - name: zephyr
      url: https://example.com/zephyr
      import: true
"#,
        );
        let source = StaticImportSource::new(); // no entry for "zephyr"
        let m = Manifest::from_path_with_imports(&root, dir.path(), &source).unwrap();
        let names: Vec<&str> = m.projects.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, vec!["zephyr"]);
    }

    #[test]
    fn import_path_prefix_accumulates_across_nesting() {
        let dir = tempfile::TempDir::new().unwrap();
        let root = write_yaml(
            dir.path(),
            "west.yml",
            r#"
manifest:
  self:
    import:
      file: middle.yml
      path-prefix: outer
  projects: []
"#,
        );
        write_yaml(
            dir.path(),
            "middle.yml",
            r#"
manifest:
  self:
    import:
      file: inner.yml
      path-prefix: inner
  projects: []
"#,
        );
        write_yaml(
            dir.path(),
            "inner.yml",
            r#"
manifest:
  projects:
    - name: deep
      url: https://example.com/deep
"#,
        );
        let source = StaticImportSource::new();
        let m = Manifest::from_path_with_imports(&root, dir.path(), &source).unwrap();
        let p = m.project("deep").unwrap();
        assert_eq!(p.path, PathBuf::from("outer/inner/deep"));
    }
}
