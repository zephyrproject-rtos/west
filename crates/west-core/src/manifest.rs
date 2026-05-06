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
    ImportNotSupported {
        context: String,
    },
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

#[derive(Debug, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
struct ManifestFile {
    #[garde(dive)]
    manifest: ManifestSection,
}

#[derive(Debug, Deserialize, Validate)]
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
    import: Option<serde::de::IgnoredAny>,
}

#[derive(Debug, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
struct RemoteSchema {
    #[garde(length(min = 1))]
    name: String,
    #[garde(length(min = 1))]
    #[serde(rename = "url-base")]
    url_base: String,
}

#[derive(Debug, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
struct DefaultsSchema {
    #[garde(skip)]
    #[serde(default)]
    remote: Option<String>,
    #[garde(skip)]
    #[serde(default)]
    revision: Option<String>,
}

#[derive(Debug, Deserialize, Validate)]
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
    import: Option<serde::de::IgnoredAny>,
}

#[derive(Debug, Deserialize, Validate)]
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
    import: Option<serde::de::IgnoredAny>,
    /// Parsed and held only to avoid `deny_unknown_fields` rejecting it. Typed
    /// access deferred until a real consumer exists.
    #[allow(dead_code)]
    #[garde(skip)]
    #[serde(default)]
    userdata: Option<serde::de::IgnoredAny>,
}

#[derive(Debug, Deserialize)]
#[serde(untagged)]
enum SubmodulesSchema {
    All(bool),
    Specific(Vec<SubmoduleSchema>),
}

#[derive(Debug, Deserialize, Validate)]
#[serde(deny_unknown_fields)]
struct SubmoduleSchema {
    #[garde(length(min = 1))]
    path: String,
    #[garde(skip)]
    #[serde(default)]
    name: Option<String>,
}

#[derive(Debug, Deserialize)]
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

// =====================================================================
// Public entry points
// =====================================================================

impl Manifest {
    pub fn from_yaml_str(s: &str) -> Result<Self, ManifestError> {
        let file: ManifestFile = serde_saphyr::from_str(s).map_err(ManifestError::Yaml)?;
        validate_and_resolve(file)
    }

    pub fn from_toml_str(s: &str) -> Result<Self, ManifestError> {
        let file: ManifestFile = toml_edit::de::from_str(s).map_err(ManifestError::Toml)?;
        validate_and_resolve(file)
    }

    pub fn from_json_str(s: &str) -> Result<Self, ManifestError> {
        let file: ManifestFile = serde_json::from_str(s).map_err(ManifestError::Json)?;
        validate_and_resolve(file)
    }

    /// Sniff `.yaml` / `.yml` / `.toml` / `.json` from the path's extension.
    pub fn from_path(path: &Path) -> Result<Self, ManifestError> {
        let body = fs::read_to_string(path).map_err(|e| ManifestError::Io {
            path: path.to_owned(),
            source: e,
        })?;
        match path.extension().and_then(OsStr::to_str) {
            Some("yaml") | Some("yml") => Self::from_yaml_str(&body),
            Some("toml") => Self::from_toml_str(&body),
            Some("json") => Self::from_json_str(&body),
            other => Err(ManifestError::UnsupportedFormat(
                other.unwrap_or("").to_owned(),
            )),
        }
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
}

// =====================================================================
// Validation + resolution
// =====================================================================

fn validate_and_resolve(file: ManifestFile) -> Result<Manifest, ManifestError> {
    file.validate()
        .map_err(|r| ManifestError::Validation(r.to_string()))?;
    resolve(file)
}

fn resolve(file: ManifestFile) -> Result<Manifest, ManifestError> {
    let m = file.manifest;

    if m.import.is_some() {
        return Err(ManifestError::ImportNotSupported {
            context: "top-level".into(),
        });
    }

    if let Some(self_) = &m.self_ {
        if self_.import.is_some() {
            return Err(ManifestError::ImportNotSupported {
                context: "self".into(),
            });
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
            return Err(ManifestError::ImportNotSupported {
                context: format!("project {:?}", ps.name),
            });
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
}
