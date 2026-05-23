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

use std::collections::{BTreeSet, HashMap, HashSet};
use std::ffi::OsStr;
use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};

use garde::Validate;
use path_clean::PathClean;
use serde::{Deserialize, Deserializer};

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
    /// Workspace-side consumers want the resolved value; for the literal as
    /// it appeared in the manifest (or `None` when omitted) use [`path_raw`].
    ///
    /// [`path_raw`]: ManifestRepo::path_raw
    pub path: PathBuf,
    /// `manifest.self.path` exactly as written. `None` when the key was
    /// absent — distinct from `Some("manifest")`. Surfaced because the
    /// python contract uses this to populate `Manifest.path_raw` and to
    /// decide whether the synthetic `ManifestProject` has a path at all.
    pub path_raw: Option<PathBuf>,
    /// Relative paths to west-commands YAML files inside the manifest repo.
    pub west_commands: Vec<PathBuf>,
    /// Opaque payload carried verbatim from `manifest.self.userdata`. West
    /// itself does not interpret it; extensions read it via the manifest API.
    pub userdata: Option<serde_json::Value>,
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
    /// Opaque payload carried verbatim from the project's `userdata:` key.
    /// Free-form by design — any YAML/TOML/JSON value.
    pub userdata: Option<serde_json::Value>,
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
/// silently skipped — one missing imported file shouldn't break the
/// rest of resolution. Returning `Err` means "the source operation
/// itself failed"; the resolver emits a `log::warn!(target:
/// "west.manifest", …)` and continues with the rest of the projects,
/// surfacing the failure once at the end via
/// [`ManifestError::ImportSourceFailed`] only if the resolver aborts.
/// What an [`ImportSource`] returns for a per-project import.
///
/// `Single` is a one-file body (the common case, when the project's
/// `import:` names a single file). `Multiple` is a sorted list of
/// `(filename, body)` pairs, used by the directory form
/// (`import: <dir>` where `<dir>` lives in the project at
/// `manifest-rev`). The resolver absorbs each entry as a separate
/// sub-manifest in the given order; the filename selects the parser
/// per entry, so YAML, TOML, and JSON sub-manifests can coexist in a
/// single import directory.
///
/// Matches v1's `_manifest_content_at` return shape (`str | list[str]`),
/// extended with filenames because the rust port supports multiple
/// manifest formats — v1 was YAML-only and so could fold filenames
/// out of the API.
#[derive(Debug, Clone)]
pub enum ImportContent {
    Single(String),
    Multiple(Vec<NamedBody>),
}

/// One entry in [`ImportContent::Multiple`] — a sub-manifest body
/// paired with the filename it was read from. The filename's extension
/// (`.yml`/`.yaml`/`.toml`/`.json`) selects the parser; an empty name
/// or missing extension defaults to YAML (matches the
/// `west.yml`-as-default convention).
#[derive(Debug, Clone)]
pub struct NamedBody {
    pub name: String,
    pub body: String,
}

pub trait ImportSource {
    fn project_manifest(
        &self,
        project: &Project,
        relative_file: &str,
    ) -> Result<Option<ImportContent>, ImportSourceError>;

    /// Where this project's working tree lives on disk, used by the
    /// resolver to anchor *filesystem*-style imports (`self.import:
    /// <dir>/`, top-level `import: <file>`) that appear inside the
    /// manifest body returned by [`Self::project_manifest`].
    ///
    /// The default returns `None`, in which case the resolver falls
    /// back to the outer manifest repo root for nested filesystem
    /// imports — fine for in-memory test sources, wrong for any
    /// source that materializes projects on disk. Implementations
    /// like `WorkspaceImportSource` / `ReadOnlyImportSource` override
    /// to return `Some(workspace.join(&project.path))`.
    fn project_root(&self, project: &Project) -> Option<PathBuf> {
        let _ = project;
        None
    }
}

/// An `ImportSource` that always reports "this import is unavailable".
/// Used internally as the no-source default by entry points that don't
/// take a caller-supplied source — the resolver still runs, but per-project
/// imports under [`SitePolicy::Resolve`] become no-ops (they call the source,
/// the source returns `Ok(None)`, the resolver moves on).
struct NoopImportSource;

impl ImportSource for NoopImportSource {
    fn project_manifest(
        &self,
        _project: &Project,
        _relative_file: &str,
    ) -> Result<Option<ImportContent>, ImportSourceError> {
        Ok(None)
    }
}

/// Opaque error type returned by [`ImportSource`] implementations. The
/// resolver only ever displays it (warning + skip on `Err`), so the
/// inner error is type-erased: any [`std::error::Error`] can be wrapped
/// via [`ImportSourceError::new`], and a free-form string message via
/// [`ImportSourceError::msg`]. Source chaining is preserved via the
/// transparent `#[error]` delegate.
#[derive(Debug, thiserror::Error)]
#[error(transparent)]
pub struct ImportSourceError(Box<dyn std::error::Error + Send + Sync>);

impl ImportSourceError {
    /// Wrap any concrete error implementation. The wrapped value is kept
    /// alive for the lifetime of the `ImportSourceError`, so callers can
    /// rely on `.source()` chaining.
    pub fn new<E>(e: E) -> Self
    where
        E: std::error::Error + Send + Sync + 'static,
    {
        Self(Box::new(e))
    }

    /// Wrap a free-form message — for I/O-with-context errors and other
    /// callsites that don't have an upstream `Error` value to forward.
    pub fn msg(s: impl Into<String>) -> Self {
        Self(Box::new(MessageError(s.into())))
    }
}

#[derive(Debug, thiserror::Error)]
#[error("{0}")]
struct MessageError(String);

/// Hard cap on import nesting depth — a chain longer than this returns
/// [`ManifestError::ImportTooDeep`] rather than blowing the stack.
pub const MAX_IMPORT_DEPTH: usize = 32;

// =====================================================================
// Errors
// =====================================================================

#[derive(Debug, thiserror::Error)]
pub enum ManifestError {
    #[error("YAML parse error: {0}")]
    Yaml(#[source] serde_saphyr::Error),
    #[error("TOML parse error: {0}")]
    Toml(#[source] toml_edit::de::Error),
    #[error("JSON parse error: {0}")]
    Json(#[source] serde_json::Error),
    #[error("unsupported manifest format: {0:?} (expected .yaml/.yml/.toml/.json)")]
    UnsupportedFormat(String),
    #[error("validation failed: {0}")]
    Validation(String),
    #[error("project {project:?}: remote {remote:?} is not defined")]
    UnknownRemote { project: String, remote: String },
    #[error("defaults.remote {remote:?} is not defined in remotes")]
    UnknownDefaultRemote { remote: String },
    #[error("duplicate project name: {0:?}")]
    DuplicateProjectName(String),
    #[error("duplicate project path: {0:?}")]
    DuplicateProjectPath(String),
    #[error("no project may be named \"manifest\" (reserved)")]
    ProjectNamedManifest,
    #[error("project {0:?}: no remote or url and no default remote is set")]
    NoUrl(String),
    #[error("project {project:?}: cannot specify both `url` and `remote`")]
    UrlAndRemote { project: String },
    #[error("project {project:?}: cannot specify both `url` and `repo-path`")]
    UrlAndRepoPath { project: String },
    #[error(
        "project {project:?}: invalid group {group:?} \
         (must not be empty, contain whitespace/comma/colon, or start with `+`/`-`)"
    )]
    InvalidGroup { project: String, group: String },
    #[error("project {project:?}: \"groups\" cannot be combined with \"import\"")]
    GroupsWithImport { project: String },
    #[error("{origin} group filter contains invalid item {item:?}; {reason}")]
    InvalidGroupFilter {
        origin: String,
        item: String,
        reason: String,
    },
    #[error("\"manifest: group-filter: []\" may not be empty")]
    EmptyGroupFilter,
    #[error("project {project:?} has absolute path {path:?}; must be relative to the workspace")]
    AbsoluteProjectPath { project: String, path: String },
    #[error("project {project:?} has path {path:?} that escapes the workspace topdir")]
    EscapingProjectPath { project: String, path: String },
    #[error("project {project:?} has reserved path {path:?} (the .west directory \
             and its subdirectories are reserved for workspace metadata)")]
    ReservedProjectPath { project: String, path: String },
    /// Retired: emitted by the strict policy on legacy callers, but kept
    /// in the enum so external `match` arms don't break. New code should
    /// use [`Manifest::from_path_with`] with a source for resolution or
    /// [`Manifest::from_path_lenient`] to skip imports.
    #[error("manifest imports are not supported (found in {context})")]
    ImportNotSupported { context: String },
    /// An import directive (self/top-level/per-project) cycles back to a
    /// file or project that's already on the resolution stack.
    #[error("{kind} import cycle detected: {target:?}")]
    ImportLoop { kind: ImportSite, target: String },
    /// Nested imports exceeded [`MAX_IMPORT_DEPTH`].
    #[error("manifest imports nested too deeply (limit: {limit})")]
    ImportTooDeep { limit: usize },
    /// An [`ImportSource`] callback failed for a per-project import. The
    /// resolver promotes a non-skipped error from the source into this so
    /// callers can distinguish "the source itself failed" from "the
    /// imported file isn't there."
    #[error("import source failed for project {project:?}: {detail}")]
    ImportSourceFailed { project: String, detail: String },
    /// `resolve_projects` was given a selector that matches no project (by
    /// name or by path).
    #[error("unknown project name or path: {0:?}")]
    UnknownProject(String),
    #[error("io error on {}: {source}", path.display())]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
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
    group_filter: Option<Vec<String>>,
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
    /// Double-`Option` so the schema can distinguish three states:
    /// `None` (key absent → no path; resolved default applies),
    /// `Some(None)` (key present but `null` / empty scalar → rejected),
    /// `Some(Some(s))` (key present with a real value). Emptiness is
    /// enforced post-parse in `build_self`; the wording is part of the
    /// public API so it lives there rather than in a garde message.
    #[garde(skip)]
    #[serde(default, deserialize_with = "deserialize_optional_path")]
    path: Option<Option<String>>,
    #[garde(skip)]
    #[serde(rename = "west-commands", default)]
    west_commands: Option<OneOrMany<String>>,
    #[garde(skip)]
    #[serde(rename = "import", default)]
    import: Option<ImportSchema>,
    #[garde(skip)]
    #[serde(default)]
    userdata: Option<serde_json::Value>,
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
    /// Per-project `west-commands` is a scalar string only (unlike
    /// the `self.west-commands` field, which also accepts a list).
    /// Legacy v1 rejects the list form here even with a single entry —
    /// see `tests/manifests/invalid_west_commands_2.yml`.
    #[garde(skip)]
    #[serde(rename = "west-commands", default)]
    west_commands: Option<String>,
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
    /// Opaque payload passed through to `Project::userdata`. Free-form by
    /// spec — no schema constraints beyond "it's a YAML/TOML/JSON value".
    #[garde(skip)]
    #[serde(default)]
    userdata: Option<serde_json::Value>,
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
/// Deserialization is hand-rolled (rather than `#[serde(untagged)]`)
/// to keep the per-variant error from being collapsed into "data did
/// not match any variant" — once we know the value is an object, we
/// delegate to `ImportMap`'s derived `Deserialize` so serde's
/// `deny_unknown_fields` message (which lists the valid keys) makes
/// it through to the user.
#[derive(Debug, Clone)]
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

impl<'de> Deserialize<'de> for ImportSchema {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        use serde::de::Error;
        // Route through `serde_json::Value` so we can branch on the
        // shape and call the right per-variant deserializer with its
        // own error message. One extra allocation per `import:` —
        // fine, imports parse once at load.
        let value = serde_json::Value::deserialize(deserializer)?;
        match value {
            serde_json::Value::Bool(b) => Ok(ImportSchema::Bool(b)),
            serde_json::Value::String(s) => Ok(ImportSchema::Str(s)),
            serde_json::Value::Array(_) => {
                serde_json::from_value(value).map(ImportSchema::List).map_err(D::Error::custom)
            }
            serde_json::Value::Object(_) => {
                // ImportMap's derived `deny_unknown_fields` reports
                // unknown keys with the list of valid ones; each field
                // type produces serde's natural "invalid type" error
                // when wrong-shaped.
                serde_json::from_value(value).map(ImportSchema::Map).map_err(D::Error::custom)
            }
            other => Err(D::Error::custom(format!(
                "invalid `import:` value: expected bool, string, list, or map; got {}",
                json_type_name(&other),
            ))),
        }
    }
}

fn json_type_name(v: &serde_json::Value) -> &'static str {
    match v {
        serde_json::Value::Null => "null",
        serde_json::Value::Bool(_) => "bool",
        serde_json::Value::Number(_) => "number",
        serde_json::Value::String(_) => "string",
        serde_json::Value::Array(_) => "list",
        serde_json::Value::Object(_) => "map",
    }
}

/// Dict form of [`ImportSchema`]. All list fields also accept a single
/// string for ergonomics; `OneOrMany` handles the deserialization.
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

#[derive(Debug, Clone)]
enum OneOrMany<T> {
    One(T),
    Many(Vec<T>),
}

impl<'de, T: Deserialize<'de>> Deserialize<'de> for OneOrMany<T> {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        use std::fmt;
        use std::marker::PhantomData;

        use serde::de::{IntoDeserializer, MapAccess, SeqAccess, Visitor};

        // Custom Visitor (rather than `#[serde(untagged)]`) so the
        // outer deserializer's field-path context propagates through
        // a wrong-shape error: serde's untagged collapses every
        // per-variant failure into "data did not match any variant"
        // and discards the surrounding context, leaving callers
        // staring at an unattributed message.
        struct OneOrManyVisitor<T>(PhantomData<T>);

        impl<'de, T: Deserialize<'de>> Visitor<'de> for OneOrManyVisitor<T> {
            type Value = OneOrMany<T>;

            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("a value or a list of values")
            }

            // Scalars / single-value forms — forward to T's own
            // deserializer via the `IntoDeserializer` helpers so any
            // type T (String, struct, …) round-trips correctly.
            fn visit_bool<E: serde::de::Error>(self, v: bool) -> Result<Self::Value, E> {
                T::deserialize(v.into_deserializer()).map(OneOrMany::One)
            }
            fn visit_i64<E: serde::de::Error>(self, v: i64) -> Result<Self::Value, E> {
                T::deserialize(v.into_deserializer()).map(OneOrMany::One)
            }
            fn visit_u64<E: serde::de::Error>(self, v: u64) -> Result<Self::Value, E> {
                T::deserialize(v.into_deserializer()).map(OneOrMany::One)
            }
            fn visit_f64<E: serde::de::Error>(self, v: f64) -> Result<Self::Value, E> {
                T::deserialize(v.into_deserializer()).map(OneOrMany::One)
            }
            fn visit_str<E: serde::de::Error>(self, v: &str) -> Result<Self::Value, E> {
                T::deserialize(v.into_deserializer()).map(OneOrMany::One)
            }
            fn visit_string<E: serde::de::Error>(self, v: String) -> Result<Self::Value, E> {
                T::deserialize(v.into_deserializer()).map(OneOrMany::One)
            }

            fn visit_seq<S>(self, seq: S) -> Result<Self::Value, S::Error>
            where
                S: SeqAccess<'de>,
            {
                let items = Vec::<T>::deserialize(serde::de::value::SeqAccessDeserializer::new(seq))?;
                Ok(OneOrMany::Many(items))
            }

            fn visit_map<M>(self, map: M) -> Result<Self::Value, M::Error>
            where
                M: MapAccess<'de>,
            {
                // T might itself be a map-shaped type (e.g. the
                // submodule schema). Try deserializing the map as T
                // first so legitimate map-shaped Ts still work; only
                // re-emit a OneOrMany-flavoured error if T rejects.
                T::deserialize(serde::de::value::MapAccessDeserializer::new(map))
                    .map(OneOrMany::One)
                    .map_err(|_| {
                        serde::de::Error::invalid_type(
                            serde::de::Unexpected::Map,
                            &"a value or a list of values",
                        )
                    })
            }
        }

        deserializer.deserialize_any(OneOrManyVisitor(PhantomData))
    }
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
// Resolution rules:
//
// - Cycle/depth guards are explicit: a per-site visited-set plus a
//   `MAX_IMPORT_DEPTH` cap, instead of relying on stack overflow.
// - Resolution order: each manifest file processes its own
//   directly-defined projects first, then walks self/top-level imports,
//   then per-project imports for projects that have them. First-wins
//   means parent-defined projects beat imported ones with the same name.

const MANIFEST_DEFAULT_FILE: &str = "west.yml";

/// Per-import filter (allowlist/blocklist of project names + paths). The
/// resolver carries a composed filter through the recursion — parent and
/// child rules combine: allowlists narrow (a project must be in both),
/// blocklists union (any block on the chain rejects).
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
    // Empty = no constraint, so an empty side returns the other; both
    // empty stays empty; both populated takes the union (the gate
    // predicate is "in either list").
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
    /// Filesystem base for the manifest currently being absorbed.
    /// Starts as the workspace manifest's repo root; swapped to a
    /// project's working-tree root inside
    /// [`Self::absorb_project_import`] so that a project-imported
    /// manifest's nested `self.import: <dir>/` resolves against the
    /// importing project, not the outer manifest repo. Restored on
    /// the way out.
    current_repo_root: PathBuf,
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
    /// Per-site policy applied uniformly at every nesting level. Recursive
    /// `absorb` invocations inherit it unchanged so a `PROJECTS_ONLY` root
    /// also silently drops nested filesystem imports inside a project-imported
    /// manifest body.
    policy: ImportPolicy,
}

impl<'a> Resolver<'a> {
    fn new(repo_root: &'a Path, source: &'a dyn ImportSource, policy: ImportPolicy) -> Self {
        Self {
            current_repo_root: repo_root.to_path_buf(),
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
            policy,
        }
    }

    /// Process the root manifest file. Records `self`/`version` (which
    /// imports do not propagate) and recursively walks all imports.
    fn absorb_root(&mut self, file: ManifestFile) -> Result<(), ManifestError> {
        self.version = file.manifest.version.clone();
        let self_section = file.manifest.self_.clone();
        self.self_ = Some(build_self(self_section)?);
        self.absorb(
            file,
            ImportFilter::default(),
            PathBuf::new(),
            &HashSet::new(),
        )
    }

    fn absorb(
        &mut self,
        file: ManifestFile,
        filter: ImportFilter,
        path_prefix: PathBuf,
        parent_skip: &HashSet<String>,
    ) -> Result<(), ManifestError> {
        if self.depth > MAX_IMPORT_DEPTH {
            return Err(ManifestError::ImportTooDeep {
                limit: MAX_IMPORT_DEPTH,
            });
        }

        let m = file.manifest;

        // Per-file project-name uniqueness. Duplicates *within* a single
        // manifest file are an error — there is no first-wins semantic
        // for "the same file twice". Across files (root + imports) the
        // global `seen_names` set in phase B below drops dupes
        // first-wins, which is the intended import behavior.
        let mut this_file_names: HashSet<&str> = HashSet::new();
        for ps in &m.projects {
            if !this_file_names.insert(ps.name.as_str()) {
                return Err(ManifestError::DuplicateProjectName(ps.name.clone()));
            }
        }

        let remotes: HashMap<String, &RemoteSchema> =
            m.remotes.iter().map(|r| (r.name.clone(), r)).collect();
        let defaults = m.defaults.as_ref();

        // If `defaults.remote:` names a remote, that remote must exist in
        // the same file's `remotes:` list — even when no project actually
        // resolves through it. Catches typos that would otherwise lurk
        // until someone added a project relying on the default.
        if let Some(d) = defaults
            && let Some(name) = &d.remote
            && !remotes.contains_key(name)
        {
            return Err(ManifestError::UnknownDefaultRemote {
                remote: name.clone(),
            });
        }

        // v1 ordering / precedence rules for `self.import:` and the
        // top-level `manifest.import:`:
        //   - Output order: imported projects appear *before* the
        //     locally-defined ones in the final list (v1's docs frame
        //     local definitions as additions on top of imports).
        //   - Precedence on name conflict: the *importing* file's
        //     locally-defined project wins; the import's same-named
        //     project is silently dropped.
        //
        // To get both: process imports first (phase A), then push
        // locals (phase B), and thread a `skip` set down to the
        // recursive imports so the inner phase B knows which names the
        // outer file will claim.
        let my_locals: HashSet<String> =
            m.projects.iter().map(|p| p.name.clone()).collect();
        let mut child_skip: HashSet<String> = parent_skip.clone();
        child_skip.extend(my_locals.iter().cloned());

        // Phase A: self / top-level imports (filesystem). Recursive
        // absorbs see `child_skip` so any name this file (or one of
        // its ancestors) will define locally is skipped on the import
        // side.
        if let Some(self_section) = &m.self_
            && let Some(import) = &self_section.import
        {
            reject_bool_import(import, "self")?;
            let imaps = flatten_imports(import);
            if !imaps.is_empty()
                && self.dispatch_resolving_policy(
                    self.policy.self_repo,
                    "self",
                    "manifest `self.import:` is unsupported and will be ignored",
                )?
            {
                for imap in imaps {
                    self.absorb_filesystem_import(
                        &imap,
                        &filter,
                        &path_prefix,
                        ImportSite::SelfRepo,
                        &child_skip,
                    )?;
                }
            }
        }
        if let Some(import) = &m.import {
            reject_bool_import(import, "top-level")?;
            let imaps = flatten_imports(import);
            if !imaps.is_empty()
                && self.dispatch_resolving_policy(
                    self.policy.top_level,
                    "top-level",
                    "manifest top-level `import:` is unsupported and will be ignored; \
                     projects pulled in by the import will not be updated",
                )?
            {
                for imap in imaps {
                    self.absorb_filesystem_import(
                        &imap,
                        &filter,
                        &path_prefix,
                        ImportSite::TopLevel,
                        &child_skip,
                    )?;
                }
            }
        }

        // Phase A.5: this file's own `self.west-commands:` lands now,
        // after its imports' contributions, so the final list is
        // ordered deepest-first with the root manifest's commands at
        // the tail (v1 "imports come before locals" applied to self-
        // block extension scripts).
        self.append_own_self_west_commands(&m);

        // Phase A.5 (cont.): same idea for group-filter — append
        // this file's own entries now that any self/top-level
        // imports have already appended theirs, with Phase C project
        // imports still to come. `into_manifest` walks the resulting
        // vec in reverse to simplify, so this push order produces
        // v1's apply sequence: project imports first (lowest
        // precedence), then own, then self/top-level imports last
        // (highest precedence wins). Reject explicit `[]` here too,
        // matching v1's `_validated_group_filter`.
        if let Some(gf) = &m.group_filter {
            if gf.is_empty() {
                return Err(ManifestError::EmptyGroupFilter);
            }
            self.group_filter_strs.extend(gf.iter().cloned());
        }

        // Phase B: this file's directly-defined projects. Skip any
        // name reserved by an outer scope (v1 precedence: outermost
        // wins). The seen_names check below also catches the case
        // where phase A pushed a same-named import that wasn't
        // covered by `parent_skip` for some reason — defensive.
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
            // v1: a project may not declare both `groups:` and a
            // truthy `import:`. `import: false` is falsy and stays
            // benign, matching the legacy `if imp and groups:` check.
            let has_active_import = ps
                .import
                .as_ref()
                .is_some_and(|i| !matches!(i, ImportSchema::Bool(false)));
            if has_active_import && !ps.groups.is_empty() {
                return Err(ManifestError::GroupsWithImport {
                    project: ps.name.clone(),
                });
            }
            let project = resolve_project(ps.clone(), &remotes, defaults)?;
            // A project that carries `import: { path-prefix: X }` has
            // its own path prefixed with `X` too, so it sits alongside
            // the projects pulled in from its imported body (which
            // pick up the same prefix when `absorb_project_import`
            // composes it onto `path_prefix`). For list-form imports
            // we use the first map's prefix — multiple prefixes on
            // one project's `import:` is ill-defined.
            let own_prefix = ps
                .import
                .as_ref()
                .and_then(|s| flatten_imports(s).into_iter().find_map(|m| m.path_prefix))
                .map(PathBuf::from)
                .unwrap_or_default();
            let effective_prefix = match (
                path_prefix.as_os_str().is_empty(),
                own_prefix.as_os_str().is_empty(),
            ) {
                (true, true) => PathBuf::new(),
                (false, true) => path_prefix.clone(),
                (true, false) => own_prefix,
                (false, false) => path_prefix.join(&own_prefix),
            };
            let prefixed_path = if effective_prefix.as_os_str().is_empty() {
                project.path.clone()
            } else {
                effective_prefix.join(&project.path)
            };
            let mut project = project;
            // Canonicalize the stored path so `{path}` rendering, the
            // selector-by-path lookup, and any downstream consumer all
            // see a single normal form. Collapses `subdir///foo`,
            // `subdir/./foo`, and `subdir/inner/../foo` to their
            // obvious equivalents; backslashes inside a segment
            // (legal filename chars on POSIX) are preserved.
            project.path = prefixed_path.clean();
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
            if relative_path_escapes_root(&project.path) {
                return Err(ManifestError::EscapingProjectPath {
                    project: project.name.clone(),
                    path: path_str,
                });
            }
            if path_collides_with_west_dir(&project.path) {
                return Err(ManifestError::ReservedProjectPath {
                    project: project.name.clone(),
                    path: path_str,
                });
            }

            if !filter.allows(&project) {
                continue;
            }
            if let Some(reason) = name_already_claimed(&project.name, &self.seen_names, parent_skip)
            {
                log::debug!(
                    "manifest import: dropping duplicate project {:?} ({reason})",
                    project.name,
                );
                continue;
            }
            if !self.seen_paths.insert(path_str.clone()) {
                return Err(ManifestError::DuplicateProjectPath(path_str));
            }
            self.seen_names.insert(project.name.clone());
            self.projects.push(project);
        }

        // Phase 3: per-project imports. We iterate the same projects again
        // because a project's import is "after this project is fetched";
        // skipping filtered-out projects is correct (no project ⇒ no
        // import to chase).
        for ps in &m.projects {
            let Some(import) = &ps.import else { continue };
            let imaps = flatten_imports(import);
            if imaps.is_empty() {
                continue; // `import: false` or an empty list — no-op.
            }
            if !self.dispatch_resolving_policy(
                self.policy.per_project,
                &format!("project {:?}", ps.name),
                &format!(
                    "project {:?}: `import:` is unsupported and will be ignored; \
                     projects from {:?}'s manifest will not be updated",
                    ps.name, ps.name
                ),
            )? {
                continue;
            }
            // Find the resolved Project (may be absent if filter dropped it).
            let project_clone = self.projects.iter().find(|p| p.name == ps.name).cloned();
            let Some(project) = project_clone else {
                continue;
            };
            for imap in imaps {
                self.absorb_project_import(&project, &imap, &filter, &path_prefix)?;
            }
        }

        Ok(())
    }

    /// Dispatch a [`SitePolicy`] on the resolving path. Returns `Ok(true)`
    /// when the resolver should proceed with the import, `Ok(false)` when
    /// the site is skipped (silently or after a warning), or an error for
    /// `SitePolicy::Error`. The warning lands on `log::warn!(target:
    /// "west.manifest", …)`; pyo3-log routes it to python's
    /// `logging.getLogger("west.manifest")` and the CLI binary's
    /// env_logger emits it with the standard prefix.
    fn dispatch_resolving_policy(
        &self,
        policy: SitePolicy,
        context: &str,
        warn_message: &str,
    ) -> Result<bool, ManifestError> {
        match policy {
            SitePolicy::Resolve => Ok(true),
            SitePolicy::Skip => Ok(false),
            SitePolicy::WarnAndStrip => {
                log::warn!(target: "west.manifest", "{warn_message}");
                Ok(false)
            }
            SitePolicy::Error => Err(ManifestError::ImportNotSupported {
                context: context.to_owned(),
            }),
        }
    }

    fn absorb_filesystem_import(
        &mut self,
        imap: &ImportMap,
        parent_filter: &ImportFilter,
        parent_prefix: &Path,
        site: ImportSite,
        parent_skip: &HashSet<String>,
    ) -> Result<(), ManifestError> {
        let file_name = imap
            .file
            .clone()
            .unwrap_or_else(|| MANIFEST_DEFAULT_FILE.to_owned());
        // Resolve against the manifest currently being absorbed —
        // for the root manifest this equals `self.repo_root`; for a
        // project-imported manifest body this is the project's
        // working-tree root (set by `absorb_project_import` via
        // `ImportSource::project_root`).
        let abs_path = self.current_repo_root.join(&file_name);

        // A missing path here means the manifest author named a
        // file/directory that isn't on disk; surface it as a
        // validation error (which maps to `MalformedManifest` on the
        // python side) rather than a raw IO error from the read
        // attempt downstream. Keeps the diagnostic specific without
        // poking at io::ErrorKind in `absorb_one_file`.
        if !abs_path.exists() {
            return Err(ManifestError::Validation(format!(
                "manifest.{site}.import: file not found: {}",
                abs_path.display(),
            )));
        }

        // Compose filter and prefix once at this level so a directory
        // form's per-file recursions all see the same constraints.
        let composed_filter = ImportFilter::compose(parent_filter, &ImportFilter::from_map(imap));
        let composed_prefix = match imap.path_prefix.as_deref() {
            None | Some("") => parent_prefix.to_path_buf(),
            Some(p) => parent_prefix.join(p),
        };

        // Directory form: iterate manifest files in sorted order and
        // absorb each as if it were listed explicitly. Accepted
        // extensions match the single-file form (`parse_body_by_extension`):
        // `yml`, `yaml`, `toml`, `json`. The directory itself isn't
        // tracked in visited_files — only the leaves are, so the cycle
        // guard still works.
        if abs_path.is_dir() {
            let mut entries: Vec<PathBuf> = fs::read_dir(&abs_path)
                .map_err(|e| ManifestError::Io {
                    path: abs_path.clone(),
                    source: e,
                })?
                .filter_map(|r| r.ok())
                .map(|e| e.path())
                .filter(|p| p.is_file())
                .filter(|p| {
                    matches!(
                        p.extension().and_then(OsStr::to_str),
                        Some("yml") | Some("yaml") | Some("toml") | Some("json")
                    )
                })
                .collect();
            entries.sort();
            for path in entries {
                self.absorb_one_file(
                    &path,
                    &composed_filter,
                    &composed_prefix,
                    site,
                    parent_skip,
                )?;
            }
            return Ok(());
        }

        self.absorb_one_file(
            &abs_path,
            &composed_filter,
            &composed_prefix,
            site,
            parent_skip,
        )
    }

    fn absorb_one_file(
        &mut self,
        abs_path: &Path,
        filter: &ImportFilter,
        prefix: &Path,
        site: ImportSite,
        parent_skip: &HashSet<String>,
    ) -> Result<(), ManifestError> {
        let canonical = abs_path
            .canonicalize()
            .unwrap_or_else(|_| abs_path.to_path_buf());
        if !self.visited_files.insert(canonical.clone()) {
            return Err(ManifestError::ImportLoop {
                kind: site,
                target: abs_path.display().to_string(),
            });
        }

        let body = fs::read_to_string(abs_path).map_err(|e| ManifestError::Io {
            path: abs_path.to_path_buf(),
            source: e,
        })?;
        let parsed = parse_body_by_extension(abs_path, &body)?;
        parsed
            .validate()
            .map_err(|r| ManifestError::Validation(r.to_string()))?;

        self.depth += 1;
        let res = self.absorb(parsed, filter.clone(), prefix.to_path_buf(), parent_skip);
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
        let content = match self.source.project_manifest(project, &file) {
            Ok(Some(c)) => c,
            Ok(None) => {
                self.visited_projects.remove(&project.name);
                return Ok(());
            }
            Err(e) => {
                log::warn!(
                    target: "west.manifest",
                    "project {:?} import failed: {e}; skipping",
                    project.name,
                );
                self.visited_projects.remove(&project.name);
                return Ok(());
            }
        };

        let composed = ImportFilter::compose(parent_filter, &ImportFilter::from_map(imap));
        let prefix = match imap.path_prefix.as_deref() {
            None | Some("") => parent_prefix.to_path_buf(),
            Some(p) => parent_prefix.join(p),
        };

        // Anchor filesystem imports inside the project-imported body
        // to the project's own working tree. The save/swap/restore
        // dance lets nested project-imports below this point continue
        // to use *their* project root rather than the outer manifest
        // repo root. If the source can't provide a root (in-memory
        // test sources), keep the outer root — matches the existing
        // resolver behaviour and the trait's documented fallback.
        let saved_repo_root = self.current_repo_root.clone();
        if let Some(root) = self.source.project_root(project) {
            self.current_repo_root = root;
        }

        // Fold Single into a 1-element batch so both branches go through
        // the same loop. `Single` carries the original `import:` path
        // as the parser-dispatch hint; `Multiple` already has per-entry
        // filenames from the directory expansion.
        let entries: Vec<NamedBody> = match content {
            ImportContent::Single(body) => vec![NamedBody {
                name: file.clone(),
                body,
            }],
            ImportContent::Multiple(entries) => entries,
        };

        let mut res: Result<(), ManifestError> = Ok(());
        for entry in entries {
            if let Err(e) = self.absorb_imported_submanifest(
                &entry.name,
                &entry.body,
                &project.name,
                &composed,
                &prefix,
            ) {
                res = Err(e);
                break;
            }
        }

        self.current_repo_root = saved_repo_root;
        self.visited_projects.remove(&project.name);
        res
    }

    /// Parse, inherit, then absorb one sub-manifest pulled in by
    /// `importing_project`'s per-project `import:` directive. `name`
    /// is the filename used both for parser dispatch (extension
    /// selects YAML/TOML/JSON) and for diagnostics.
    fn absorb_imported_submanifest(
        &mut self,
        name: &str,
        body: &str,
        importing_project: &str,
        filter: &ImportFilter,
        prefix: &Path,
    ) -> Result<(), ManifestError> {
        let parsed = parse_body_by_extension(Path::new(name), body)?;
        parsed
            .validate()
            .map_err(|r| ManifestError::Validation(r.to_string()))?;
        self.inherit_imported_west_commands(importing_project, &parsed);
        self.depth += 1;
        // Per-project imports fire in Phase 3, after the outer Phase
        // B already pushed locals — so `seen_names` covers the
        // precedence dedup. The outer-scope skip set used by
        // filesystem-anchored imports doesn't apply here.
        let res = self.absorb(
            parsed,
            filter.clone(),
            prefix.to_path_buf(),
            &HashSet::new(),
        );
        self.depth -= 1;
        res
    }

    /// Append any `self.west-commands:` declared in an imported
    /// sub-manifest onto the importing project's own `west_commands`.
    /// v1 contract: a project that imports a sub-manifest inherits
    /// the sub-manifest's extension scripts even if it has none of
    /// its own. Multiple imports compose in absorption order; no-ops
    /// silently when the imported file has no `self:` block or the
    /// project was filter-dropped before the resolver got here.
    fn inherit_imported_west_commands(&mut self, into: &str, imported: &ManifestFile) {
        let Some(self_section) = &imported.manifest.self_ else {
            return;
        };
        let Some(wc) = &self_section.west_commands else {
            return;
        };
        if let Some(p) = self.projects.iter_mut().find(|p| p.name == into) {
            p.west_commands
                .extend(wc.to_vec().into_iter().map(PathBuf::from));
        }
    }

    /// Append the current file's own `self.west-commands:` onto the
    /// resolver's accumulated self block. Called from `absorb` after
    /// Phase A imports return, so deeper sub-manifests' commands land
    /// before the importing file's own (v1 ordering).
    fn append_own_self_west_commands(&mut self, m: &ManifestSection) {
        let Some(self_section) = &m.self_ else {
            return;
        };
        let Some(wc) = &self_section.west_commands else {
            return;
        };
        let Some(target) = self.self_.as_mut() else {
            return;
        };
        target
            .west_commands
            .extend(wc.to_vec().into_iter().map(PathBuf::from));
    }

    fn into_manifest(self) -> Result<Manifest, ManifestError> {
        // Validate every accumulated entry, then simplify by walking
        // the accumulator in reverse (lowest-precedence-first per
        // v1's apply order — see the Phase A.5 push site) and
        // updating a disabled-groups set: `-X` adds, `+X` removes.
        // The final `Manifest.group_filter` exposes only the disabled
        // groups in sorted order — v1's v0.10 contract.
        let entries = parse_group_filter(&self.group_filter_strs, "manifest")?;
        let mut disabled: BTreeSet<String> = BTreeSet::new();
        for e in entries.iter().rev() {
            if e.disabled {
                disabled.insert(e.group.clone());
            } else {
                disabled.remove(&e.group);
            }
        }
        let group_filter: Vec<GroupFilterEntry> = disabled
            .into_iter()
            .map(|group| GroupFilterEntry {
                group,
                disabled: true,
            })
            .collect();
        Ok(Manifest {
            version: self.version,
            self_: self.self_.unwrap_or_default(),
            projects: self.projects,
            group_filter,
        })
    }
}

/// Returns a debug-log reason when `name` is already claimed by some
/// other scope so the caller should drop the candidate project:
///
/// - `"outer-scope first-wins"` — an outer (caller) absorb's locals
///   set has reserved the name; the outer hasn't pushed yet but will,
///   and v1's precedence rule is that the outermost-defining manifest
///   wins on a name conflict.
/// - `"first-wins"` — some earlier phase or recursion has already
///   pushed this name to `seen_names`; later attempts are dropped.
///
/// Returns `None` when the name is free to push.
fn name_already_claimed(
    name: &str,
    seen_names: &HashSet<String>,
    parent_skip: &HashSet<String>,
) -> Option<&'static str> {
    if parent_skip.contains(name) {
        Some("outer-scope first-wins")
    } else if seen_names.contains(name) {
        Some("first-wins")
    } else {
        None
    }
}

/// Reject `import: true` / `import: false` at filesystem-anchored
/// import sites (`self.import:` and the top-level `manifest.import:`).
/// Booleans are only meaningful for per-project imports, where
/// `import: true` is sugar for "look at this project's
/// `west.yml`" — there's no analog for self / top-level, which are
/// already anchored to a known file. v1 surfaced this as a parse-time
/// error mentioning the value's type; mirroring that here keeps the
/// diagnostic specific instead of bouncing through the resolver's
/// generic "imports unsupported" path.
/// Whether a relative `path` (possibly containing `..`) climbs above
/// its starting point. `.` is ignored; `..` past the start counts.
/// Absolute paths report as escapes too — callers can treat this as a
/// single "stays within root" predicate. The "root" is implicit: the
/// math is pure path-arithmetic with no workspace baked in.
///
/// Routes through [`path_clean::clean`], which collapses `.`/`..`
/// segments without touching the filesystem. A cleaned path that
/// starts with `..` means the original tried to back out past the
/// anchor; an absolute path stays absolute after cleaning.
fn relative_path_escapes_root(path: &Path) -> bool {
    if path.is_absolute() {
        return true;
    }
    path.clean().starts_with("..")
}

/// Returns true when `path`, after `..`/`.` collapsing, lands at the
/// workspace's reserved `.west` directory or any descendant of it.
/// Manifest YAML can spell the same destination in many ways
/// (`.west`, `.west/sub`, `foo/../.west`); collapsing first lets us
/// reject them all uniformly.
fn path_collides_with_west_dir(path: &Path) -> bool {
    path.clean()
        .components()
        .next()
        .is_some_and(|c| c.as_os_str() == OsStr::new(crate::WEST_DIR))
}

fn reject_bool_import(import: &ImportSchema, site: &'static str) -> Result<(), ManifestError> {
    if matches!(import, ImportSchema::Bool(_)) {
        return Err(ManifestError::Validation(format!(
            "manifest.{site}.import: invalid import type of boolean \
             (expected a file path, a list of paths, or a map)"
        )));
    }
    Ok(())
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
    // ---- Simple convenience entries (STRICT policy, no source) ----------
    //
    // These are equivalent to calling `from_*_with(..., None, ImportPolicy::STRICT)`
    // and exist so the common "just parse this thing, no imports" call stays
    // a one-liner.

    pub fn from_yaml_str(s: &str) -> Result<Self, ManifestError> {
        Self::from_yaml_str_with(s, None, ImportPolicy::STRICT)
    }

    pub fn from_toml_str(s: &str) -> Result<Self, ManifestError> {
        Self::from_toml_str_with(s, None, ImportPolicy::STRICT)
    }

    pub fn from_json_str(s: &str) -> Result<Self, ManifestError> {
        Self::from_json_str_with(s, None, ImportPolicy::STRICT)
    }

    /// Construct from an already-parsed [`serde_json::Value`].
    ///
    /// Like [`Manifest::from_json_str`] but skips the textual
    /// parse step — useful when the caller produced the
    /// `Value` from a non-string source (the python binding
    /// passes a `dict` straight in as a `Value` via PyO3's
    /// `from_pyobject` path).
    pub fn from_value(value: serde_json::Value) -> Result<Self, ManifestError> {
        Self::from_value_with(value, None, ImportPolicy::STRICT)
    }

    /// Sniff `.yaml` / `.yml` / `.toml` / `.json` from the path's extension.
    pub fn from_path(path: &Path) -> Result<Self, ManifestError> {
        Self::from_path_with(path, None, None, ImportPolicy::STRICT)
    }

    /// Like [`Manifest::from_path`] but treats `import:` directives as a
    /// **warning** rather than an error: the directly-defined projects are
    /// still returned, with a `log::warn!` noting that imported entries
    /// will not be resolved. Useful for commands like `west update` that
    /// can do something useful with the locally-defined projects even
    /// while full import resolution remains unimplemented.
    pub fn from_path_lenient(path: &Path) -> Result<Self, ManifestError> {
        Self::from_path_with(path, None, None, ImportPolicy::WARN_AND_STRIP)
    }

    // ---- Full-control entries (per format) ------------------------------
    //
    // Each `_with` takes an optional [`ImportSource`] (the per-project
    // import callback) and a per-site [`ImportPolicy`]. `source` is only
    // consulted for sites whose policy is `SitePolicy::Resolve` — when no
    // source is provided, a private `NoopImportSource` returns `Ok(None)`
    // for every callback invocation. The four documented policy presets
    // (`STRICT`, `WARN_AND_STRIP`, `IGNORE_ALL`, `SKIP_PROJECTS`,
    // `PROJECTS_ONLY`, `RESOLVE_ALL`) cover the python `ImportFlag` values
    // plus the validation defaults.

    pub fn from_yaml_str_with(
        s: &str,
        source: Option<&dyn ImportSource>,
        policy: ImportPolicy,
    ) -> Result<Self, ManifestError> {
        let file: ManifestFile = serde_saphyr::from_str(s).map_err(ManifestError::Yaml)?;
        resolve_file(file, Path::new(""), source, policy, None)
    }

    pub fn from_toml_str_with(
        s: &str,
        source: Option<&dyn ImportSource>,
        policy: ImportPolicy,
    ) -> Result<Self, ManifestError> {
        let file: ManifestFile = toml_edit::de::from_str(s).map_err(ManifestError::Toml)?;
        resolve_file(file, Path::new(""), source, policy, None)
    }

    pub fn from_json_str_with(
        s: &str,
        source: Option<&dyn ImportSource>,
        policy: ImportPolicy,
    ) -> Result<Self, ManifestError> {
        let file: ManifestFile = serde_json::from_str(s).map_err(ManifestError::Json)?;
        resolve_file(file, Path::new(""), source, policy, None)
    }

    pub fn from_value_with(
        value: serde_json::Value,
        source: Option<&dyn ImportSource>,
        policy: ImportPolicy,
    ) -> Result<Self, ManifestError> {
        let file: ManifestFile = serde_json::from_value(value).map_err(ManifestError::Json)?;
        resolve_file(file, Path::new(""), source, policy, None)
    }

    /// Parse a manifest file at `path` and run the resolver against it.
    ///
    /// - `manifest_repo_root`: directory used to anchor relative paths in
    ///   `self.import:` / top-level `import:`. Pass `None` to use the file's
    ///   own parent directory (fine when imports are policy-skipped).
    /// - `source`: per-project import callback. Pass `None` to skip per-project
    ///   imports (or have them returned as "unavailable").
    /// - `policy`: per-site `import:` policy. See [`ImportPolicy`].
    pub fn from_path_with(
        path: &Path,
        manifest_repo_root: Option<&Path>,
        source: Option<&dyn ImportSource>,
        policy: ImportPolicy,
    ) -> Result<Self, ManifestError> {
        let body = fs::read_to_string(path).map_err(|e| ManifestError::Io {
            path: path.to_owned(),
            source: e,
        })?;
        let file = parse_body_by_extension(path, &body)?;
        let fallback_root: PathBuf;
        let repo_root: &Path = if let Some(r) = manifest_repo_root {
            r
        } else {
            fallback_root = path.parent().map(Path::to_path_buf).unwrap_or_default();
            &fallback_root
        };
        resolve_file(file, repo_root, source, policy, Some(path))
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
    /// least one of its groups is *not* in the disabled set. (Last
    /// matching ± entry wins as a side-effect: a later `+foo` clears
    /// `foo` from the set, and a later `-foo` adds it.)
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

    /// Walk the parsed manifest into a `serde_json::Value` matching the
    /// canonical `west.yml` shape after import resolution:
    ///
    /// ```yaml
    /// manifest:
    ///   group-filter: [...]       # only if non-empty
    ///   self: {path, west-commands?}
    ///   projects: [...]
    /// ```
    ///
    /// This is the input to the structured-data serializers the CLI
    /// uses for `west manifest --resolve` / `--freeze`. Resolved-form
    /// only: there's no `remotes:` / `defaults:` block, and each
    /// project carries a direct `url` (the input-schema's
    /// `remote:` / `repo-path:` constructs have already been resolved
    /// away). Submodules collapse to `true` (all), are omitted
    /// (none), or rendered as `[{path, name?}, …]` (specific).
    pub fn to_value(&self) -> serde_json::Value {
        use serde_json::{Map, Value, json};

        let group_filter: Vec<Value> = self
            .group_filter
            .iter()
            .map(|e| {
                let sign = if e.disabled { '-' } else { '+' };
                Value::String(format!("{sign}{}", e.group))
            })
            .collect();

        let mut self_block = Map::new();
        self_block.insert(
            "path".into(),
            Value::String(self.self_.path.to_string_lossy().into_owned()),
        );
        match self.self_.west_commands.as_slice() {
            [] => {}
            [single] => {
                self_block.insert(
                    "west-commands".into(),
                    Value::String(single.to_string_lossy().into_owned()),
                );
            }
            many => {
                let arr: Vec<Value> = many
                    .iter()
                    .map(|p| Value::String(p.to_string_lossy().into_owned()))
                    .collect();
                self_block.insert("west-commands".into(), Value::Array(arr));
            }
        }
        if let Some(u) = &self.self_.userdata {
            self_block.insert("userdata".into(), u.clone());
        }

        let projects: Vec<Value> = self.projects.iter().map(project_to_value).collect();

        let mut manifest_block = Map::new();
        if !group_filter.is_empty() {
            manifest_block.insert("group-filter".into(), Value::Array(group_filter));
        }
        manifest_block.insert("self".into(), Value::Object(self_block));
        manifest_block.insert("projects".into(), Value::Array(projects));

        json!({ "manifest": Value::Object(manifest_block) })
    }
}

/// Serialize one resolved `Project` into the canonical YAML shape.
/// Pure function so `Manifest::to_value` can map over `self.projects`
/// without borrowing self.
fn project_to_value(p: &Project) -> serde_json::Value {
    use serde_json::{Map, Value};

    let mut o = Map::new();
    o.insert("name".into(), Value::String(p.name.clone()));
    if let Some(d) = &p.description {
        o.insert("description".into(), Value::String(d.clone()));
    }
    o.insert("url".into(), Value::String(p.url.clone()));
    o.insert("revision".into(), Value::String(p.revision.clone()));
    // Only emit `path` when it differs from `name` — matches python's
    // `Project.as_dict` shape and keeps the output compact for the
    // common case (project named after its directory).
    let path_str = p.path.to_string_lossy();
    if path_str != p.name {
        o.insert("path".into(), Value::String(path_str.into_owned()));
    }
    if let Some(d) = p.clone_depth {
        o.insert("clone-depth".into(), Value::Number(d.into()));
    }
    if !p.west_commands.is_empty() {
        match p.west_commands.as_slice() {
            [single] => {
                o.insert(
                    "west-commands".into(),
                    Value::String(single.to_string_lossy().into_owned()),
                );
            }
            many => {
                let arr: Vec<Value> = many
                    .iter()
                    .map(|p| Value::String(p.to_string_lossy().into_owned()))
                    .collect();
                o.insert("west-commands".into(), Value::Array(arr));
            }
        }
    }
    if !p.groups.is_empty() {
        let arr: Vec<Value> = p.groups.iter().cloned().map(Value::String).collect();
        o.insert("groups".into(), Value::Array(arr));
    }
    match &p.submodules {
        Submodules::None => {}
        Submodules::All => {
            o.insert("submodules".into(), Value::Bool(true));
        }
        Submodules::Specific(items) => {
            let arr: Vec<Value> = items
                .iter()
                .map(|s| {
                    let mut sub = Map::new();
                    sub.insert(
                        "path".into(),
                        Value::String(s.path.to_string_lossy().into_owned()),
                    );
                    if let Some(n) = &s.name {
                        sub.insert("name".into(), Value::String(n.clone()));
                    }
                    Value::Object(sub)
                })
                .collect();
            o.insert("submodules".into(), Value::Array(arr));
        }
    }
    if let Some(u) = &p.userdata {
        o.insert("userdata".into(), u.clone());
    }
    Value::Object(o)
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

/// How to handle a single `import:` site when validating or resolving a
/// manifest. The variants correspond to actions the resolver / validator can
/// take when it encounters an import directive: resolve it normally, drop it
/// silently, drop it with a user-visible warning, or reject the manifest.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SitePolicy {
    /// Resolve the import (chase the file, call the source, recurse).
    /// Only valid on the resolving path — the non-resolving validator has
    /// no way to actually do the work and falls back to [`SitePolicy::Error`].
    Resolve,
    /// Skip the import silently. No warning, no error.
    Skip,
    /// Skip the import after printing a single-line warning to stderr.
    WarnAndStrip,
    /// Reject the manifest with [`ManifestError::ImportNotSupported`].
    Error,
}

/// Per-site policy for the three places an `import:` directive can appear:
/// the top-level `manifest.import:`, the manifest repo's `manifest.self.import:`,
/// and a project entry's `import:`. The site-by-site shape composes the
/// `ImportFlag` bitmask from the python wrapper without multiplying enum
/// variants.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ImportPolicy {
    pub top_level: SitePolicy,
    pub self_repo: SitePolicy,
    pub per_project: SitePolicy,
}

impl ImportPolicy {
    /// Reject any `import:` directive. Used by the format-specific text
    /// loaders (`from_yaml_str`, etc.) that have no resolver attached.
    pub const STRICT: Self = Self {
        top_level: SitePolicy::Error,
        self_repo: SitePolicy::Error,
        per_project: SitePolicy::Error,
    };
    /// Warn-and-strip every site. Used by [`Manifest::from_path_lenient`].
    pub const WARN_AND_STRIP: Self = Self {
        top_level: SitePolicy::WarnAndStrip,
        self_repo: SitePolicy::WarnAndStrip,
        per_project: SitePolicy::WarnAndStrip,
    };
    /// Resolve every site (the resolving path's default).
    pub const RESOLVE_ALL: Self = Self {
        top_level: SitePolicy::Resolve,
        self_repo: SitePolicy::Resolve,
        per_project: SitePolicy::Resolve,
    };
    /// Skip every site silently. Mirrors python `ImportFlag.IGNORE`.
    pub const IGNORE_ALL: Self = Self {
        top_level: SitePolicy::Skip,
        self_repo: SitePolicy::Skip,
        per_project: SitePolicy::Skip,
    };
    /// Resolve top-level / self imports; skip per-project imports.
    /// Mirrors python `ImportFlag.IGNORE_PROJECTS`.
    pub const SKIP_PROJECTS: Self = Self {
        top_level: SitePolicy::Resolve,
        self_repo: SitePolicy::Resolve,
        per_project: SitePolicy::Skip,
    };
    /// Skip top-level / self imports; resolve per-project imports through
    /// the supplied [`ImportSource`]. Mirrors python `ImportFlag.FORCE_PROJECTS`.
    pub const PROJECTS_ONLY: Self = Self {
        top_level: SitePolicy::Skip,
        self_repo: SitePolicy::Skip,
        per_project: SitePolicy::Resolve,
    };
}

/// Single execution path used by every `Manifest::from_*` entry. Validates
/// the parsed schema, sets up a [`Resolver`] with the chosen `policy`, and
/// drives it via [`Resolver::absorb_root`]. When `source` is `None` a
/// private no-op source is used; the resolver still runs but
/// `SitePolicy::Resolve` for per-project imports becomes a silent no-op
/// (the source reports every project as unavailable). `root_canonical_from`
/// is the on-disk path of the root manifest if there is one — used to
/// seed the loop-detection set so a self-import naming the root file
/// produces a clean `ImportLoop` diagnostic.
fn resolve_file(
    file: ManifestFile,
    repo_root: &Path,
    source: Option<&dyn ImportSource>,
    policy: ImportPolicy,
    root_canonical_from: Option<&Path>,
) -> Result<Manifest, ManifestError> {
    file.validate()
        .map_err(|r| ManifestError::Validation(r.to_string()))?;
    let noop = NoopImportSource;
    let src: &dyn ImportSource = source.unwrap_or(&noop);
    let mut resolver = Resolver::new(repo_root, src, policy);
    if let Some(path) = root_canonical_from
        && let Ok(canon) = path.canonicalize()
    {
        resolver.visited_files.insert(canon);
    }
    resolver.absorb_root(file)?;
    resolver.into_manifest()
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

    let west_commands: Vec<PathBuf> = ps.west_commands.map(PathBuf::from).into_iter().collect();

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
        userdata: ps.userdata,
    })
}

/// Returns `Ok(Some(_))` whenever the key was present, so `Option<Option<T>>`
/// can carry both "absent" (`None`) and "explicitly null" (`Some(None)`).
fn deserialize_optional_path<'de, D>(d: D) -> Result<Option<Option<String>>, D::Error>
where
    D: Deserializer<'de>,
{
    Option::<String>::deserialize(d).map(Some)
}

fn build_self(s: Option<SelfSchema>) -> Result<ManifestRepo, ManifestError> {
    let Some(s) = s else {
        return Ok(ManifestRepo::default_with_path("manifest"));
    };
    // `path:` (null) and `path: ""` both count as "present but empty";
    // distinguished from the absent case by the outer Option layer.
    let resolved_path = match &s.path {
        None => None,
        Some(None) => {
            return Err(ManifestError::Validation(
                "self.path must be nonempty if present".to_owned(),
            ));
        }
        Some(Some(p)) if p.is_empty() => {
            return Err(ManifestError::Validation(
                "self.path must be nonempty if present".to_owned(),
            ));
        }
        Some(Some(p)) => Some(p.clone()),
    };
    let path_raw = resolved_path.as_ref().map(PathBuf::from);
    Ok(ManifestRepo {
        path: PathBuf::from(resolved_path.unwrap_or_else(|| "manifest".to_owned())),
        path_raw,
        // `west_commands` is appended by `Resolver::absorb` after each
        // file's Phase A imports complete, so that imported sub-manifest
        // commands land before the importing file's own (v1 "import
        // order: deepest first, importer last").
        west_commands: Vec::new(),
        userdata: s.userdata,
    })
}

impl ManifestRepo {
    fn default_with_path(p: &str) -> Self {
        ManifestRepo {
            path: PathBuf::from(p),
            path_raw: None,
            west_commands: Vec::new(),
            userdata: None,
        }
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
                origin: source.to_owned(),
                item: item.clone(),
                reason: "must begin with `+` or `-`".into(),
            });
        }
        let (disabled, group) = match item.as_bytes()[0] {
            b'+' => (false, &item[1..]),
            b'-' => (true, &item[1..]),
            _ => {
                return Err(ManifestError::InvalidGroupFilter {
                    origin: source.to_owned(),
                    item: item.clone(),
                    reason: "must begin with `+` or `-`".into(),
                });
            }
        };
        if !is_valid_group(group) {
            return Err(ManifestError::InvalidGroupFilter {
                origin: source.to_owned(),
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
pub fn is_valid_group(g: &str) -> bool {
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
    use serde_json::json;

    fn yaml(s: &str) -> Result<Manifest, ManifestError> {
        Manifest::from_yaml_str(s)
    }

    #[test]
    fn to_value_emits_canonical_shape() {
        let m = yaml(
            r#"
manifest:
  group-filter: [-noisy]
  self:
    path: my-manifest
    west-commands: scripts/wc.yml
  projects:
    - name: alpha
      url: https://example.com/alpha
      revision: main
      groups: [optional]
    - name: beta
      url: https://example.com/beta
      path: external/beta
      submodules: true
"#,
        )
        .unwrap();
        let v = m.to_value();
        assert_eq!(
            v,
            json!({
                "manifest": {
                    "group-filter": ["-noisy"],
                    "self": {
                        "path": "my-manifest",
                        "west-commands": "scripts/wc.yml"
                    },
                    "projects": [
                        {
                            "name": "alpha",
                            "url": "https://example.com/alpha",
                            "revision": "main",
                            "groups": ["optional"]
                        },
                        {
                            "name": "beta",
                            "url": "https://example.com/beta",
                            "revision": "master",
                            "path": "external/beta",
                            "submodules": true
                        }
                    ]
                }
            })
        );
    }

    #[test]
    fn to_value_emits_submodule_list_form() {
        let m = yaml(
            r#"
manifest:
  projects:
    - name: a
      url: https://x
      submodules:
        - path: sub1
        - path: sub2
          name: named
"#,
        )
        .unwrap();
        let v = m.to_value();
        let subs = &v["manifest"]["projects"][0]["submodules"];
        assert_eq!(
            *subs,
            json!([{"path": "sub1"}, {"path": "sub2", "name": "named"}])
        );
    }

    #[test]
    fn to_value_omits_optional_empty_fields() {
        let m = yaml(
            r#"
manifest:
  projects:
    - name: a
      url: https://x
"#,
        )
        .unwrap();
        let v = m.to_value();
        let proj = &v["manifest"]["projects"][0];
        // Default rev still emits — it's always present after resolution.
        assert!(proj.get("revision").is_some());
        // None of the truly-optional fields appear.
        assert!(proj.get("description").is_none());
        assert!(proj.get("path").is_none()); // path == name → elided
        assert!(proj.get("clone-depth").is_none());
        assert!(proj.get("groups").is_none());
        assert!(proj.get("west-commands").is_none());
        assert!(proj.get("submodules").is_none());
        // Top-level: no group-filter when manifest doesn't set one.
        assert!(v["manifest"].get("group-filter").is_none());
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
        // v0.10 simplification: `+core` collapses (groups are enabled
        // by default), only `-optional` survives.
        assert_eq!(m.group_filter.len(), 1);
        assert_eq!(m.group_filter[0].group, "optional");
        assert!(m.group_filter[0].disabled);
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
    fn west_commands_per_project_is_string_only() {
        // Per-project `west-commands:` accepts a scalar string only. The
        // list form is reserved for the `self.west-commands` field; legacy
        // v1 rejects the list form here (even with a single entry) — see
        // `tests/manifests/invalid_west_commands_2.yml`.
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

        let err = yaml(
            r#"
manifest:
  projects:
    - name: p2
      url: https://x
      west-commands: [a.yml, b.yml]
"#,
        )
        .unwrap_err();
        assert!(matches!(err, ManifestError::Yaml(_)), "got {err:?}");
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
    /// bodies. Returns `Ok(None)` for unmapped projects, exercising the
    /// resolver's "missing import file → silently skipped" branch.
    /// Optionally overrides [`ImportSource::project_root`] per project,
    /// which lets tests verify that filesystem imports inside a
    /// project-imported body resolve against the correct base.
    struct StaticImportSource {
        manifests: BTreeMap<String, String>,
        roots: BTreeMap<String, PathBuf>,
        calls: RefCell<Vec<(String, String)>>,
    }

    impl StaticImportSource {
        fn new() -> Self {
            Self {
                manifests: BTreeMap::new(),
                roots: BTreeMap::new(),
                calls: RefCell::new(Vec::new()),
            }
        }
        fn with(mut self, project: &str, body: &str) -> Self {
            self.manifests.insert(project.to_owned(), body.to_owned());
            self
        }
        fn with_root(mut self, project: &str, root: PathBuf) -> Self {
            self.roots.insert(project.to_owned(), root);
            self
        }
    }

    impl ImportSource for StaticImportSource {
        fn project_manifest(
            &self,
            project: &Project,
            file: &str,
        ) -> Result<Option<ImportContent>, ImportSourceError> {
            self.calls
                .borrow_mut()
                .push((project.name.clone(), file.to_owned()));
            Ok(self
                .manifests
                .get(&project.name)
                .cloned()
                .map(ImportContent::Single))
        }

        fn project_root(&self, project: &Project) -> Option<PathBuf> {
            self.roots.get(&project.name).cloned()
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
        let m = Manifest::from_path_with(&root, Some(dir.path()), Some(&source), ImportPolicy::RESOLVE_ALL).unwrap();
        let names: Vec<&str> = m.projects.iter().map(|p| p.name.as_str()).collect();
        // Imported projects come before locally-defined ones in the
        // output list — v1's `self.import:` ordering contract.
        assert_eq!(names, vec!["q", "p"]);
    }

    #[test]
    fn import_self_bool_is_rejected_at_parse_time() {
        // v1: `self.import: true|false` is invalid — booleans are only
        // meaningful for per-project imports. Reject before the
        // resolver gets involved so the diagnostic names the value's
        // type instead of "imports unsupported".
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
        let err = Manifest::from_path_with(&root, Some(dir.path()), Some(&source), ImportPolicy::RESOLVE_ALL).unwrap_err();
        match err {
            ManifestError::Validation(msg) => {
                assert!(
                    msg.contains("boolean") && msg.contains("self"),
                    "expected boolean/self in validation error, got: {msg}"
                );
            }
            other => panic!("expected Validation error, got: {other:?}"),
        }
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
        let m = Manifest::from_path_with(&root, Some(dir.path()), Some(&source), ImportPolicy::RESOLVE_ALL).unwrap();
        let names: Vec<&str> = m.projects.iter().map(|p| p.name.as_str()).collect();
        // Imports come first in the output list (q from a.yml, r
        // from b.yml), then the locally-defined `p`.
        assert_eq!(names, vec!["q", "r", "p"]);
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
        let m = Manifest::from_path_with(&root, Some(dir.path()), Some(&source), ImportPolicy::RESOLVE_ALL).unwrap();
        let names: Vec<&str> = m.projects.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, vec!["keepme", "p"]);
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
        let m = Manifest::from_path_with(&root, Some(dir.path()), Some(&source), ImportPolicy::RESOLVE_ALL).unwrap();
        let names: Vec<&str> = m.projects.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, vec!["b", "p"]);
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
        let m = Manifest::from_path_with(&root, Some(dir.path()), Some(&source), ImportPolicy::RESOLVE_ALL).unwrap();
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
        let m = Manifest::from_path_with(&root, Some(dir.path()), Some(&source), ImportPolicy::RESOLVE_ALL).unwrap();
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
        let err = Manifest::from_path_with(&root, Some(dir.path()), Some(&source), ImportPolicy::RESOLVE_ALL).unwrap_err();
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
        let m = Manifest::from_path_with(&root, Some(dir.path()), Some(&source), ImportPolicy::RESOLVE_ALL).unwrap();
        let names: Vec<&str> = m.projects.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, vec!["zephyr", "cmsis"]);
        assert_eq!(source.calls.borrow().len(), 1);
        assert_eq!(source.calls.borrow()[0].0, "zephyr");
    }

    /// Regression: when a project-imported manifest body has its own
    /// `self.import: <dir>/`, the resolver must anchor that
    /// filesystem walk against the *project's* working tree, not the
    /// outer manifest repo. This is the Zephyr setup: example-application
    /// imports zephyr's west.yml, which in turn does
    /// `self.import: submanifests/` — and `submanifests/` lives inside
    /// the zephyr clone, not next to example-application's manifest.
    #[test]
    fn project_imported_body_self_import_resolves_against_project_root() {
        // The outer manifest repo.
        let outer = tempfile::TempDir::new().unwrap();
        let outer_root = write_yaml(
            outer.path(),
            "west.yml",
            r#"
manifest:
  projects:
    - name: zephyr
      url: https://example.com/zephyr
      import: true
"#,
        );

        // The project's working tree, with `submanifests/extras.yml`.
        let project_root_dir = tempfile::TempDir::new().unwrap();
        let submanifests = project_root_dir.path().join("submanifests");
        std::fs::create_dir_all(&submanifests).unwrap();
        std::fs::write(
            submanifests.join("extras.yml"),
            r#"
manifest:
  projects:
    - name: cmsis
      url: https://example.com/cmsis
"#,
        )
        .unwrap();

        // Source returns zephyr's manifest body inline; its `project_root`
        // points the resolver at the temp project tree so the
        // `self.import: submanifests` inside zephyr's body resolves
        // there, not next to `outer/west.yml`.
        let source = StaticImportSource::new()
            .with(
                "zephyr",
                r#"
manifest:
  self:
    import: submanifests
  projects: []
"#,
            )
            .with_root("zephyr", project_root_dir.path().to_path_buf());

        let m = Manifest::from_path_with(&outer_root, Some(outer.path()), Some(&source), ImportPolicy::RESOLVE_ALL).unwrap();
        let names: Vec<&str> = m.projects.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, vec!["zephyr", "cmsis"]);
    }

    /// Negative: same shape as above but the source returns
    /// `project_root = None` (default). The resolver falls back to the
    /// outer manifest repo for the nested filesystem import — same
    /// `submanifests/` directory next to `outer/west.yml` IS read,
    /// confirming the fallback path still works for in-memory sources.
    #[test]
    fn project_imported_body_self_import_falls_back_when_no_project_root() {
        let outer = tempfile::TempDir::new().unwrap();
        let outer_root = write_yaml(
            outer.path(),
            "west.yml",
            r#"
manifest:
  projects:
    - name: zephyr
      url: https://example.com/zephyr
      import: true
"#,
        );
        // Place the imported submanifest next to outer/west.yml — the
        // fallback's natural location.
        let submanifests = outer.path().join("submanifests");
        std::fs::create_dir_all(&submanifests).unwrap();
        std::fs::write(
            submanifests.join("extras.yml"),
            r#"
manifest:
  projects:
    - name: cmsis
      url: https://example.com/cmsis
"#,
        )
        .unwrap();

        let source = StaticImportSource::new().with(
            "zephyr",
            r#"
manifest:
  self:
    import: submanifests
  projects: []
"#,
        );

        let m = Manifest::from_path_with(&outer_root, Some(outer.path()), Some(&source), ImportPolicy::RESOLVE_ALL).unwrap();
        let names: Vec<&str> = m.projects.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, vec!["zephyr", "cmsis"]);
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
        let m = Manifest::from_path_with(&root, Some(dir.path()), Some(&source), ImportPolicy::RESOLVE_ALL).unwrap();
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
        let m = Manifest::from_path_with(&root, Some(dir.path()), Some(&source), ImportPolicy::RESOLVE_ALL).unwrap();
        let names: Vec<&str> = m.projects.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, vec!["zephyr"]);
    }

    #[test]
    fn import_self_directory_iterates_yml_files_sorted() {
        // Real zephyr uses `self.import: submanifests` with a directory
        // of `*.yml` files; the resolver should pick all of them up in
        // sorted order.
        let dir = tempfile::TempDir::new().unwrap();
        let root = write_yaml(
            dir.path(),
            "west.yml",
            r#"
manifest:
  self:
    import: submanifests
  projects: []
"#,
        );
        let sub = dir.path().join("submanifests");
        std::fs::create_dir(&sub).unwrap();
        // Write in non-alphabetical order; the resolver should still
        // visit them sorted (a.yml, b.yml).
        write_yaml(
            &sub,
            "b.yml",
            r#"
manifest:
  projects:
    - name: from-b
      url: https://b
"#,
        );
        write_yaml(
            &sub,
            "a.yml",
            r#"
manifest:
  projects:
    - name: from-a
      url: https://a
"#,
        );
        // Non-yml files should be ignored.
        std::fs::write(sub.join("README"), "ignore me").unwrap();

        let source = StaticImportSource::new();
        let m = Manifest::from_path_with(&root, Some(dir.path()), Some(&source), ImportPolicy::RESOLVE_ALL).unwrap();
        let names: Vec<&str> = m.projects.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, vec!["from-a", "from-b"]);
    }

    #[test]
    fn import_self_directory_accepts_all_manifest_extensions() {
        // The directory glob accepts every extension the single-file
        // form does — yml/yaml/toml/json — so a mixed-format
        // `submanifests/` folder works.
        let dir = tempfile::TempDir::new().unwrap();
        let root = write_yaml(
            dir.path(),
            "west.yml",
            r#"
manifest:
  self:
    import: submanifests
  projects: []
"#,
        );
        let sub = dir.path().join("submanifests");
        std::fs::create_dir(&sub).unwrap();
        write_yaml(
            &sub,
            "a.yaml",
            r#"
manifest:
  projects:
    - name: from-yaml
      url: https://a
"#,
        );
        std::fs::write(
            sub.join("b.toml"),
            r#"
[manifest]
[[manifest.projects]]
name = "from-toml"
url = "https://b"
"#,
        )
        .unwrap();
        std::fs::write(
            sub.join("c.json"),
            r#"{ "manifest": { "projects": [ { "name": "from-json", "url": "https://c" } ] } }"#,
        )
        .unwrap();
        // Unsupported extensions still ignored.
        std::fs::write(sub.join("README.txt"), "ignore me").unwrap();

        let source = StaticImportSource::new();
        let m = Manifest::from_path_with(&root, Some(dir.path()), Some(&source), ImportPolicy::RESOLVE_ALL).unwrap();
        let mut names: Vec<&str> = m.projects.iter().map(|p| p.name.as_str()).collect();
        names.sort();
        assert_eq!(names, vec!["from-json", "from-toml", "from-yaml"]);
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
        let m = Manifest::from_path_with(&root, Some(dir.path()), Some(&source), ImportPolicy::RESOLVE_ALL).unwrap();
        let p = m.project("deep").unwrap();
        assert_eq!(p.path, PathBuf::from("outer/inner/deep"));
    }

    // ---- ImportPolicy / SitePolicy tests -----------------------------------

    #[test]
    fn policy_skip_projects_drops_per_project_imports_silently() {
        // A project declares `import:` but the source has no manifest for it.
        // With SKIP_PROJECTS the resolver must not call the source for that
        // project and must produce the directly-defined project list.
        let dir = tempfile::TempDir::new().unwrap();
        let root = write_yaml(
            dir.path(),
            "west.yml",
            r#"
manifest:
  projects:
    - name: p
      url: https://x
      import: true
    - name: q
      url: https://y
"#,
        );
        let source = StaticImportSource::new();
        let m = Manifest::from_path_with(&root, Some(dir.path()), Some(&source), ImportPolicy::SKIP_PROJECTS)
        .unwrap();
        let names: Vec<&str> = m.projects.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, vec!["p", "q"]);
        assert!(
            source.calls.borrow().is_empty(),
            "per-project import callback must not fire under SKIP_PROJECTS, got {:?}",
            source.calls.borrow()
        );
    }

    #[test]
    fn policy_skip_self_drops_self_import() {
        // self.import points at a file that doesn't exist on disk;
        // RESOLVE_ALL would fail with Io. SKIP_PROJECTS still resolves
        // self/top-level (they're Resolve in that policy), so use a policy
        // that flips self_repo to Skip directly.
        let dir = tempfile::TempDir::new().unwrap();
        let root = write_yaml(
            dir.path(),
            "west.yml",
            r#"
manifest:
  self:
    import: missing.yml
  projects:
    - name: p
      url: https://x
"#,
        );
        let policy = ImportPolicy {
            self_repo: SitePolicy::Skip,
            ..ImportPolicy::RESOLVE_ALL
        };
        let source = StaticImportSource::new();
        let m = Manifest::from_path_with(&root, Some(dir.path()), Some(&source), policy).unwrap();
        let names: Vec<&str> = m.projects.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, vec!["p"]);
    }

    #[test]
    fn policy_skip_top_level_drops_top_level_import() {
        let dir = tempfile::TempDir::new().unwrap();
        let root = write_yaml(
            dir.path(),
            "west.yml",
            r#"
manifest:
  import: missing.yml
  projects:
    - name: p
      url: https://x
"#,
        );
        let policy = ImportPolicy {
            top_level: SitePolicy::Skip,
            ..ImportPolicy::RESOLVE_ALL
        };
        let source = StaticImportSource::new();
        let m = Manifest::from_path_with(&root, Some(dir.path()), Some(&source), policy).unwrap();
        let names: Vec<&str> = m.projects.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, vec!["p"]);
    }

    #[test]
    fn policy_ignore_all_returns_directly_defined_projects_only() {
        // All three sites declare imports; IGNORE_ALL must produce only the
        // directly-defined projects without touching the filesystem or the
        // source.
        let dir = tempfile::TempDir::new().unwrap();
        let root = write_yaml(
            dir.path(),
            "west.yml",
            r#"
manifest:
  import: nope-top.yml
  self:
    import: nope-self.yml
  projects:
    - name: p
      url: https://x
      import: true
    - name: q
      url: https://y
"#,
        );
        let source = StaticImportSource::new();
        let m = Manifest::from_path_with(&root, Some(dir.path()), Some(&source), ImportPolicy::IGNORE_ALL)
        .unwrap();
        let names: Vec<&str> = m.projects.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, vec!["p", "q"]);
        assert!(source.calls.borrow().is_empty());
    }

    #[test]
    fn policy_projects_only_resolves_project_imports_but_skips_filesystem() {
        // Top-level + self imports declare files that don't exist; under
        // PROJECTS_ONLY they're silently skipped. Per-project import IS
        // resolved through the source.
        let dir = tempfile::TempDir::new().unwrap();
        let root = write_yaml(
            dir.path(),
            "west.yml",
            r#"
manifest:
  import: nope-top.yml
  self:
    import: nope-self.yml
  projects:
    - name: p
      url: https://x
      import: true
"#,
        );
        let imported = r#"
manifest:
  projects:
    - name: from-p
      url: https://from-p
"#;
        let source = StaticImportSource::new().with("p", imported);
        let m = Manifest::from_path_with(&root, Some(dir.path()), Some(&source), ImportPolicy::PROJECTS_ONLY)
        .unwrap();
        let names: Vec<&str> = m.projects.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, vec!["p", "from-p"]);
        // The source must have been called once for p.
        assert_eq!(source.calls.borrow().len(), 1);
        assert_eq!(source.calls.borrow()[0].0, "p");
    }

    #[test]
    fn project_import_path_prefix_applies_to_importing_project() {
        // A project that carries `import: { path-prefix: bar }` has
        // its own path prefixed with `bar` too, so it ends up at
        // `bar/foo` alongside the projects pulled in from foo's
        // imported body (which `absorb_project_import` composes onto
        // the same prefix when recursing).
        let m = Manifest::from_yaml_str_with(r#"
manifest:
  projects:
    - name: foo
      url: https://example.com/foo
      import:
        path-prefix: bar
"#, None, ImportPolicy::IGNORE_ALL)
        .unwrap();
        assert_eq!(m.projects[0].path, PathBuf::from("bar/foo"));
    }

    #[test]
    fn policy_yaml_str_with_imports_round_trip() {
        // Exercise the in-memory entry point used by python
        // `from_data(..., importer=cb, import_flags=FORCE_PROJECTS)`.
        let body = r#"
manifest:
  projects:
    - name: upstream
      url: upstream.com/upstream
      import: true
    - name: downstream
      url: downstream.com/downstream
"#;
        let imported = r#"
manifest:
  projects:
    - name: nested
      url: upstream.com/nested
"#;
        let source = StaticImportSource::new().with("upstream", imported);
        let m = Manifest::from_yaml_str_with(body, Some(&source), ImportPolicy::PROJECTS_ONLY)
            .unwrap();
        let names: Vec<&str> = m.projects.iter().map(|p| p.name.as_str()).collect();
        assert_eq!(names, vec!["upstream", "downstream", "nested"]);
    }
}
