//! PyO3 bindings exposing `west-core` primitives to python.
//!
//! Imported as `_west_native` by the python compat layer in `src/west/`.
//! Each submodule registers its types/functions with the python module
//! via its `register` entry-point; this file is the wiring shell.

use pyo3::prelude::*;

mod config;
mod data;
mod manifest;
mod topdir;

#[pymodule]
fn _west_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Bridge rust `log::*` records into python's `logging` module so
    // diagnostics from west-core (`log::warn!(target: "west.manifest",
    // ...)`, the import-resolver's debug logs, etc.) reach python's
    // logger hierarchy and pytest's `caplog`. `try_init` so re-imports
    // (e.g. test runs that reload the module) don't panic on the
    // already-set global logger.
    let _ = pyo3_log::try_init();
    topdir::register(m)?;
    config::register(m)?;
    manifest::register(m)?;
    data::register(m)?;
    Ok(())
}
