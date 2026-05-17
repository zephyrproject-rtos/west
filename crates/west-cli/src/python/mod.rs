//! PyO3 bindings exposing `west-core` primitives to python.
//!
//! Imported as `_west_native` by the python compat layer in `src/west/`.
//! Each submodule registers its types/functions with the python module
//! via its `register` entry-point; this file is the wiring shell.

use pyo3::prelude::*;

mod config;
mod topdir;

#[pymodule]
fn _west_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    topdir::register(m)?;
    config::register(m)?;
    Ok(())
}
