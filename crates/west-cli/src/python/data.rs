//! Generic structured-data parse/dump bindings.
//!
//! Exposes six functions to python:
//!
//! ```text
//!   _west_native.parse_yaml(source: str) -> Any   # dict/list/str/int/float/bool/None
//!   _west_native.parse_toml(source: str) -> Any
//!   _west_native.parse_json(source: str) -> Any
//!   _west_native.dump_yaml(value: Any) -> str
//!   _west_native.dump_toml(value: Any) -> str
//!   _west_native.dump_json(value: Any) -> str
//! ```
//!
//! All three formats route through `serde_json::Value` as the
//! intermediate representation: parsers deserialize into `Value`,
//! `Value` → python via `value_to_py`, and the reverse for dumps.
//! `Value` is the right pivot because all three formats share the
//! same JSON-flavoured data model (scalar / array / object), and the
//! conversion between it and python primitives is a tight match.
//!
//! These bindings are what lets `src/west/` drop `pyyaml` and
//! `pykwalify` — manifest validate / from_data, the west-commands
//! parser, and any future structured-data work go through here.

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString};
use serde_json::Value;

// ---- Parsers -------------------------------------------------------------

#[pyfunction]
fn parse_yaml<'py>(py: Python<'py>, source: &str) -> PyResult<Bound<'py, PyAny>> {
    let v: Value = serde_yaml_ng::from_str(source)
        .map_err(|e| PyValueError::new_err(format!("parse_yaml: {e}")))?;
    value_to_py(py, &v)
}

#[pyfunction]
fn parse_toml<'py>(py: Python<'py>, source: &str) -> PyResult<Bound<'py, PyAny>> {
    let v: Value = toml_edit::de::from_str(source)
        .map_err(|e| PyValueError::new_err(format!("parse_toml: {e}")))?;
    value_to_py(py, &v)
}

#[pyfunction]
fn parse_json<'py>(py: Python<'py>, source: &str) -> PyResult<Bound<'py, PyAny>> {
    let v: Value = serde_json::from_str(source)
        .map_err(|e| PyValueError::new_err(format!("parse_json: {e}")))?;
    value_to_py(py, &v)
}

// ---- Dumpers -------------------------------------------------------------

#[pyfunction]
fn dump_yaml(value: &Bound<'_, PyAny>) -> PyResult<String> {
    let v = py_to_value(value)?;
    serde_yaml_ng::to_string(&v).map_err(|e| PyValueError::new_err(format!("dump_yaml: {e}")))
}

#[pyfunction]
fn dump_toml(value: &Bound<'_, PyAny>) -> PyResult<String> {
    let v = py_to_value(value)?;
    // toml-rs (via toml_edit's serde feature) requires the top-level
    // value to be a table — TOML has no canonical scalar/array root.
    // Surface that mismatch as a clear TypeError rather than the
    // cryptic serde error.
    if !matches!(v, Value::Object(_)) {
        return Err(PyTypeError::new_err(
            "dump_toml: top-level value must be a dict",
        ));
    }
    toml_edit::ser::to_string(&v).map_err(|e| PyValueError::new_err(format!("dump_toml: {e}")))
}

#[pyfunction]
fn dump_json(value: &Bound<'_, PyAny>) -> PyResult<String> {
    let v = py_to_value(value)?;
    serde_json::to_string(&v).map_err(|e| PyValueError::new_err(format!("dump_json: {e}")))
}

// ---- serde_json::Value ↔ python -----------------------------------------

/// Convert a `serde_json::Value` into a fresh python object.
///
/// `pub(super)` so the `Project.userdata` / `ManifestRepo.userdata`
/// getters in the manifest binding can reuse it.
pub(super) fn value_to_py<'py>(py: Python<'py>, v: &Value) -> PyResult<Bound<'py, PyAny>> {
    match v {
        Value::Null => Ok(py.None().into_bound(py)),
        Value::Bool(b) => Ok(b.into_pyobject(py)?.to_owned().into_any()),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Ok(i.into_pyobject(py)?.into_any())
            } else if let Some(f) = n.as_f64() {
                Ok(f.into_pyobject(py)?.into_any())
            } else {
                // u64 values that don't fit i64.
                let s = n.to_string();
                Ok(s.into_pyobject(py)?.into_any())
            }
        }
        Value::String(s) => Ok(s.clone().into_pyobject(py)?.into_any()),
        Value::Array(arr) => {
            let items: PyResult<Vec<Bound<'py, PyAny>>> =
                arr.iter().map(|x| value_to_py(py, x)).collect();
            Ok(PyList::new(py, items?)?.into_any())
        }
        Value::Object(map) => {
            let d = PyDict::new(py);
            for (k, val) in map {
                d.set_item(k, value_to_py(py, val)?)?;
            }
            Ok(d.into_any())
        }
    }
}

/// Extract a `serde_json::Value` from any python value. Bool is
/// checked before int because `bool` is an `int` subclass in python
/// and would otherwise collapse `True` to `Number(1)`.
///
/// `pub(super)` so the `Manifest.from_dict` binding can reuse this
/// path (python dict → `Value` → `Manifest`, no JSON detour).
pub(super) fn py_to_value(value: &Bound<'_, PyAny>) -> PyResult<Value> {
    if value.is_none() {
        return Ok(Value::Null);
    }
    if value.is_instance_of::<PyBool>() {
        return Ok(Value::Bool(value.extract::<bool>()?));
    }
    if value.is_instance_of::<PyInt>() {
        let i: i64 = value.extract().map_err(|_| {
            PyValueError::new_err("integer value out of i64 range; serialize as string instead")
        })?;
        return Ok(Value::from(i));
    }
    if value.is_instance_of::<PyFloat>() {
        let f: f64 = value.extract()?;
        return serde_json::Number::from_f64(f)
            .map(Value::Number)
            .ok_or_else(|| {
                PyValueError::new_err("non-finite float (NaN / inf) is not serializable")
            });
    }
    if value.is_instance_of::<PyString>() {
        return Ok(Value::String(value.extract::<String>()?));
    }
    if value.is_instance_of::<PyDict>() {
        let d = value.cast::<PyDict>()?;
        let mut map = serde_json::Map::with_capacity(d.len());
        for (k, v) in d.iter() {
            let key: String = k.extract().map_err(|_| {
                let tname = k
                    .get_type()
                    .name()
                    .map(|n| n.to_string())
                    .unwrap_or_else(|_| "?".to_owned());
                PyTypeError::new_err(format!("dict keys must be strings (got {tname})"))
            })?;
            map.insert(key, py_to_value(&v)?);
        }
        return Ok(Value::Object(map));
    }
    // Anything iterable that isn't a string/dict — list, tuple, set,
    // generator, etc. — becomes an array.
    if let Ok(seq) = value.try_iter() {
        let items: PyResult<Vec<Value>> = seq.map(|item| py_to_value(&item?)).collect();
        return Ok(Value::Array(items?));
    }
    Err(PyTypeError::new_err(format!(
        "unsupported value type for structured-data dump: {}",
        value.get_type().name()?
    )))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(parse_yaml, m)?)?;
    m.add_function(wrap_pyfunction!(parse_toml, m)?)?;
    m.add_function(wrap_pyfunction!(parse_json, m)?)?;
    m.add_function(wrap_pyfunction!(dump_yaml, m)?)?;
    m.add_function(wrap_pyfunction!(dump_toml, m)?)?;
    m.add_function(wrap_pyfunction!(dump_json, m)?)?;
    Ok(())
}
