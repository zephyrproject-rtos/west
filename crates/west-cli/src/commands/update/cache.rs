//! Cache-source resolution for `west update`.
//!
//! Three independent cache modes exist; on every project the worker
//! asks for the highest-priority match:
//!
//! 1. `--name-cache <DIR>` — clone source `<DIR>/<project.name>`,
//!    used only if the directory exists. The user populates and
//!    maintains it out-of-band.
//! 2. `--path-cache <DIR>` — clone source `<DIR>/<project.path>`,
//!    same "user-managed" contract as `name-cache`.
//! 3. `--auto-cache <DIR>` — west populates and refreshes a bare
//!    mirror at `<DIR>/<basename(url)>/<md5_hex(url)>`. Always
//!    selected when set; missing directories trigger a `git clone
//!    --mirror`, present ones are smart-fetched before the workspace
//!    clone.
//!
//! Each project resolves independently — the same workspace might
//! use a name-cache for one project and an auto-cache for another.

use std::path::{Path, PathBuf};

use md5::{Digest, Md5};
use west_core::manifest::Project;
use west_core::vcs::{CloneSpec, FetchSpec, Output, RevSpec, RevType, Vcs};

use super::Settings;
use super::error::UpdateError;

/// Where the worker should source `project`'s clone from.
pub(super) enum CacheSource {
    /// User-managed cache — name-cache or path-cache. The directory
    /// already exists and is expected to be a populated repository
    /// (bare or normal). The worker uses it as the clone source as-is.
    Static(PathBuf),
    /// Auto-cache — west owns this directory. Worker must ensure it's
    /// populated (mirror-clone) and current (smart-fetch) before using
    /// it as the clone source.
    Auto(PathBuf),
}

impl CacheSource {
    pub(super) fn path(&self) -> &Path {
        match self {
            CacheSource::Static(p) | CacheSource::Auto(p) => p,
        }
    }
}

/// Resolve the highest-priority cache source for `project`. Returns
/// `None` when no cache flag is set or none of the static caches has a
/// matching directory and `auto-cache` is also unset.
pub(super) fn resolve_cache_source(project: &Project, settings: &Settings) -> Option<CacheSource> {
    if let Some(dir) = settings.name_cache.as_deref() {
        let candidate = dir.join(&project.name);
        if candidate.is_dir() {
            return Some(CacheSource::Static(candidate));
        }
    }
    if let Some(dir) = settings.path_cache.as_deref() {
        let candidate = dir.join(&project.path);
        if candidate.is_dir() {
            return Some(CacheSource::Static(candidate));
        }
    }
    if let Some(dir) = settings.auto_cache.as_deref() {
        return Some(CacheSource::Auto(auto_cache_path(dir, &project.url)));
    }
    None
}

/// Compute the auto-cache directory layout for a given URL. Mirrors
/// python: `<dir>/<url_basename(url)>/<md5_hex(url)>`. The basename
/// is human-readable, the md5 prevents collisions when two URLs share
/// the same basename.
pub(super) fn auto_cache_path(dir: &Path, url: &str) -> PathBuf {
    dir.join(url_basename(url)).join(md5_hex(url))
}

/// Populate / refresh an auto-cache mirror at `cache_path` for
/// `project`. First-time: mirror-clones the project URL. Subsequent
/// runs: smart-skips when the manifest pins a SHA the cache already
/// has, otherwise fetches all refs. Called both by
/// `WorkspaceImportSource::materialize` (the import-resolution
/// phase) and by the main worker pool (per-project update).
pub(super) fn ensure_auto_cache(
    vcs: &dyn Vcs,
    project: &Project,
    cache_path: &Path,
    out: &mut Output<'_>,
) -> Result<(), UpdateError> {
    if cache_path.exists() && vcs.is_repo(cache_path).unwrap_or(false) {
        // Smart-skip path: only when the manifest pins an immutable
        // revision the cache already resolves. The fetch we'd
        // otherwise run is `revision: None`, which can't smart-skip.
        // Classification is authoritative — `RevType::Branch` keeps
        // us fetching even if the branch name happens to look like a
        // SHA, and `Tag` correctly skips even when it doesn't.
        let rev = RevSpec::Named(&project.revision);
        if matches!(
            vcs.rev_type(cache_path, rev).unwrap_or(RevType::Other),
            RevType::Tag | RevType::Commit
        ) && vcs.sha(cache_path, rev).is_ok()
        {
            return Ok(());
        }
        vcs.fetch(
            cache_path,
            &FetchSpec {
                remote: "origin",
                revision: None,
            },
            out,
        )
        .map_err(|source| UpdateError::CacheRefresh {
            path: cache_path.to_path_buf(),
            source,
        })?;
        return Ok(());
    }
    if let Some(parent) = cache_path.parent() {
        std::fs::create_dir_all(parent).map_err(|source| UpdateError::CreateParent {
            path: parent.to_path_buf(),
            source,
        })?;
    }
    vcs.clone(
        &CloneSpec {
            url: &project.url,
            dest: cache_path,
            revision: None,
            origin: None,
            mirror: true,
        },
        out,
    )
    .map_err(|source| UpdateError::CachePopulate {
        url: project.url.clone(),
        source,
    })
}

/// Populate the auto-cache (if configured) and clone `project` into
/// `dest`. The clone source is the cache directory when a cache
/// flag matched; otherwise the project URL directly. After a
/// cache-driven clone the recorded remote URL is rewritten to the
/// project's real upstream so subsequent fetches go to the network.
///
/// Caller must guarantee `dest` is not already a valid git repo;
/// this function only handles the "first clone" case.
pub(super) fn clone_via_cache(
    vcs: &dyn Vcs,
    project: &Project,
    settings: &Settings,
    dest: &Path,
    out: &mut Output<'_>,
) -> Result<(), UpdateError> {
    let cache_source = resolve_cache_source(project, settings);
    if let Some(CacheSource::Auto(path)) = &cache_source {
        ensure_auto_cache(vcs, project, path, out)?;
    }
    if let Some(parent) = dest.parent() {
        std::fs::create_dir_all(parent).map_err(|source| UpdateError::CreateParent {
            path: parent.to_path_buf(),
            source,
        })?;
    }
    let clone_url = match cache_source.as_ref() {
        Some(src) => src
            .path()
            .to_str()
            .ok_or_else(|| UpdateError::NonUtf8CachePath(src.path().to_path_buf()))?,
        None => &project.url,
    };
    vcs.clone(
        &CloneSpec {
            url: clone_url,
            dest,
            revision: None,
            origin: Some(&project.remote_name),
            mirror: false,
        },
        out,
    )
    .map_err(|source| UpdateError::Clone {
        url: clone_url.to_owned(),
        source,
    })?;
    if cache_source.is_some() {
        vcs.set_remote_url(dest, &project.remote_name, &project.url)
            .map_err(UpdateError::SetRemoteUrl)?;
    }
    Ok(())
}

/// Hex md5 of `url`. Lowercase, 32 chars. Matches python's
/// `hashlib.md5(url.encode()).hexdigest()` so the directory layout
/// interops between rust and python on shared CI auto-caches.
fn md5_hex(url: &str) -> String {
    let digest = Md5::digest(url.as_bytes());
    let mut s = String::with_capacity(32);
    for b in digest.iter() {
        use std::fmt::Write as _;
        let _ = write!(s, "{b:02x}");
    }
    s
}

/// Strip URL noise (path separators, scheme prefixes, trailing `.git`)
/// to get a sensible directory-name component. Same shape as
/// `init.rs::url_basename` — duplicated here to keep the cache module
/// self-contained until a third user lands.
fn url_basename(url: &str) -> String {
    let last = url
        .rsplit(['/', '\\', ':'])
        .find(|s| !s.is_empty())
        .unwrap_or(url);
    let trimmed = last.strip_suffix(".git").unwrap_or(last);
    if trimmed.is_empty() {
        "manifest".to_owned()
    } else {
        trimmed.to_owned()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn md5_hex_matches_python_hashlib() {
        // python: hashlib.md5(b"https://example.com/p.git").hexdigest()
        // = "64822c6e6fcc385fcaeffa7084b7e6c1"
        assert_eq!(
            md5_hex("https://example.com/p.git"),
            "64822c6e6fcc385fcaeffa7084b7e6c1",
        );
    }

    #[test]
    fn auto_cache_path_layout() {
        let p = auto_cache_path(Path::new("/cache"), "https://example.com/foo.git");
        // basename strips `.git`; md5 is the lowercase hex.
        assert!(p.starts_with("/cache/foo/"));
        assert_eq!(p.file_name().unwrap().len(), 32);
    }

    #[test]
    fn url_basename_strips_dot_git() {
        assert_eq!(url_basename("https://example.com/foo.git"), "foo");
        assert_eq!(url_basename("git@github.com:org/proj"), "proj");
        assert_eq!(url_basename("/local/path/repo/"), "repo");
    }
}
