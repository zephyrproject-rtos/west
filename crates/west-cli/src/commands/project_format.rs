//! Shared per-project format-string rendering for commands that
//! expose `-f / --format` (`list`, `compare`, eventually any of the
//! project-iterating commands). Centralises the lazy key lookup so
//! the key set is identical across commands.
//!
//! Format strings use python-style `{key:[fill][align][width]}`
//! specs via the `strfmt` crate. Keys that need filesystem or vcs
//! access (`sha`, `cloned`, `active`) only run their lookup when the
//! template references them — relevant for workspaces with hundreds
//! of projects.

use std::path::{Path, PathBuf};

use west_core::loaded::LoadedManifest;
use west_core::manifest::Project;
use west_core::vcs::Vcs;

use super::select;

/// Errors that can arise while rendering a per-project format
/// template. Commands that consume this convert to their own error
/// enum via a `From` impl.
#[derive(Debug, thiserror::Error)]
pub(crate) enum FormatError {
    #[error("unknown format key: {{{0}}}")]
    UnknownKey(String),
    #[error("format error: {0}")]
    Format(String),
    #[error("project {0:?} is not cloned; cannot resolve {{sha}} (run `west update` first)")]
    UnclonedSha(String),
    #[error("{0}")]
    Vcs(String),
}

/// Per-project state the format keys read from. Constructed once per
/// project per render pass.
pub(crate) struct ProjectContext<'a> {
    pub project: &'a Project,
    pub loaded: &'a LoadedManifest,
    pub workspace: &'a Path,
    pub vcs: &'a dyn Vcs,
}

impl ProjectContext<'_> {
    pub(crate) fn lookup(&self, key: &str) -> Result<String, FormatError> {
        match key {
            "name" => Ok(self.project.name.clone()),
            "description" => Ok(self
                .project
                .description
                .clone()
                .unwrap_or_else(|| "None".into())),
            // Empty url/revision → "N/A". Real projects are validated to
            // have a non-empty url and a revision, so this fallback only
            // fires for the synthetic manifest project (matches Python's
            // `project.url or 'N/A'` rendering in `_format_project`).
            "url" => Ok(or_na(&self.project.url)),
            "path" => Ok(self.project.path.to_string_lossy().into_owned()),
            "abspath" => Ok(self
                .workspace
                .join(&self.project.path)
                .to_string_lossy()
                .into_owned()),
            "posixpath" => Ok({
                // Mirror python's `PurePath.as_posix()`: rewrite the
                // native separator only on Windows. On POSIX, `\` is
                // a legal filename character and must not be touched.
                let s = self
                    .workspace
                    .join(&self.project.path)
                    .to_string_lossy()
                    .into_owned();
                if cfg!(windows) { s.replace('\\', "/") } else { s }
            }),
            "revision" => Ok(or_na(&self.project.revision)),
            "remote" => Ok(self.project.remote_name.clone()),
            "clone_depth" => Ok(self
                .project
                .clone_depth
                .map(|n| n.to_string())
                .unwrap_or_else(|| "None".into())),
            "groups" => Ok(self.project.groups.join(",")),
            "active" => Ok(if self.loaded.is_active(self.project, &[]) {
                "active".into()
            } else {
                "inactive".into()
            }),
            "cloned" => Ok(if self.is_cloned() {
                "cloned".into()
            } else {
                "not-cloned".into()
            }),
            "sha" => self.compute_sha(),
            other => Err(FormatError::UnknownKey(other.to_owned())),
        }
    }

    fn repo_path(&self) -> PathBuf {
        self.workspace.join(&self.project.path)
    }

    fn is_cloned(&self) -> bool {
        let repo = self.repo_path();
        repo.exists() && self.vcs.is_repo(&repo).unwrap_or(false)
    }

    fn compute_sha(&self) -> Result<String, FormatError> {
        // The synthetic manifest project has no manifest-controlled
        // revision — the manifest repo's HEAD moves under the user's
        // own control, not west's. Match v1's "N/A" rendering.
        if select::is_synthetic_manifest_project(self.project) {
            return Ok("N/A".into());
        }
        if !self.is_cloned() {
            return Err(FormatError::UnclonedSha(self.project.name.clone()));
        }
        self.vcs
            .sha(&self.repo_path(), "HEAD")
            .map_err(|e| FormatError::Vcs(e.to_string()))
    }
}

fn or_na(s: &str) -> String {
    if s.is_empty() {
        "N/A".into()
    } else {
        s.to_owned()
    }
}

/// Render `template` with `ctx`'s key lookups. Surfaces errors
/// through [`FormatError`].
pub(crate) fn render(template: &str, ctx: &ProjectContext<'_>) -> Result<String, FormatError> {
    strfmt::strfmt_map(template, |mut fmt: strfmt::Formatter<'_, '_>| {
        // Errors flow back through `FmtError::KeyError(String)`; we
        // recover them after `strfmt_map` returns via the message
        // round-trip. Acceptable because the only consumer is a
        // human-facing error message.
        match ctx.lookup(fmt.key) {
            Ok(value) => fmt.str(&value),
            Err(e) => Err(strfmt::FmtError::KeyError(e.to_string())),
        }
    })
    .map_err(|e| match e {
        strfmt::FmtError::KeyError(msg) => parse_back_format_error(&msg),
        strfmt::FmtError::TypeError(msg) | strfmt::FmtError::Invalid(msg) => {
            FormatError::Format(msg)
        }
    })
}

/// Best-effort: when `strfmt` surfaces a `KeyError` that our lookup
/// produced, the message starts with the human-readable form of one
/// of our `FormatError` variants. Keep the original text rather than
/// re-typing — losing the typed structure here is OK because the
/// caller just prints the message.
fn parse_back_format_error(msg: &str) -> FormatError {
    if let Some(rest) = msg.strip_prefix("unknown format key: {")
        && let Some(key) = rest.strip_suffix('}')
    {
        return FormatError::UnknownKey(key.to_owned());
    }
    FormatError::Format(msg.to_owned())
}
