//! Bindings for `west_core::topdir`: `west_topdir()` walks upward from a
//! starting directory looking for a `.west/` marker, and `WestNotFound`
//! is the exception raised when no workspace is in scope.

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

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("WestNotFound", m.py().get_type::<WestNotFound>())?;
    m.add_function(wrap_pyfunction!(west_topdir, m)?)?;
    Ok(())
}
