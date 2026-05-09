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

use super::Settings;

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
