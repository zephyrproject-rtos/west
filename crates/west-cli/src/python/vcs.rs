//! Thin PyO3 surface over `west_core::vcs` helpers that the python
//! wrapper needs to keep semantic parity with the rust CLI without
//! duplicating shell-out logic.
//!
//! Right now there's exactly one helper: [`read_at_ref`], the
//! "read a file from git at this revision" primitive that
//! [`west_core::vcs::Vcs::read_at_ref`] exposes for the import
//! resolver. The python `_filesystem_importer` delegates here so
//! both languages share one definition of "what does
//! `manifest-rev:<file>` mean, and which git errors are soft-fails."

use std::path::PathBuf;

use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use west_core::vcs::{GitClient, GitOptions, Vcs};

/// Read `relative_path` from `repo` at the git revision `rev`.
///
/// Returns the raw bytes when the path resolves, `None` when either
/// the ref or the path is absent at that ref (matches v1's
/// `_manifest_content_at` soft-fail), and raises `OSError` for
/// genuine git invocation failures.
///
/// The west import resolver passes `refs/heads/manifest-rev` as `rev`
/// — the python `_filesystem_importer` consumes the result.
#[pyfunction]
#[pyo3(signature = (repo, rev, relative_path))]
fn read_at_ref<'py>(
    py: Python<'py>,
    repo: PathBuf,
    rev: &str,
    relative_path: PathBuf,
) -> PyResult<Option<Bound<'py, PyBytes>>> {
    if rev.is_empty() {
        return Err(PyValueError::new_err("rev must be non-empty"));
    }
    let client = GitClient::new(GitOptions::default());
    match client.read_at_ref(&repo, rev, &relative_path) {
        Ok(Some(bytes)) => Ok(Some(PyBytes::new(py, &bytes))),
        Ok(None) => Ok(None),
        Err(e) => Err(PyOSError::new_err(e.to_string())),
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(read_at_ref, m)?)?;
    Ok(())
}
