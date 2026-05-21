//! Bindings for `west_core::manifest`: `Manifest`, `Project`, plus
//! supporting types (`ManifestRepo`, `Submodule`, `GroupFilterEntry`),
//! the `parse_cli_group_filter` helper, and the `MalformedManifest` /
//! `ManifestImportFailed` exception classes.
//!
//! This is the data-layer cut: parse + introspect + activity-check.
//! Workspace-integration concerns (git helpers, `from_topdir`,
//! serializers) stay on the python side for now — the python
//! `west.manifest` module wraps this binding and adds those.

use std::path::PathBuf;

use pyo3::create_exception;
use pyo3::exceptions::{PyException, PyIOError, PyKeyError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyList;

use west_core::manifest::{
    self as core, GroupFilterEntry as CoreGroupFilterEntry, ImportPolicy, ImportSource,
    ImportSourceError, ManifestError, Submodule as CoreSubmodule, Submodules as CoreSubmodules,
};

// Mirrors python `west.manifest.ImportFlag` IntFlag bits. Translated to
// `ImportPolicy` at the FFI boundary so python remains the single source of
// truth for the bitmask name.
const FLAG_IGNORE: u32 = 1;
const FLAG_FORCE_PROJECTS: u32 = 2;
const FLAG_IGNORE_PROJECTS: u32 = 4;
const FLAG_ALL: u32 = FLAG_IGNORE | FLAG_FORCE_PROJECTS | FLAG_IGNORE_PROJECTS;

/// Translate a python `ImportFlag` bitmask into the rust [`ImportPolicy`].
///
/// Mirrors the legacy `_flags_ok` semantics: `FORCE_PROJECTS` is incompatible
/// with `IGNORE` / `IGNORE_PROJECTS`, but `IGNORE | IGNORE_PROJECTS` is allowed
/// (redundant but consistent — `IGNORE` subsumes `IGNORE_PROJECTS`). Unknown
/// bits reject loudly.
///
/// `has_source` shapes the meaning of the `DEFAULT` flag (bits == 0): with a
/// caller-supplied import callback we resolve everything; without one, we
/// fall back to `STRICT` so an `import:` directive surfaces as
/// `ManifestImportFailed` instead of an IO error from the resolver's attempt
/// to walk the empty filesystem anchor. The skip flags map identically in
/// either context.
fn flags_to_policy(bits: u32, has_source: bool) -> PyResult<ImportPolicy> {
    if bits & !FLAG_ALL != 0 {
        return Err(PyValueError::new_err(format!(
            "invalid import_flags {bits:#x}: unknown bits set"
        )));
    }
    let has_fp = bits & FLAG_FORCE_PROJECTS != 0;
    let has_skip = bits & (FLAG_IGNORE | FLAG_IGNORE_PROJECTS) != 0;
    if has_fp && has_skip {
        return Err(PyValueError::new_err(format!(
            "invalid import_flags {bits:#x}: FORCE_PROJECTS cannot combine with \
             IGNORE/IGNORE_PROJECTS"
        )));
    }
    Ok(if has_fp {
        ImportPolicy::PROJECTS_ONLY
    } else if bits & FLAG_IGNORE != 0 {
        // IGNORE subsumes IGNORE_PROJECTS.
        ImportPolicy::IGNORE_ALL
    } else if bits & FLAG_IGNORE_PROJECTS != 0 {
        ImportPolicy::SKIP_PROJECTS
    } else if has_source {
        ImportPolicy::RESOLVE_ALL
    } else {
        ImportPolicy::STRICT
    })
}

use super::data::{py_to_value, value_to_py};

create_exception!(
    west._west_native,
    MalformedManifest,
    PyException,
    "The west manifest was malformed."
);

create_exception!(
    west._west_native,
    ManifestImportFailed,
    PyException,
    "Resolving a manifest import failed."
);

// ---- Submodule ------------------------------------------------------------

/// One entry in the per-project `submodules:` mapping. Mirrors the
/// legacy python `west.manifest.Submodule` NamedTuple.
#[pyclass(
    name = "Submodule",
    module = "west._west_native",
    frozen,
    from_py_object
)]
#[derive(Clone)]
pub struct Submodule {
    #[pyo3(get)]
    path: String,
    #[pyo3(get)]
    name: Option<String>,
}

#[pymethods]
impl Submodule {
    #[new]
    #[pyo3(signature = (path, name=None))]
    fn py_new(path: String, name: Option<String>) -> Self {
        Submodule { path, name }
    }

    fn __repr__(&self) -> String {
        match &self.name {
            Some(n) => format!("Submodule(path={:?}, name={:?})", self.path, n),
            None => format!("Submodule(path={:?})", self.path),
        }
    }
}

impl Submodule {
    fn from_core(s: &CoreSubmodule) -> Self {
        Submodule {
            path: s.path.to_string_lossy().into_owned(),
            name: s.name.clone(),
        }
    }
}

// ---- GroupFilterEntry -----------------------------------------------------

/// One parsed `+group` / `-group` entry. The python representation is
/// frozen — it's a parsed token, not a mutable configuration value.
#[pyclass(
    name = "GroupFilterEntry",
    module = "west._west_native",
    frozen,
    from_py_object
)]
#[derive(Clone)]
pub struct GroupFilterEntry {
    #[pyo3(get)]
    group: String,
    #[pyo3(get)]
    disabled: bool,
}

#[pymethods]
impl GroupFilterEntry {
    #[new]
    fn py_new(group: String, disabled: bool) -> Self {
        GroupFilterEntry { group, disabled }
    }

    fn __repr__(&self) -> String {
        format!(
            "GroupFilterEntry(group={:?}, disabled={})",
            self.group, self.disabled
        )
    }
}

impl GroupFilterEntry {
    fn from_core(e: &CoreGroupFilterEntry) -> Self {
        GroupFilterEntry {
            group: e.group.clone(),
            disabled: e.disabled,
        }
    }

    fn to_core(&self) -> CoreGroupFilterEntry {
        CoreGroupFilterEntry {
            group: self.group.clone(),
            disabled: self.disabled,
        }
    }
}

// ---- Project --------------------------------------------------------------

/// Per-project entry pulled from the manifest. The bound type carries
/// only the fields that come from the manifest YAML/TOML/JSON — git
/// helpers, `topdir`, and `abspath` live on the python wrapper.
#[pyclass(
    name = "Project",
    module = "west._west_native",
    subclass,
    from_py_object
)]
#[derive(Clone)]
pub struct Project {
    #[pyo3(get)]
    name: String,
    #[pyo3(get)]
    url: String,
    #[pyo3(get)]
    revision: String,
    /// Manifest-relative path string. The python wrapper resolves to
    /// absolute paths against `topdir` since `topdir` is a workspace
    /// concept the rust core doesn't track.
    #[pyo3(get)]
    path: String,
    #[pyo3(get)]
    description: Option<String>,
    #[pyo3(get)]
    groups: Vec<String>,
    #[pyo3(get)]
    clone_depth: Option<u32>,
    #[pyo3(get)]
    remote_name: String,
    /// `west_commands` entries; each is a project-relative path.
    #[pyo3(get)]
    west_commands: Vec<String>,
    /// Stored as the rust-side enum so `submodules` getter can pick
    /// the right python representation lazily.
    submodules: CoreSubmodules,
    /// Opaque payload from `userdata:`. Surfaced via a getter (rather
    /// than `#[pyo3(get)]`) because `Value` isn't an
    /// `IntoPyObject` and the conversion needs the GIL token.
    userdata: Option<serde_json::Value>,
}

#[pymethods]
impl Project {
    /// `True` (all), `False` (none), or a `list[Submodule]` for the
    /// explicit per-name form. Mirrors the legacy `SubmodulesType`.
    #[getter]
    fn submodules<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        match &self.submodules {
            CoreSubmodules::All => Ok(true.into_pyobject(py)?.to_owned().into_any()),
            CoreSubmodules::None => Ok(false.into_pyobject(py)?.to_owned().into_any()),
            CoreSubmodules::Specific(items) => {
                let mapped: Vec<Submodule> = items.iter().map(Submodule::from_core).collect();
                Ok(
                    PyList::new(py, mapped.into_iter().map(|s| s.into_pyobject(py).unwrap()))?
                        .into_any(),
                )
            }
        }
    }

    /// Free-form `userdata:` payload. Returns `None` when absent;
    /// otherwise a python value mirroring the parsed YAML/TOML/JSON
    /// structure (dict / list / str / int / float / bool / None).
    #[getter]
    fn userdata<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        match &self.userdata {
            Some(v) => value_to_py(py, v),
            None => Ok(py.None().into_bound(py)),
        }
    }

    fn __repr__(&self) -> String {
        format!(
            "Project(name={:?}, path={:?}, revision={:?})",
            self.name, self.path, self.revision
        )
    }
}

impl Project {
    fn from_core(p: &core::Project) -> Self {
        Project {
            name: p.name.clone(),
            url: p.url.clone(),
            revision: p.revision.clone(),
            path: p.path.to_string_lossy().into_owned(),
            description: p.description.clone(),
            groups: p.groups.clone(),
            clone_depth: p.clone_depth,
            remote_name: p.remote_name.clone(),
            west_commands: p
                .west_commands
                .iter()
                .map(|p| p.to_string_lossy().into_owned())
                .collect(),
            submodules: p.submodules.clone(),
            userdata: p.userdata.clone(),
        }
    }
}

// ---- ManifestRepo (the `self:` block) -------------------------------------

/// The manifest's `self:` block — workspace-relative path of the
/// manifest repo plus its `west-commands` files.
#[pyclass(
    name = "ManifestRepo",
    module = "west._west_native",
    frozen,
    from_py_object
)]
#[derive(Clone)]
pub struct ManifestRepo {
    /// Resolved manifest-repo path. `"manifest"` when the manifest omitted
    /// `self.path:`.
    #[pyo3(get)]
    path: String,
    /// Literal `self.path:` as written in the manifest. `None` when omitted —
    /// distinct from `Some("manifest")`.
    #[pyo3(get)]
    path_raw: Option<String>,
    #[pyo3(get)]
    west_commands: Vec<String>,
    /// `manifest.self.userdata`, surfaced verbatim. See `Project::userdata`
    /// for the conversion contract.
    userdata: Option<serde_json::Value>,
}

#[pymethods]
impl ManifestRepo {
    #[getter]
    fn userdata<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        match &self.userdata {
            Some(v) => value_to_py(py, v),
            None => Ok(py.None().into_bound(py)),
        }
    }

    fn __repr__(&self) -> String {
        format!("ManifestRepo(path={:?})", self.path)
    }
}

impl ManifestRepo {
    fn from_core(r: &core::ManifestRepo) -> Self {
        ManifestRepo {
            path: r.path.to_string_lossy().into_owned(),
            path_raw: r.path_raw.as_ref().map(|p| p.to_string_lossy().into_owned()),
            west_commands: r
                .west_commands
                .iter()
                .map(|p| p.to_string_lossy().into_owned())
                .collect(),
            userdata: r.userdata.clone(),
        }
    }
}

// ---- Manifest -------------------------------------------------------------

/// Parsed manifest object. The factories on this class handle the
/// data-layer parsing only; workspace concerns (`topdir`,
/// `manifest.path`/`manifest.file` config, ZEPHYR-style imports) sit
/// in the python `west.manifest` wrapper.
#[pyclass(name = "Manifest", module = "west._west_native", subclass, unsendable)]
pub struct Manifest {
    inner: core::Manifest,
}

#[pymethods]
impl Manifest {
    /// Parse from a YAML string. Mirrors python's eventual
    /// `Manifest.from_data(str)` — the python wrapper picks YAML vs
    /// TOML vs JSON based on file extension or caller intent and
    /// delegates here.
    ///
    /// `import_flags` is the python `ImportFlag` bitmask. `DEFAULT` and
    /// `IGNORE` work as expected. Flags that imply a callback
    /// (`FORCE_PROJECTS`) or have nothing to act on here (`IGNORE_PROJECTS`)
    /// reach the resolver and either silently no-op or surface
    /// `ManifestImportFailed` if the manifest declares an import — callers
    /// that want guaranteed semantics should use `from_yaml_str_with_imports`
    /// for the FORCE_PROJECTS case.
    #[staticmethod]
    #[pyo3(signature = (s, import_flags=0))]
    fn from_yaml_str(s: &str, import_flags: u32) -> PyResult<Self> {
        let policy = flags_to_policy(import_flags, false)?;
        core::Manifest::from_yaml_str_with(s, None, policy)
            .map(|inner| Manifest { inner })
            .map_err(manifest_error_to_py)
    }

    #[staticmethod]
    #[pyo3(signature = (s, import_flags=0))]
    fn from_toml_str(s: &str, import_flags: u32) -> PyResult<Self> {
        let policy = flags_to_policy(import_flags, false)?;
        core::Manifest::from_toml_str_with(s, None, policy)
            .map(|inner| Manifest { inner })
            .map_err(manifest_error_to_py)
    }

    #[staticmethod]
    #[pyo3(signature = (s, import_flags=0))]
    fn from_json_str(s: &str, import_flags: u32) -> PyResult<Self> {
        let policy = flags_to_policy(import_flags, false)?;
        core::Manifest::from_json_str_with(s, None, policy)
            .map(|inner| Manifest { inner })
            .map_err(manifest_error_to_py)
    }

    /// Construct from an already-parsed python `dict` (or any
    /// mapping/sequence/scalar tree the structured-data
    /// converter accepts). Useful when the caller already holds the data
    /// in-memory (e.g. `west.manifest.validate(d)` where `d` is
    /// a dict).
    ///
    /// Internally: python value → `serde_json::Value` (via the
    /// same `py_to_value` the `dump_*` bindings use) →
    /// `Manifest::from_value`. No JSON text round-trip.
    #[staticmethod]
    #[pyo3(signature = (value, import_flags=0))]
    fn from_dict(value: &Bound<'_, PyAny>, import_flags: u32) -> PyResult<Self> {
        let policy = flags_to_policy(import_flags, false)?;
        let v = py_to_value(value)?;
        core::Manifest::from_value_with(v, None, policy)
            .map(|inner| Manifest { inner })
            .map_err(manifest_error_to_py)
    }

    /// Parse the manifest file at `path`. Format dispatch is by
    /// extension (yaml/yml/toml/json). Imports in the manifest raise
    /// `ManifestImportFailed` unless `import_flags=IGNORE` is passed;
    /// use `from_path_with_imports` to actually resolve them.
    #[staticmethod]
    #[pyo3(signature = (path, import_flags=0))]
    fn from_path(path: PathBuf, import_flags: u32) -> PyResult<Self> {
        let policy = flags_to_policy(import_flags, false)?;
        core::Manifest::from_path_with(&path, None, None, policy)
            .map(|inner| Manifest { inner })
            .map_err(manifest_error_to_py)
    }

    /// Parse the manifest at `path`, resolving any imports via the
    /// supplied python `callback`. `manifest_repo_root` is the
    /// workspace-absolute path to the manifest repo (used to resolve
    /// top-level / self-repo imports relative to the right tree).
    ///
    /// The callback signature is `(project_name: str,
    /// project_path: str, relative_file: str) -> str | None`:
    ///   - return the file's content (as a string) to feed the
    ///     resolver,
    ///   - return `None` to signal "this import is unavailable" (the
    ///     resolver continues with whatever has been collected so far —
    ///     same semantics as west's read-only flows).
    ///
    /// `import_flags` accepts the full set: `DEFAULT` (resolve all),
    /// `IGNORE` (skip everything; callback never fires), `FORCE_PROJECTS`
    /// (skip filesystem imports, resolve per-project through the callback),
    /// and `IGNORE_PROJECTS` (resolve filesystem imports, skip per-project
    /// — used by `west update` flows when the imported projects aren't
    /// yet cloned).
    ///
    /// Errors raised inside the callback are surfaced as
    /// `ManifestImportFailed`.
    #[staticmethod]
    #[pyo3(signature = (path, manifest_repo_root, callback, import_flags=0))]
    fn from_path_with_imports(
        path: PathBuf,
        manifest_repo_root: PathBuf,
        callback: Py<PyAny>,
        import_flags: u32,
    ) -> PyResult<Self> {
        let policy = flags_to_policy(import_flags, true)?;
        let source = PyImportSource { callback };
        core::Manifest::from_path_with(&path, Some(&manifest_repo_root), Some(&source), policy)
            .map(|inner| Manifest { inner })
            .map_err(manifest_error_to_py)
    }

    /// Parse a YAML manifest string and resolve per-project imports via
    /// `callback`. Used by `Manifest.from_data(..., importer=cb,
    /// import_flags=FORCE_PROJECTS)` — no workspace anchor, so only
    /// `FORCE_PROJECTS` is meaningful (filesystem imports under any other
    /// policy will fail with an IO error against the empty root).
    #[staticmethod]
    #[pyo3(signature = (s, callback, import_flags=FLAG_FORCE_PROJECTS))]
    fn from_yaml_str_with_imports(
        s: &str,
        callback: Py<PyAny>,
        import_flags: u32,
    ) -> PyResult<Self> {
        let policy = flags_to_policy(import_flags, true)?;
        let source = PyImportSource { callback };
        core::Manifest::from_yaml_str_with(s, Some(&source), policy)
            .map(|inner| Manifest { inner })
            .map_err(manifest_error_to_py)
    }

    #[getter]
    fn version(&self) -> Option<String> {
        self.inner.version.clone()
    }

    #[getter]
    fn self_(&self) -> ManifestRepo {
        ManifestRepo::from_core(&self.inner.self_)
    }

    /// All projects declared in the manifest, in manifest order.
    /// Returns fresh `Project` instances on each access (each is a
    /// shallow clone of the rust state).
    #[getter]
    fn projects(&self) -> Vec<Project> {
        self.inner.projects.iter().map(Project::from_core).collect()
    }

    /// Top-level group-filter entries. Workspace-level `manifest.group-filter`
    /// config layers are composed by the python caller, not here.
    #[getter]
    fn group_filter(&self) -> Vec<GroupFilterEntry> {
        self.inner
            .group_filter
            .iter()
            .map(GroupFilterEntry::from_core)
            .collect()
    }

    /// Look up a project by its manifest name. Returns `None` if no
    /// such project exists; the python wrapper raises `ValueError` /
    /// `KeyError` as it sees fit.
    fn project(&self, name: &str) -> Option<Project> {
        self.inner.project(name).map(Project::from_core)
    }

    /// Resolve a list of project selectors (names) to `Project`
    /// records. Unknown names raise `KeyError`.
    fn resolve_projects(&self, selectors: Vec<String>) -> PyResult<Vec<Project>> {
        match self.inner.resolve_projects(selectors.iter()) {
            Ok(refs) => Ok(refs.into_iter().map(Project::from_core).collect()),
            Err(e) => Err(manifest_error_to_py(e)),
        }
    }

    /// Return `True` if `project` is active under the manifest's
    /// group filter, optionally composed with `extra_filter`
    /// (typically sourced from CLI flags or `manifest.group-filter`).
    #[pyo3(signature = (project, extra_filter=None))]
    fn is_active(
        &self,
        project: &Project,
        extra_filter: Option<Vec<PyRef<'_, GroupFilterEntry>>>,
    ) -> PyResult<bool> {
        // Reconstruct a `core::Project` for the `is_active` check —
        // only the `groups` field matters to the activity decision
        // (verified by inspecting `core::Manifest::is_active`).
        let core_project = core::Project {
            name: project.name.clone(),
            url: project.url.clone(),
            revision: project.revision.clone(),
            path: PathBuf::from(&project.path),
            description: project.description.clone(),
            groups: project.groups.clone(),
            clone_depth: project.clone_depth,
            west_commands: project.west_commands.iter().map(PathBuf::from).collect(),
            remote_name: project.remote_name.clone(),
            submodules: project.submodules.clone(),
            userdata: project.userdata.clone(),
        };
        let extra: Vec<CoreGroupFilterEntry> = extra_filter
            .into_iter()
            .flatten()
            .map(|e| e.to_core())
            .collect();
        Ok(self.inner.is_active(&core_project, &extra))
    }

    fn __repr__(&self) -> String {
        format!(
            "Manifest(self.path={:?}, projects={})",
            self.inner.self_.path.to_string_lossy(),
            self.inner.projects.len()
        )
    }
}

// ---- Module-level functions ----------------------------------------------

/// Parse `+group` / `-group` tokens (typically CLI-sourced) into
/// structured entries. Empty input yields an empty list.
#[pyfunction]
fn parse_cli_group_filter(items: Vec<String>) -> PyResult<Vec<GroupFilterEntry>> {
    match core::parse_cli_group_filter(&items) {
        Ok(entries) => Ok(entries.iter().map(GroupFilterEntry::from_core).collect()),
        Err(e) => Err(manifest_error_to_py(e)),
    }
}

// ---- Import resolution bridge --------------------------------------------

/// Adapts a python callable to `west_core::manifest::ImportSource`.
/// Each `project_manifest` invocation acquires the GIL, calls the
/// python callable with `(project_name, project_path, relative_file)`,
/// and translates the return value: `None` → resolver continues
/// without this import; `str` → fed to the resolver as the import's
/// content; any other type, or an exception, → `ImportSourceError`
/// (which the rust resolver surfaces as
/// `ManifestError::ImportSourceFailed` → `ManifestImportFailed`).
struct PyImportSource {
    callback: Py<PyAny>,
}

impl ImportSource for PyImportSource {
    fn project_manifest(
        &self,
        project: &core::Project,
        relative_file: &str,
    ) -> Result<Option<String>, ImportSourceError> {
        Python::attach(|py| {
            let project_path = project.path.to_string_lossy();
            let result = self
                .callback
                .bind(py)
                .call1((project.name.as_str(), &*project_path, relative_file))
                .map_err(|e| ImportSourceError::msg(format!("{}: {e}", project.name)))?;
            if result.is_none() {
                Ok(None)
            } else {
                let s: String = result
                    .extract()
                    .map_err(|e| ImportSourceError::msg(format!("{}: {e}", project.name)))?;
                Ok(Some(s))
            }
        })
    }
}

// ---- Error translation ----------------------------------------------------

/// `ManifestError` → python exception. Parse / validation / content
/// errors collapse to `MalformedManifest`; import-time failures map
/// to `ManifestImportFailed`; `UnknownProject` raises `KeyError`; I/O
/// failures raise `OSError`.
fn manifest_error_to_py(err: ManifestError) -> PyErr {
    match err {
        ManifestError::UnknownProject(s) => PyKeyError::new_err(s),
        ManifestError::Io { ref path, .. } => {
            PyIOError::new_err(format!("{}: {err}", path.display()))
        }
        ManifestError::ImportNotSupported { .. }
        | ManifestError::ImportLoop { .. }
        | ManifestError::ImportTooDeep { .. }
        | ManifestError::ImportSourceFailed { .. } => {
            ManifestImportFailed::new_err(err.to_string())
        }
        _ => MalformedManifest::new_err(err.to_string()),
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("MalformedManifest", m.py().get_type::<MalformedManifest>())?;
    m.add(
        "ManifestImportFailed",
        m.py().get_type::<ManifestImportFailed>(),
    )?;
    m.add_class::<Submodule>()?;
    m.add_class::<GroupFilterEntry>()?;
    m.add_class::<ManifestRepo>()?;
    m.add_class::<Project>()?;
    m.add_class::<Manifest>()?;
    m.add_function(wrap_pyfunction!(parse_cli_group_filter, m)?)?;
    Ok(())
}
