//! PyO3 bindings exposing `west-core` primitives to python.
//!
//! Imported as `_west_native` by the python compat layer in `src/west/`.
//! This is the Phase 2a starter cut: `west_topdir()` + `WestNotFound`
//! only — both leaf utilities that pin down the abi3 build, the
//! `west-core` dependency graph, and the python-side fallback pattern.
//! Phase 2b grows this module to back the `Manifest`, `Configuration`,
//! `Project` public python classes.

use std::env;
use std::path::PathBuf;

use pyo3::create_exception;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

create_exception!(
    _west_native,
    WestNotFound,
    PyRuntimeError,
    "Neither the current directory nor any parent has a west workspace."
);

/// Walk upward from `start` (or the current working directory) looking
/// for a `.west/` marker; return the parent directory as a string, or
/// raise `WestNotFound` when the walk hits the filesystem root.
#[pyfunction]
#[pyo3(signature = (start=None))]
fn west_topdir(start: Option<PathBuf>) -> PyResult<String> {
    let start = match start {
        Some(p) => p,
        None => env::current_dir().map_err(|e| {
            PyRuntimeError::new_err(format!("could not get current directory: {e}"))
        })?,
    };
    match west_core::topdir::topdir(&start) {
        Ok(p) => Ok(p.to_string_lossy().into_owned()),
        Err(_) => Err(WestNotFound::new_err(
            "Could not find a west workspace in this or any parent directory",
        )),
    }
}

#[pymodule]
fn _west_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("WestNotFound", m.py().get_type::<WestNotFound>())?;
    m.add_function(wrap_pyfunction!(west_topdir, m)?)?;
    Ok(())
}
