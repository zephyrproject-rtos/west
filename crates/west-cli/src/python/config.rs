//! Bindings for `west_core::config::Configuration`: layered TOML config
//! exposed to python as `Configuration` + `ConfigFile` enum +
//! `MalformedConfig` exception. The python side (`west.configuration`)
//! re-exports these directly.
//!
//! `west_core::config` itself is level-agnostic (just a stack of TOML
//! layers); `west_core::config_paths::resolve()` is where the
//! system/global/conf.d/local convention lives. The binding glues
//! python's enum-based addressing (`ConfigFile.LOCAL` etc.) onto the
//! path-based rust API.

use std::path::PathBuf;

use pyo3::create_exception;
use pyo3::exceptions::{PyException, PyKeyError, PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyList;

use west_core::config::{ConfigError, ConfigValue};
use west_core::config_paths::{ResolvedConfig, resolve};

create_exception!(
    west._west_native,
    MalformedConfig,
    PyException,
    "The west configuration was malformed."
);

/// Python-visible enum identifying *which* config layer(s) a method
/// operates on. Mirrors the legacy `west.configuration.ConfigFile`:
/// integer discriminants are preserved so any caller that compares to
/// the bare ints still works (the python class was `Enum`, not
/// `IntEnum`, but the values were stable and a few callers leaned on
/// them).
#[pyclass(
    eq,
    eq_int,
    frozen,
    from_py_object,
    name = "ConfigFile",
    module = "west._west_native"
)]
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
#[allow(clippy::upper_case_acronyms)] // python ConfigFile.ALL etc. are stable
pub enum ConfigFile {
    ALL = 1,
    SYSTEM = 2,
    GLOBAL = 3,
    LOCAL = 4,
}

#[pyclass(name = "Configuration", module = "west._west_native", unsendable, subclass)]
pub struct Configuration {
    inner: west_core::config::Configuration,
    resolved: ResolvedConfig,
}

#[pymethods]
impl Configuration {
    #[new]
    #[pyo3(signature = (topdir=None))]
    fn py_new(topdir: Option<PathBuf>) -> PyResult<Self> {
        let resolved = resolve(topdir.as_deref());
        let inner = west_core::config::Configuration::load(resolved.layer_paths())
            .map_err(config_error_to_py)?;
        Ok(Configuration { inner, resolved })
    }

    /// String accessor. Walks `configfile`'s layers high → low; returns
    /// `default` if the option isn't set anywhere.
    #[pyo3(signature = (option, default=None, configfile=ConfigFile::ALL))]
    fn get(
        &self,
        option: &str,
        default: Option<String>,
        configfile: ConfigFile,
    ) -> PyResult<Option<String>> {
        let paths = self.paths_for(configfile);
        for layer in paths.iter().rev() {
            match self.inner.get_str_in(option, layer) {
                Ok(Some(v)) => return Ok(Some(v)),
                Ok(None) => continue,
                Err(e) => return Err(config_error_to_py(e)),
            }
        }
        Ok(default)
    }

    #[pyo3(signature = (option, default=false, configfile=ConfigFile::ALL))]
    fn getboolean(
        &self,
        option: &str,
        default: bool,
        configfile: ConfigFile,
    ) -> PyResult<bool> {
        let paths = self.paths_for(configfile);
        for layer in paths.iter().rev() {
            match self.inner.get_bool_in(option, layer) {
                Ok(Some(v)) => return Ok(v),
                Ok(None) => continue,
                Err(e) => return Err(config_error_to_py(e)),
            }
        }
        Ok(default)
    }

    #[pyo3(signature = (option, default=None, configfile=ConfigFile::ALL))]
    fn getint(
        &self,
        option: &str,
        default: Option<i64>,
        configfile: ConfigFile,
    ) -> PyResult<Option<i64>> {
        let paths = self.paths_for(configfile);
        for layer in paths.iter().rev() {
            match self.inner.get_i64_in(option, layer) {
                Ok(Some(v)) => return Ok(Some(v)),
                Ok(None) => continue,
                Err(e) => return Err(config_error_to_py(e)),
            }
        }
        Ok(default)
    }

    #[pyo3(signature = (option, default=None, configfile=ConfigFile::ALL))]
    fn getfloat(
        &self,
        option: &str,
        default: Option<f64>,
        configfile: ConfigFile,
    ) -> PyResult<Option<f64>> {
        let paths = self.paths_for(configfile);
        for layer in paths.iter().rev() {
            match self.inner.get_f64_in(option, layer) {
                Ok(Some(v)) => return Ok(Some(v)),
                Ok(None) => continue,
                Err(e) => return Err(config_error_to_py(e)),
            }
        }
        Ok(default)
    }

    /// Set an option. `configfile` must be a single concrete layer
    /// (`SYSTEM` / `GLOBAL` / `LOCAL`); `ALL` and ambiguous multi-path
    /// configs raise `ValueError`, matching the legacy python
    /// behaviour.
    #[pyo3(signature = (option, value, configfile=ConfigFile::LOCAL))]
    fn set(
        &mut self,
        option: &str,
        value: &Bound<'_, PyAny>,
        configfile: ConfigFile,
    ) -> PyResult<()> {
        if configfile == ConfigFile::ALL {
            return Err(PyValueError::new_err("ConfigFile.ALL"));
        }
        let paths: Vec<PathBuf> = self.paths_for(configfile);
        if paths.is_empty() {
            return Err(PyValueError::new_err(format!(
                "{configfile:?}: file not found; retry in a workspace or set WEST_CONFIG_LOCAL"
            )));
        }
        if paths.len() > 1 {
            return Err(PyValueError::new_err(format!(
                "Cannot set value if multiple configs in use: {paths:?}"
            )));
        }
        let path = &paths[0];
        let cv = pyany_to_config_value(value)?;
        self.inner.set(option, cv, path).map_err(config_error_to_py)
    }

    /// Delete an option. `configfile=None` deletes from the
    /// highest-precedence layer that has it; `configfile=ConfigFile.ALL`
    /// deletes from every layer; a specific layer scopes the delete.
    /// Raises `KeyError` if the option doesn't exist in the requested
    /// scope.
    #[pyo3(signature = (option, configfile=None))]
    fn delete(&mut self, option: &str, configfile: Option<ConfigFile>) -> PyResult<()> {
        match configfile {
            None => self
                .inner
                .delete_topmost(option)
                .map_err(config_error_to_py),
            Some(ConfigFile::ALL) => {
                let all_paths: Vec<PathBuf> = self.paths_for(ConfigFile::ALL);
                let mut found = false;
                for path in all_paths {
                    match self.inner.delete(option, &path) {
                        Ok(()) => found = true,
                        Err(ConfigError::NotFound(_)) => continue,
                        Err(e) => return Err(config_error_to_py(e)),
                    }
                }
                if !found {
                    Err(PyKeyError::new_err(option.to_owned()))
                } else {
                    Ok(())
                }
            }
            Some(scope) => {
                let paths: Vec<PathBuf> = self.paths_for(scope);
                if paths.is_empty() {
                    return Err(PyKeyError::new_err(option.to_owned()));
                }
                let mut found = false;
                for path in paths {
                    match self.inner.delete(option, &path) {
                        Ok(()) => found = true,
                        Err(ConfigError::NotFound(_)) => continue,
                        Err(e) => return Err(config_error_to_py(e)),
                    }
                }
                if !found {
                    Err(PyKeyError::new_err(option.to_owned()))
                } else {
                    Ok(())
                }
            }
        }
    }

    /// Configured search paths for the requested scope. Returns all
    /// candidate paths whether they exist on disk or not.
    #[pyo3(signature = (location=ConfigFile::ALL))]
    fn get_search_paths<'py>(
        &self,
        py: Python<'py>,
        location: ConfigFile,
    ) -> PyResult<Bound<'py, PyList>> {
        paths_to_pylist(py, &self.paths_for(location))
    }

    /// Subset of `get_search_paths` containing only the layers that
    /// existed on disk at load time.
    #[pyo3(signature = (location=ConfigFile::ALL))]
    fn get_existing_paths<'py>(
        &self,
        py: Python<'py>,
        location: ConfigFile,
    ) -> PyResult<Bound<'py, PyList>> {
        let candidate: Vec<PathBuf> = self.paths_for(location);
        let existing: std::collections::HashSet<PathBuf> = self
            .inner
            .existing()
            .into_iter()
            .map(|p| p.to_path_buf())
            .collect();
        let kept: Vec<PathBuf> = candidate
            .into_iter()
            .filter(|p| existing.contains(p))
            .collect();
        paths_to_pylist(py, &kept)
    }

    /// Iterable of (option, value) pairs for the requested scope.
    /// Values are coerced to python via [`config_value_to_py`].
    #[pyo3(signature = (configfile=ConfigFile::ALL))]
    fn items<'py>(
        &self,
        py: Python<'py>,
        configfile: ConfigFile,
    ) -> PyResult<Bound<'py, PyList>> {
        let mut merged: std::collections::BTreeMap<String, ConfigValue> =
            std::collections::BTreeMap::new();
        let paths = self.paths_for(configfile);
        // Low → high precedence so later layers overwrite earlier.
        for layer in &paths {
            let items = self
                .inner
                .items_in(layer)
                .map_err(config_error_to_py)?;
            for (k, v) in items {
                merged.insert(k, v);
            }
        }
        let py_items: PyResult<Vec<Bound<'py, PyAny>>> = merged
            .into_iter()
            .map(|(k, v)| {
                let value = config_value_to_py(py, &v)?;
                let tuple = pyo3::types::PyTuple::new(py, [k.into_pyobject(py)?.into_any(), value])?;
                Ok(tuple.into_any())
            })
            .collect();
        PyList::new(py, py_items?)
    }
}

impl Configuration {
    /// Layer paths for a given `ConfigFile` scope, in low → high
    /// precedence order (matches `ResolvedConfig::layer_paths()`).
    fn paths_for(&self, configfile: ConfigFile) -> Vec<PathBuf> {
        match configfile {
            ConfigFile::ALL => self.resolved.layer_paths(),
            ConfigFile::SYSTEM => self.resolved.system.iter().cloned().collect(),
            ConfigFile::GLOBAL => {
                let mut out: Vec<PathBuf> = Vec::new();
                if let Some(g) = &self.resolved.global {
                    out.push(g.clone());
                }
                out.extend(self.resolved.global_confd.iter().cloned());
                out
            }
            ConfigFile::LOCAL => self.resolved.local.iter().cloned().collect(),
        }
    }
}

/// Materialise a list of paths as `pathlib.Path` objects in a python
/// list. The legacy `Configuration` did the same — callers expect real
/// `Path` instances out of `get_search_paths()` etc.
fn paths_to_pylist<'py>(py: Python<'py>, paths: &[PathBuf]) -> PyResult<Bound<'py, PyList>> {
    let pathlib = py.import("pathlib")?;
    let path_cls = pathlib.getattr("Path")?;
    let items: PyResult<Vec<Bound<'py, PyAny>>> = paths
        .iter()
        .map(|p| path_cls.call1((p.to_string_lossy().into_owned(),)))
        .collect();
    PyList::new(py, items?)
}

/// Convert a `ConfigValue` into a fresh python object. Scalars map to
/// their natural python types; `List` recurses.
fn config_value_to_py<'py>(py: Python<'py>, v: &ConfigValue) -> PyResult<Bound<'py, PyAny>> {
    match v {
        ConfigValue::String(s) => Ok(s.clone().into_pyobject(py)?.into_any()),
        ConfigValue::Bool(b) => Ok(b.into_pyobject(py)?.to_owned().into_any()),
        ConfigValue::Integer(i) => Ok(i.into_pyobject(py)?.into_any()),
        ConfigValue::Float(f) => Ok(f.into_pyobject(py)?.into_any()),
        ConfigValue::List(items) => {
            let py_items: PyResult<Vec<Bound<'py, PyAny>>> =
                items.iter().map(|x| config_value_to_py(py, x)).collect();
            Ok(PyList::new(py, py_items?)?.into_any())
        }
    }
}

/// Extract a `ConfigValue` from any python value. The order matters:
/// `bool` is a subclass of `int` in python, so check it first; `int`
/// before `float` so an int literal doesn't get promoted; `str` after
/// numerics (a numeric string should stay `String`); iterables last.
fn pyany_to_config_value(value: &Bound<'_, PyAny>) -> PyResult<ConfigValue> {
    if let Ok(b) = value.extract::<bool>() {
        // `True`/`False` extract cleanly to `bool` but `1`/`0` also extract to
        // `bool` because they're truthy ints. Guard against that by also
        // requiring the python type to be exactly `bool`.
        if value.is_instance_of::<pyo3::types::PyBool>() {
            return Ok(ConfigValue::Bool(b));
        }
    }
    if let Ok(i) = value.extract::<i64>() {
        return Ok(ConfigValue::Integer(i));
    }
    if let Ok(f) = value.extract::<f64>() {
        return Ok(ConfigValue::Float(f));
    }
    if let Ok(s) = value.extract::<String>() {
        return Ok(ConfigValue::String(s));
    }
    if let Ok(seq) = value.try_iter() {
        let items: PyResult<Vec<ConfigValue>> = seq
            .map(|item| pyany_to_config_value(&item?))
            .collect();
        return Ok(ConfigValue::List(items?));
    }
    Err(PyTypeError::new_err(format!(
        "unsupported value type for Configuration.set: {}",
        value.get_type().name()?
    )))
}

/// Map `west_core::ConfigError` variants to python exception types.
/// `MalformedToml` is the load-time parser failure that the legacy
/// python code surfaced as `MalformedConfig`; everything else is
/// `RuntimeError` (no caller in the codebase relies on a finer-grained
/// mapping today, per grep at planning time).
fn config_error_to_py(err: ConfigError) -> PyErr {
    match err {
        ConfigError::MalformedToml { .. } => MalformedConfig::new_err(err.to_string()),
        ConfigError::InvalidKey(_) | ConfigError::InvalidValue { .. } => {
            PyValueError::new_err(err.to_string())
        }
        ConfigError::NotFound(opt) => PyKeyError::new_err(opt),
        other => PyRuntimeError::new_err(other.to_string()),
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("MalformedConfig", m.py().get_type::<MalformedConfig>())?;
    m.add_class::<ConfigFile>()?;
    m.add_class::<Configuration>()?;
    Ok(())
}
